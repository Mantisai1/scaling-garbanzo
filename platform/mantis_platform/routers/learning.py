from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..audit import record
from ..auth import Principal, get_principal
from ..db import get_db
from ..models import (AgentConfig, Annotation, Candidate, Checkpoint, DatasetVersion, Environment, EvalRun, Project,
                      PromotionPolicy, Release, TrainingJob, TrainingSignal)
from ..services.adapters import ADAPTERS

router = APIRouter(prefix="/v1", tags=["learning", "release"])


class TrainIn(BaseModel):
    training_dataset_version_id: str
    base_agent_config_id: str
    adapter: str = "local_stub"
    method: str = "sft"
    params: dict = {}
    project_id: str | None = None


@router.post("/training-jobs")
def launch_training(body: TrainIn, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("engineer")
    pid = p.scope_project(body.project_id)
    dv = db.query(DatasetVersion).filter_by(id=body.training_dataset_version_id, project_id=pid).first()
    base = db.query(AgentConfig).filter_by(id=body.base_agent_config_id, project_id=pid).first()
    if not dv or not base: raise HTTPException(404, "dataset version or base config not found")
    if body.adapter not in ADAPTERS: raise HTTPException(400, f"adapter must be one of {list(ADAPTERS)}")
    project = db.get(Project, pid)
    params = dict(body.params)
    if body.adapter != "local_stub":
        params["_allow_protected"] = project.allow_external_training_of_protected
    job = TrainingJob(project_id=pid, training_dataset_version_id=dv.id, base_agent_config_id=base.id, adapter=body.adapter,
                      method=body.method, params=params, status="running", created_by=p.id)
    db.add(job); db.flush()
    record(db, p, "training_job.launch", "training_job", job.id, project_id=pid, adapter=body.adapter, method=body.method, dataset_version=dv.id, dataset_hash=dv.content_hash)
    out = ADAPTERS[body.adapter].run(job, dv, base)
    job.status, job.log, job.provider_job_ref = out["status"], out.get("log", []), out.get("provider_job_ref")
    resp = {"job_id": job.id, "status": job.status, "log": job.log}
    if out["status"] in ("succeeded", "submitted"):
        ck = Checkpoint(project_id=pid, training_job_id=job.id, artifact_ref=out["artifact_ref"], artifact_hash=out["artifact_hash"])
        db.add(ck); db.flush()
        cfg = AgentConfig(project_id=pid, name=base.name, version=f"{base.version}+{ck.artifact_hash[:8]}", model=out["model"],
                          prompt_version=base.prompt_version, invoke=out["candidate_invoke"], checkpoint_id=ck.id)
        db.add(cfg); db.flush()
        cand = Candidate(project_id=pid, agent_config_id=cfg.id, checkpoint_id=ck.id)
        db.add(cand); db.flush()
        record(db, p, "checkpoint.create", "checkpoint", ck.id, project_id=pid, job_id=job.id, hash=ck.artifact_hash)
        record(db, p, "candidate.register", "candidate", cand.id, project_id=pid, agent_config_id=cfg.id, checkpoint_id=ck.id)
        resp.update({"checkpoint_id": ck.id, "candidate_id": cand.id, "candidate_agent_config_id": cfg.id})
    record(db, p, "training_job.finish", "training_job", job.id, project_id=pid, status=job.status)
    db.commit()
    return resp


@router.get("/training-jobs")
def list_jobs(project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    return [{"id": j.id, "adapter": j.adapter, "method": j.method, "status": j.status, "dataset_version_id": j.training_dataset_version_id,
             "base_agent_config_id": j.base_agent_config_id, "provider_job_ref": j.provider_job_ref, "log": j.log, "created_at": j.created_at.isoformat()}
            for j in db.query(TrainingJob).filter_by(project_id=p.scope_project(project_id)).order_by(TrainingJob.created_at.desc())]


@router.get("/candidates")
def list_candidates(project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    out = []
    for c in db.query(Candidate).filter_by(project_id=p.scope_project(project_id)).order_by(Candidate.created_at.desc()):
        cfg = db.get(AgentConfig, c.agent_config_id)
        out.append({"id": c.id, "status": c.status, "agent_config_id": c.agent_config_id, "agent_version": cfg.version, "model": cfg.model,
                    "checkpoint_id": c.checkpoint_id, "gate_result": c.gate_result, "created_at": c.created_at.isoformat()})
    return out


# ---------------- Promotion policy & gate ----------------

class PolicyIn(BaseModel):
    rules: dict
    project_id: str | None = None


@router.post("/promotion-policies")
def create_policy(body: PolicyIn, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("release_approver", "project_admin")
    pid = p.scope_project(body.project_id)
    if "required_metrics" not in body.rules: raise HTTPException(422, "rules.required_metrics required")
    prev = db.query(PromotionPolicy).filter_by(project_id=pid).order_by(PromotionPolicy.version.desc()).first()
    pol = PromotionPolicy(project_id=pid, version=(prev.version + 1 if prev else 1), rules=body.rules, created_by=p.id)
    db.add(pol); db.flush()
    record(db, p, "promotion_policy.create", "promotion_policy", pol.id, project_id=pid, version=pol.version, rules=body.rules)
    db.commit()
    return {"id": pol.id, "version": pol.version}


@router.get("/promotion-policies")
def list_policies(project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    return [{"id": x.id, "version": x.version, "rules": x.rules} for x in db.query(PromotionPolicy).filter_by(project_id=p.scope_project(project_id)).order_by(PromotionPolicy.version)]


def evaluate_gate(policy: PromotionPolicy, cand_run: EvalRun, base_run: EvalRun | None) -> dict:
    checks, passed = [], True
    for m in policy.rules.get("required_metrics", []):
        v = cand_run.summary.get(m["name"])
        ok = v is not None and v >= m.get("min", 0)
        detail = {"metric": m["name"], "value": v, "min": m.get("min", 0), "ok": ok}
        if "min_delta_vs_baseline" in m:
            if base_run is None:
                ok = False; detail["note"] = "baseline required by policy but not provided"
            else:
                bv = base_run.summary.get(m["name"]) or 0
                detail["baseline"] = bv; detail["delta"] = round((v or 0) - bv, 4)
                ok = ok and detail["delta"] >= m["min_delta_vs_baseline"]
        detail["ok"] = ok; checks.append(detail); passed &= ok
    for c in policy.rules.get("critical_checks", []):
        v = cand_run.summary.get(c)
        ok = v == 1.0
        checks.append({"critical_check": c, "value": v, "ok": ok}); passed &= ok
    return {"passed": passed, "checks": checks, "policy_id": policy.id, "policy_version": policy.version,
            "candidate_eval_run": cand_run.id, "baseline_eval_run": base_run.id if base_run else None}


class GateIn(BaseModel):
    eval_run_id: str
    baseline_eval_run_id: str | None = None
    policy_id: str | None = None  # default: latest
    project_id: str | None = None


@router.post("/candidates/{cand_id}/gate")
def gate(cand_id: str, body: GateIn, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("engineer")
    pid = p.scope_project(body.project_id)
    c = db.query(Candidate).filter_by(id=cand_id, project_id=pid).first()
    if not c: raise HTTPException(404, "candidate not found")
    run = db.query(EvalRun).filter_by(id=body.eval_run_id, project_id=pid).first()
    if not run: raise HTTPException(404, "eval run not found")
    if run.agent_config_id != c.agent_config_id:
        raise HTTPException(400, "eval run was not produced by this candidate's agent config")
    base = db.query(EvalRun).filter_by(id=body.baseline_eval_run_id, project_id=pid).first() if body.baseline_eval_run_id else None
    if base and base.dataset_version_id != run.dataset_version_id:
        raise HTTPException(400, "baseline must use the same dataset version")
    pol = (db.query(PromotionPolicy).filter_by(id=body.policy_id, project_id=pid).first() if body.policy_id
           else db.query(PromotionPolicy).filter_by(project_id=pid).order_by(PromotionPolicy.version.desc()).first())
    if not pol: raise HTTPException(400, "no promotion policy defined for project")
    res = evaluate_gate(pol, run, base)
    c.gate_result = res; c.status = "gated_pass" if res["passed"] else "gated_fail"
    record(db, p, "candidate.gate", "candidate", c.id, project_id=pid, passed=res["passed"], policy_version=pol.version, eval_run=run.id)
    db.commit()
    return res


class PromoteIn(BaseModel):
    environment_id: str
    project_id: str | None = None


@router.post("/candidates/{cand_id}/promote")
def promote(cand_id: str, body: PromoteIn, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Human approval step. Only a release_approver can promote, and only a candidate whose gate passed."""
    pid = p.scope_project(body.project_id)
    c = db.query(Candidate).filter_by(id=cand_id, project_id=pid).first()
    if not c: raise HTTPException(404, "candidate not found")
    pol = db.get(PromotionPolicy, c.gate_result.get("policy_id")) if c.gate_result else None
    p.require(pol.rules.get("required_approver_role", "release_approver") if pol else "release_approver")
    if c.status != "gated_pass":
        record(db, p, "candidate.promote_blocked", "candidate", c.id, project_id=pid, status=c.status); db.commit()
        raise HTTPException(409, f"candidate status is '{c.status}'; only 'gated_pass' candidates can be promoted")
    env = db.query(Environment).filter_by(id=body.environment_id, project_id=pid).first()
    if not env: raise HTTPException(404, "environment not found")
    current = db.query(Release).filter_by(project_id=pid, environment_id=env.id, active=True).first()
    if current: current.active = False
    rel = Release(project_id=pid, environment_id=env.id, candidate_id=c.id, policy_id=c.gate_result["policy_id"],
                  eval_run_id=c.gate_result["candidate_eval_run"], baseline_eval_run_id=c.gate_result.get("baseline_eval_run"),
                  approved_by=p.id, rollback_target_id=current.id if current else None)
    c.status = "promoted"
    db.add(rel); db.flush()
    record(db, p, "release.promote", "release", rel.id, project_id=pid, candidate_id=c.id, environment=env.name, rollback_target=rel.rollback_target_id)
    db.commit()
    return {"release_id": rel.id, "environment": env.name, "rollback_target_id": rel.rollback_target_id}


@router.post("/releases/{release_id}/rollback")
def rollback(release_id: str, project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("release_approver")
    pid = p.scope_project(project_id)
    rel = db.query(Release).filter_by(id=release_id, project_id=pid, active=True).first()
    if not rel: raise HTTPException(404, "active release not found")
    rel.active = False
    target = db.get(Release, rel.rollback_target_id) if rel.rollback_target_id else None
    if target: target.active = True
    record(db, p, "release.rollback", "release", rel.id, project_id=pid, restored_release=target.id if target else None)
    db.commit()
    return {"rolled_back": rel.id, "active_release": target.id if target else None}


@router.get("/releases")
def list_releases(project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    pid = p.scope_project(project_id)
    out = []
    for r in db.query(Release).filter_by(project_id=pid).order_by(Release.created_at.desc()):
        env = db.get(Environment, r.environment_id); c = db.get(Candidate, r.candidate_id); cfg = db.get(AgentConfig, c.agent_config_id)
        out.append({"id": r.id, "environment": env.name, "active": r.active, "candidate_id": r.candidate_id, "agent_version": cfg.version, "model": cfg.model,
                    "policy_id": r.policy_id, "eval_run_id": r.eval_run_id, "baseline_eval_run_id": r.baseline_eval_run_id,
                    "approved_by": r.approved_by, "rollback_target_id": r.rollback_target_id, "created_at": r.created_at.isoformat()})
    return out


@router.get("/releases/{release_id}/lineage")
def lineage(release_id: str, project_id: str | None = None, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Walk Release -> Candidate -> Checkpoint -> Training job -> Training dataset -> Signals -> Annotations -> Traces."""
    p.require("viewer")
    pid = p.scope_project(project_id)
    r = db.query(Release).filter_by(id=release_id, project_id=pid).first()
    if not r: raise HTTPException(404, "release not found")
    c = db.get(Candidate, r.candidate_id); ck = db.get(Checkpoint, c.checkpoint_id) if c.checkpoint_id else None
    job = db.get(TrainingJob, ck.training_job_id) if ck else None
    dv = db.get(DatasetVersion, job.training_dataset_version_id) if job else None
    sig_ids = [it["id"] for it in dv.items] if dv else []
    sigs = db.query(TrainingSignal).filter(TrainingSignal.id.in_(sig_ids)).all() if sig_ids else []
    anns = db.query(Annotation).filter(Annotation.id.in_([s.annotation_id for s in sigs])).all() if sigs else []
    return {"release": {"id": r.id, "approved_by": r.approved_by, "policy_id": r.policy_id, "eval_run_id": r.eval_run_id, "baseline_eval_run_id": r.baseline_eval_run_id},
            "candidate": {"id": c.id, "gate_result": c.gate_result},
            "checkpoint": {"id": ck.id, "hash": ck.artifact_hash, "artifact_ref": ck.artifact_ref} if ck else None,
            "training_job": {"id": job.id, "adapter": job.adapter, "method": job.method, "provider_job_ref": job.provider_job_ref} if job else None,
            "training_dataset_version": {"id": dv.id, "version": dv.version, "hash": dv.content_hash, "records": dv.item_count} if dv else None,
            "training_signals": [{"id": s.id, "type": s.signal_type, "approved_by": s.approved_by, "source_trace_id": s.source_trace_id} for s in sigs],
            "annotations": [{"id": a.id, "reviewer_id": a.reviewer_id, "approver_id": a.approver_id, "trace_id": a.trace_id} for a in anns],
            "source_trace_ids": sorted({a.trace_id for a in anns if a.trace_id})}
