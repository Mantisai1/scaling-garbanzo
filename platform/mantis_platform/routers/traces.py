from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from ..audit import record
from ..auth import Principal, get_principal
from ..db import get_db
from ..models import Score, Span

router = APIRouter(prefix="/v1", tags=["traces"])


def _span_out(s: Span) -> dict:
    return {"span_id": s.span_id, "parent_span_id": s.parent_span_id, "name": s.name, "kind": s.kind,
            "start_ns": s.start_ns, "end_ns": s.end_ns, "duration_ms": round((s.end_ns - s.start_ns) / 1e6, 3),
            "status": s.status, "provider": s.provider, "model": s.model, "input_tokens": s.input_tokens,
            "output_tokens": s.output_tokens, "cost_usd": s.cost_usd, "attributes": s.attributes, "events": s.events,
            "redactions_applied": s.redactions_applied}


@router.get("/traces")
def list_traces(project_id: str | None = None, session_id: str | None = None, user_ref: str | None = None,
                release: str | None = None, model: str | None = None, status: str | None = None,
                environment_id: str | None = None, q: str | None = None, limit: int = 50,
                p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    pid = p.scope_project(project_id)
    base = db.query(Span).filter(Span.project_id == pid)
    if session_id: base = base.filter(Span.session_id == session_id)
    if user_ref: base = base.filter(Span.user_ref == user_ref)
    if release: base = base.filter(Span.release == release)
    if model: base = base.filter(Span.model == model)
    if status: base = base.filter(Span.status == status)
    if environment_id: base = base.filter(Span.environment_id == environment_id)
    if q: base = base.filter(Span.name.ilike(f"%{q}%"))
    agg = (base.with_entities(
        Span.trace_id, func.min(Span.start_ns), func.max(Span.end_ns), func.count(Span.id),
        func.sum(Span.cost_usd), func.sum(Span.input_tokens), func.sum(Span.output_tokens),
        func.sum(case((Span.status == "ERROR", 1), else_=0)),
        func.max(Span.session_id), func.max(Span.user_ref), func.max(Span.release))
        .group_by(Span.trace_id).order_by(func.min(Span.start_ns).desc()).limit(limit).all())
    out = []
    for tid, st, en, n, cost, it, ot, errs, sess, user, rel in agg:
        root = db.query(Span).filter(Span.project_id == pid, Span.trace_id == tid, Span.parent_span_id.is_(None)).first()
        out.append({"trace_id": tid, "name": root.name if root else "(no root)", "start_ns": int(st),
                    "duration_ms": round((int(en) - int(st)) / 1e6, 3), "span_count": int(n), "cost_usd": round(float(cost or 0), 6),
                    "input_tokens": int(it or 0), "output_tokens": int(ot or 0), "has_error": bool(errs),
                    "session_id": sess, "user_ref": user, "release": rel})
    return out


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str, project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    pid = p.scope_project(project_id)
    spans = db.query(Span).filter(Span.project_id == pid, Span.trace_id == trace_id).order_by(Span.start_ns).all()
    if not spans:
        raise HTTPException(404, "trace not found")
    scores = db.query(Score).filter(Score.project_id == pid, Score.trace_id == trace_id).all()
    return {"trace_id": trace_id, "spans": [_span_out(s) for s in spans],
            "scores": [{"name": s.name, "value": s.value, "source": s.source, "evaluator_version": s.evaluator_version, "comment": s.comment} for s in scores],
            "totals": {"cost_usd": round(sum(s.cost_usd or 0 for s in spans), 6),
                       "input_tokens": sum(s.input_tokens or 0 for s in spans),
                       "output_tokens": sum(s.output_tokens or 0 for s in spans),
                       "duration_ms": round((max(s.end_ns for s in spans) - min(s.start_ns for s in spans)) / 1e6, 3)}}


@router.get("/traces/{trace_id}/export")
def export_trace(trace_id: str, project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("engineer")
    data = get_trace(trace_id, project_id, p, db)
    record(db, p, "trace.export", "trace", trace_id, project_id=p.scope_project(project_id)); db.commit()
    return data


@router.delete("/traces/{trace_id}")
def delete_trace(trace_id: str, project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("project_admin")
    pid = p.scope_project(project_id)
    n = db.query(Span).filter(Span.project_id == pid, Span.trace_id == trace_id).delete()
    if not n:
        raise HTTPException(404, "trace not found")
    record(db, p, "trace.delete", "trace", trace_id, project_id=pid, spans_deleted=n); db.commit()
    return {"deleted_spans": n}


@router.get("/analytics/breakdown")
def breakdown(by: str = "model", project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Reproducible aggregates derived from raw spans: cost/latency/error by model, provider, kind, or release."""
    p.require("viewer")
    pid = p.scope_project(project_id)
    col = {"model": Span.model, "provider": Span.provider, "kind": Span.kind, "release": Span.release}.get(by)
    if col is None:
        raise HTTPException(400, "by must be model|provider|kind|release")
    rows = (db.query(col, func.count(Span.id), func.avg(Span.end_ns - Span.start_ns), func.sum(Span.cost_usd),
                     func.sum(case((Span.status == "ERROR", 1), else_=0)))
            .filter(Span.project_id == pid).group_by(col).all())
    # Postgres returns Decimal for AVG/SUM; normalise to float so JSON and arithmetic behave identically across engines.
    return [{by: k, "spans": int(n), "avg_latency_ms": round(float(lat or 0) / 1e6, 2), "cost_usd": round(float(c or 0), 6),
             "errors": int(e or 0), "error_rate": round(int(e or 0) / int(n), 4) if n else 0} for k, n, lat, c, e in rows]
