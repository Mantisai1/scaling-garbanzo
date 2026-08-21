from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..audit import record
from ..auth import Principal, generate_key, get_principal, hash_key, validate_role
from ..config import settings
from ..db import get_db
from ..models import ApiKey, AuditEvent, Environment, Organization, Project

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class BootstrapIn(BaseModel):
    org_name: str
    admin_name: str = "founder"


@router.post("/bootstrap")
def bootstrap(body: BootstrapIn, x_bootstrap_token: str = Header(), db: Session = Depends(get_db)):
    """One-time: create the first organization and an org_admin key. Refuses if any org exists."""
    if x_bootstrap_token != settings.bootstrap_token:
        raise HTTPException(401, "bad bootstrap token")
    if db.query(Organization).first():
        raise HTTPException(409, "already bootstrapped")
    org = Organization(name=body.org_name); db.add(org); db.flush()
    raw, prefix = generate_key("org_admin")
    key = ApiKey(org_id=org.id, name=body.admin_name, role="org_admin", key_hash=hash_key(raw), prefix=prefix)
    db.add(key); db.flush()
    p = Principal(key.id, key.name, key.role, org.id, None, None)
    record(db, p, "org.bootstrap", "organization", org.id)
    db.commit()
    return {"org_id": org.id, "api_key": raw, "key_id": key.id}


class ProjectIn(BaseModel):
    name: str
    store_payloads: bool = True
    redaction_rules: dict = {}
    environments: list[str] = ["dev", "staging", "prod"]


@router.post("/projects")
def create_project(body: ProjectIn, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("org_admin")
    proj = Project(org_id=p.org_id, name=body.name, store_payloads=body.store_payloads, redaction_rules=body.redaction_rules)
    db.add(proj); db.flush()
    envs = [Environment(project_id=proj.id, name=e) for e in body.environments]
    db.add_all(envs); db.flush()
    record(db, p, "project.create", "project", proj.id, project_id=proj.id, name=body.name)
    db.commit()
    return {"id": proj.id, "environments": [{"id": e.id, "name": e.name} for e in envs]}


@router.get("/projects")
def list_projects(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    q = db.query(Project).filter(Project.org_id == p.org_id)
    if p.project_id:
        q = q.filter(Project.id == p.project_id)
    return [{"id": x.id, "name": x.name, "store_payloads": x.store_payloads,
             "environments": [{"id": e.id, "name": e.name} for e in db.query(Environment).filter_by(project_id=x.id)]}
            for x in q]


class KeyIn(BaseModel):
    name: str
    role: str
    project_id: str | None = None
    environment_id: str | None = None


@router.post("/keys")
def create_key(body: KeyIn, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("project_admin")
    validate_role(body.role)
    if body.role == "org_admin":
        p.require("org_admin")
        project_id = None
    else:
        project_id = p.scope_project(body.project_id)
        if not db.query(Project).filter_by(id=project_id, org_id=p.org_id).first():
            raise HTTPException(404, "project not found")
    raw, prefix = generate_key(body.role)
    key = ApiKey(org_id=p.org_id, project_id=project_id, environment_id=body.environment_id,
                 name=body.name, role=body.role, key_hash=hash_key(raw), prefix=prefix)
    db.add(key); db.flush()
    record(db, p, "key.create", "api_key", key.id, project_id=project_id, role=body.role, name=body.name)
    db.commit()
    return {"id": key.id, "api_key": raw, "prefix": prefix, "role": body.role, "project_id": project_id}


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: str, p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("project_admin")
    key = db.query(ApiKey).filter_by(id=key_id, org_id=p.org_id).first()
    if not key:
        raise HTTPException(404, "key not found")
    if p.project_id and key.project_id != p.project_id:
        raise HTTPException(403, "key belongs to another project")
    key.revoked_at = datetime.now(timezone.utc)
    record(db, p, "key.revoke", "api_key", key.id, project_id=key.project_id)
    db.commit()
    return {"revoked": True}


@router.get("/audit")
def audit_log(project_id: str | None = None, limit: int = 200, entity_id: str | None = None,
              p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    p.require("viewer")
    q = db.query(AuditEvent).filter(AuditEvent.org_id == p.org_id)
    if p.project_id:
        q = q.filter(AuditEvent.project_id == p.project_id)
    elif project_id:
        q = q.filter(AuditEvent.project_id == project_id)
    if entity_id:
        q = q.filter(AuditEvent.entity_id == entity_id)
    rows = q.order_by(AuditEvent.id.desc()).limit(limit).all()
    return [{"id": r.id, "at": r.at.isoformat(), "actor": r.actor_name, "actor_id": r.actor_id, "role": r.actor_role,
             "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id, "detail": r.detail} for r in rows]
