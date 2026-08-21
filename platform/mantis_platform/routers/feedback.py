from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..audit import record
from ..auth import Principal, get_principal
from ..db import get_db
from ..models import Annotation, Dataset, EvalRun, Span, TrainingSignal
from .evals import snapshot

router = APIRouter(prefix="/v1", tags=["feedback"])
SIGNAL_TYPES = ("outcome_label", "preference_pair", "corrected_output", "tool_quality", "incident")


class QueueIn(BaseModel):
    trace_ids: list[str] = []
    eval_run_id: str | None = None
    only_failures_below: float | None = None  # when queuing from an eval run, queue items whose min score < this
    protected: bool = False
    project_id: str | None = None


@router.post("/annotations/queue")
def queue(body: QueueIn, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Put traces or eval-run results into the human review queue."""
    p.require("engineer")
    pid = p.scope_project(body.project_id)
    created = []
    for tid in body.trace_ids:
        spans = db.query(Span).filter_by(project_id=pid, trace_id=tid).all()
        if not spans: raise HTTPException(404, f"trace {tid} not found")
        llm = next((s for s in spans if s.kind == "llm_generation"), spans[0])
        a = Annotation(project_id=pid, trace_id=tid, protected=body.protected,
                       input={"prompt": llm.attributes.get("gen_ai.prompt")}, output={"completion": llm.attributes.get("gen_ai.completion")})
        db.add(a); db.flush(); created.append(a.id)
    if body.eval_run_id:
        run = db.query(EvalRun).filter_by(id=body.eval_run_id, project_id=pid).first()
        if not run: raise HTTPException(404, "eval run not found")
        for r in run.results:
            worst = min([s["value"] for s in r["scores"]] or [0])
            if body.only_failures_below is not None and worst >= body.only_failures_below:
                continue
            a = Annotation(project_id=pid, trace_id=r.get("trace_id"), eval_run_id=run.id, task_id=r["task_id"], protected=body.protected,
                           input={"task": r["input"], "expected": r.get("expected")}, output={"completion": r.get("output"), "tool_calls": r.get("tool_calls")})
            db.add(a); db.flush(); created.append(a.id)
    record(db, p, "annotation.queue", "annotation", ",".join(created)[:64], project_id=pid, count=len(created))
    db.commit()
    return {"queued": created}


@router.get("/annotations")
def list_annotations(status: str | None = None, project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("reviewer", "viewer")
    q = db.query(Annotation).filter_by(project_id=p.scope_project(project_id))
    if status: q = q.filter_by(status=status)
    return [{"id": a.id, "status": a.status, "trace_id": a.trace_id, "task_id": a.task_id, "signal_type": a.signal_type,
             "input": a.input, "output": a.output, "label": a.label, "protected": a.protected,
             "reviewer_id": a.reviewer_id, "approver_id": a.approver_id} for a in q.order_by(Annotation.created_at)]


class AnnotateIn(BaseModel):
    signal_type: str
    label: dict  # e.g. {"correct": false, "corrected_output": "...", "chosen": "...", "rejected": "...", "notes": "..."}
    project_id: str | None = None


@router.post("/annotations/{ann_id}/annotate")
def annotate(ann_id: str, body: AnnotateIn, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("reviewer", "engineer")
    if body.signal_type not in SIGNAL_TYPES: raise HTTPException(400, f"signal_type must be one of {SIGNAL_TYPES}")
    a = db.query(Annotation).filter_by(id=ann_id, project_id=p.scope_project(body.project_id)).first()
    if not a: raise HTTPException(404, "annotation not found")
    if a.status == "approved": raise HTTPException(409, "already approved; immutable")
    a.signal_type, a.label, a.reviewer_id, a.status = body.signal_type, body.label, p.id, "annotated"
    a.updated_at = datetime.now(timezone.utc)
    record(db, p, "annotation.submit", "annotation", a.id, project_id=a.project_id, signal_type=body.signal_type)
    db.commit()
    return {"id": a.id, "status": a.status}


@router.post("/annotations/{ann_id}/approve")
def approve(ann_id: str, project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """The ONLY way a training signal comes into existence. Requires data_approver; reviewer cannot self-approve."""
    p.require("data_approver")
    a = db.query(Annotation).filter_by(id=ann_id, project_id=p.scope_project(project_id)).first()
    if not a: raise HTTPException(404, "annotation not found")
    if a.status != "annotated": raise HTTPException(409, f"annotation is '{a.status}', must be 'annotated'")
    if a.reviewer_id == p.id and p.role != "org_admin": raise HTTPException(403, "reviewer cannot approve their own annotation")
    rec = {"input": a.input, "output": a.output, "label": a.label}
    if a.signal_type == "corrected_output":
        rec["target"] = a.label.get("corrected_output")
    if a.signal_type == "preference_pair":
        rec["chosen"], rec["rejected"] = a.label.get("chosen"), a.label.get("rejected")
    sig = TrainingSignal(project_id=a.project_id, annotation_id=a.id, source_trace_id=a.trace_id, signal_type=a.signal_type,
                         record=rec, protected=a.protected, approved_by=p.id)
    a.status, a.approver_id, a.updated_at = "approved", p.id, datetime.now(timezone.utc)
    db.add(sig); db.flush()
    record(db, p, "annotation.approve", "training_signal", sig.id, project_id=a.project_id, annotation_id=a.id, source_trace_id=a.trace_id)
    db.commit()
    return {"training_signal_id": sig.id}


@router.post("/annotations/{ann_id}/reject")
def reject(ann_id: str, project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("data_approver")
    a = db.query(Annotation).filter_by(id=ann_id, project_id=p.scope_project(project_id)).first()
    if not a: raise HTTPException(404, "annotation not found")
    a.status, a.approver_id = "rejected", p.id
    record(db, p, "annotation.reject", "annotation", a.id, project_id=a.project_id); db.commit()
    return {"status": "rejected"}


@router.get("/training-signals")
def list_signals(project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    return [{"id": s.id, "signal_type": s.signal_type, "source_trace_id": s.source_trace_id, "annotation_id": s.annotation_id,
             "protected": s.protected, "approved_by": s.approved_by, "record": s.record}
            for s in db.query(TrainingSignal).filter_by(project_id=p.scope_project(project_id))]


class BuildTrainingDatasetIn(BaseModel):
    name: str
    signal_types: list[str] | None = None
    project_id: str | None = None


@router.post("/training-datasets")
def build_training_dataset(body: BuildTrainingDatasetIn, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Snapshot all approved signals into an immutable, versioned training dataset with per-record lineage."""
    p.require("data_approver", "engineer")
    pid = p.scope_project(body.project_id)
    q = db.query(TrainingSignal).filter_by(project_id=pid)
    if body.signal_types: q = q.filter(TrainingSignal.signal_type.in_(body.signal_types))
    sigs = q.all()
    if not sigs: raise HTTPException(400, "no approved training signals")
    items = [{"id": s.id, "input": s.record.get("input"), "expected": s.record.get("target") or s.record.get("chosen"),
              "assertions": {}, "budgets": {}, "protected_fields": ["*"] if s.protected else [], "split": "train",
              "source_trace_ids": [s.source_trace_id] if s.source_trace_id else [],
              "metadata": {"signal_type": s.signal_type, "annotation_id": s.annotation_id, "approved_by": s.approved_by, "record": s.record}} for s in sigs]
    ds = db.query(Dataset).filter_by(project_id=pid, name=body.name, kind="training").first()
    if not ds:
        ds = Dataset(project_id=pid, name=body.name, kind="training"); db.add(ds); db.flush()
        record(db, p, "dataset.create", "dataset", ds.id, project_id=pid, name=body.name, kind="training")
    v = snapshot(db, p, ds, items); db.commit()
    return {"dataset_id": ds.id, "version_id": v.id, "version": v.version, "records": len(items), "hash": v.content_hash,
            "protected_records": sum(1 for s in sigs if s.protected)}
