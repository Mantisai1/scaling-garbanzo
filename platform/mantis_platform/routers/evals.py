import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..audit import record
from ..auth import Principal, get_principal
from ..config import settings
from ..db import get_db
from ..models import AgentConfig, Dataset, DatasetVersion, EvalRun, Score
from ..services import agent_runner, scorers

router = APIRouter(prefix="/v1", tags=["evaluation"])

ITEM_REQUIRED = {"input"}


def _validate_items(items: list[dict]) -> list[dict]:
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not ITEM_REQUIRED <= set(it):
            raise HTTPException(422, f"item {i}: must be an object with at least 'input'")
        out.append({"id": it.get("id") or f"item-{i}", "input": it["input"], "expected": it.get("expected"),
                    "assertions": it.get("assertions", {}), "budgets": it.get("budgets", {}),
                    "protected_fields": it.get("protected_fields", []), "split": it.get("split", "eval"),
                    "source_trace_ids": it.get("source_trace_ids", []), "metadata": it.get("metadata", {})})
    return out


def snapshot(db: Session, p: Principal, dataset: Dataset, items: list[dict]) -> DatasetVersion:
    prev = db.query(DatasetVersion).filter_by(dataset_id=dataset.id).order_by(DatasetVersion.version.desc()).first()
    h = hashlib.sha256(json.dumps(items, sort_keys=True, default=str).encode()).hexdigest()
    v = DatasetVersion(dataset_id=dataset.id, project_id=dataset.project_id, version=(prev.version + 1 if prev else 1),
                       item_count=len(items), content_hash=h, items=items, created_by=p.id)
    db.add(v); db.flush()
    record(db, p, "dataset.snapshot", "dataset_version", v.id, project_id=dataset.project_id, dataset_id=dataset.id, version=v.version, items=len(items), hash=h)
    return v


class DatasetIn(BaseModel):
    name: str
    description: str | None = None
    kind: str = "eval"
    items: list[dict] = []
    project_id: str | None = None


@router.post("/datasets")
def create_dataset(body: DatasetIn, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("engineer")
    pid = p.scope_project(body.project_id)
    ds = Dataset(project_id=pid, name=body.name, description=body.description, kind=body.kind)
    db.add(ds); db.flush()
    record(db, p, "dataset.create", "dataset", ds.id, project_id=pid, name=body.name)
    v = snapshot(db, p, ds, _validate_items(body.items)) if body.items else None
    db.commit()
    return {"id": ds.id, "version_id": v.id if v else None, "version": v.version if v else None}


@router.post("/datasets/{dataset_id}/import")
async def import_items(dataset_id: str, file: UploadFile, project_id: str | None = None,
                       p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Import JSON (array), JSONL, or CSV (columns: input, expected, ...) as a NEW immutable version."""
    p.require("engineer")
    pid = p.scope_project(project_id)
    ds = db.query(Dataset).filter_by(id=dataset_id, project_id=pid).first()
    if not ds: raise HTTPException(404, "dataset not found")
    raw = (await file.read()).decode()
    name = (file.filename or "").lower()
    if name.endswith(".jsonl"):
        items = [json.loads(l) for l in raw.splitlines() if l.strip()]
    elif name.endswith(".csv"):
        import csv, io
        items = [{"input": r.pop("input"), "expected": r.pop("expected", None), "metadata": r} for r in csv.DictReader(io.StringIO(raw))]
    else:
        items = json.loads(raw)
    v = snapshot(db, p, ds, _validate_items(items)); db.commit()
    return {"version_id": v.id, "version": v.version, "items": v.item_count, "hash": v.content_hash}


@router.get("/datasets")
def list_datasets(project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    pid = p.scope_project(project_id)
    out = []
    for ds in db.query(Dataset).filter_by(project_id=pid):
        vs = db.query(DatasetVersion).filter_by(dataset_id=ds.id).order_by(DatasetVersion.version).all()
        out.append({"id": ds.id, "name": ds.name, "kind": ds.kind,
                    "versions": [{"id": v.id, "version": v.version, "items": v.item_count, "hash": v.content_hash[:12], "created_at": v.created_at.isoformat()} for v in vs]})
    return out


@router.get("/datasets/versions/{version_id}")
def get_version(version_id: str, project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    v = db.query(DatasetVersion).filter_by(id=version_id, project_id=p.scope_project(project_id)).first()
    if not v: raise HTTPException(404, "version not found")
    return {"id": v.id, "dataset_id": v.dataset_id, "version": v.version, "hash": v.content_hash, "items": v.items}


class AgentConfigIn(BaseModel):
    name: str
    version: str
    model: str
    prompt_version: str
    invoke: dict
    checkpoint_id: str | None = None
    project_id: str | None = None


@router.post("/agent-configs")
def create_agent_config(body: AgentConfigIn, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("engineer")
    pid = p.scope_project(body.project_id)
    ac = AgentConfig(project_id=pid, **body.model_dump(exclude={"project_id"}))
    db.add(ac); db.flush()
    record(db, p, "agent_config.create", "agent_config", ac.id, project_id=pid, name=body.name, version=body.version)
    db.commit()
    return {"id": ac.id}


@router.get("/agent-configs")
def list_agent_configs(project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    return [{"id": a.id, "name": a.name, "version": a.version, "model": a.model, "prompt_version": a.prompt_version,
             "invoke": a.invoke, "checkpoint_id": a.checkpoint_id}
            for a in db.query(AgentConfig).filter_by(project_id=p.scope_project(project_id))]


class EvalRunIn(BaseModel):
    dataset_version_id: str
    agent_config_id: str
    scorers: list[dict]
    environment_id: str | None = None
    project_id: str | None = None


@router.post("/eval-runs")
def run_eval(body: EvalRunIn, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Synchronous for the demo (datasets are small). Pins every input so the run is reproducible."""
    p.require("engineer")
    pid = p.scope_project(body.project_id)
    dv = db.query(DatasetVersion).filter_by(id=body.dataset_version_id, project_id=pid).first()
    ac = db.query(AgentConfig).filter_by(id=body.agent_config_id, project_id=pid).first()
    if not dv or not ac: raise HTTPException(404, "dataset version or agent config not found")
    run = EvalRun(project_id=pid, dataset_version_id=dv.id, agent_config_id=ac.id, scorers=body.scorers,
                  evaluator_version=scorers.EVALUATOR_VERSION, environment_image=f"mantis-eval-runtime:{settings.schema_version}",
                  status="running", created_by=p.id)
    db.add(run); db.flush()
    record(db, p, "eval_run.start", "eval_run", run.id, project_id=pid, dataset_version=dv.id, agent_config=ac.id, dataset_hash=dv.content_hash)
    results, sums = [], {}
    for item in dv.items:
        if item.get("split") not in (None, "eval", "test"):
            continue
        try:
            res = agent_runner.invoke(ac.invoke, item, pid, body.environment_id, db)
            item_scores = [scorers.score(sc, item, res) for sc in body.scorers]
            err = None
        except Exception as e:  # noqa: BLE001
            res, item_scores, err = {"output": None, "trace_id": None}, [], str(e)
        for s in item_scores:
            db.add(Score(project_id=pid, trace_id=res.get("trace_id"), eval_run_id=run.id, task_id=item["id"],
                         name=s["name"], value=s["value"], source=s["source"], evaluator_version=s["evaluator_version"], comment=s["comment"]))
            sums.setdefault(s["name"], []).append(s["value"])
        results.append({"task_id": item["id"], "input": item["input"], "expected": item.get("expected"),
                        "output": res.get("output"), "tool_calls": res.get("tool_calls", []), "trace_id": res.get("trace_id"),
                        "latency_ms": res.get("latency_ms"), "cost_usd": res.get("cost_usd"), "scores": item_scores, "error": err})
    run.results = results
    run.summary = {k: round(sum(v) / len(v), 4) for k, v in sums.items()}
    run.summary["_items"] = len(results)
    run.status = "completed"; run.finished_at = datetime.now(timezone.utc)
    record(db, p, "eval_run.complete", "eval_run", run.id, project_id=pid, summary=run.summary)
    db.commit()
    return {"id": run.id, "summary": run.summary}


@router.get("/eval-runs")
def list_runs(project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    return [{"id": r.id, "dataset_version_id": r.dataset_version_id, "agent_config_id": r.agent_config_id, "status": r.status,
             "summary": r.summary, "evaluator_version": r.evaluator_version, "created_at": r.created_at.isoformat()}
            for r in db.query(EvalRun).filter_by(project_id=p.scope_project(project_id)).order_by(EvalRun.created_at.desc())]


@router.get("/eval-runs/{run_id}")
def get_run(run_id: str, project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    r = db.query(EvalRun).filter_by(id=run_id, project_id=p.scope_project(project_id)).first()
    if not r: raise HTTPException(404, "run not found")
    ac = db.get(AgentConfig, r.agent_config_id); dv = db.get(DatasetVersion, r.dataset_version_id)
    return {"id": r.id, "status": r.status, "summary": r.summary, "results": r.results, "scorers": r.scorers,
            "pinned": {"dataset_version_id": dv.id, "dataset_hash": dv.content_hash, "agent_config": {"id": ac.id, "name": ac.name, "version": ac.version, "model": ac.model, "prompt_version": ac.prompt_version, "checkpoint_id": ac.checkpoint_id},
                       "evaluator_version": r.evaluator_version, "environment_image": r.environment_image}}


@router.get("/eval-runs/{run_id}/compare/{baseline_id}")
def compare(run_id: str, baseline_id: str, project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    pid = p.scope_project(project_id)
    a = db.query(EvalRun).filter_by(id=run_id, project_id=pid).first(); b = db.query(EvalRun).filter_by(id=baseline_id, project_id=pid).first()
    if not a or not b: raise HTTPException(404, "run not found")
    if a.dataset_version_id != b.dataset_version_id:
        raise HTTPException(400, "candidate and baseline must be evaluated on the same dataset version")
    metrics = sorted((set(a.summary) | set(b.summary)) - {"_items"})
    return {"candidate": run_id, "baseline": baseline_id, "dataset_version_id": a.dataset_version_id,
            "metrics": [{"name": m, "candidate": a.summary.get(m), "baseline": b.summary.get(m),
                         "delta": round((a.summary.get(m) or 0) - (b.summary.get(m) or 0), 4)} for m in metrics]}
