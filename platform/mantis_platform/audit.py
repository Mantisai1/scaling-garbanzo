from sqlalchemy.orm import Session

from .auth import Principal
from .models import AuditEvent


def record(db: Session, p: Principal, action: str, entity_type: str, entity_id: str,
           project_id: str | None = None, **detail):
    """Append an audit event. Called by every mutating endpoint. Never updated or deleted."""
    db.add(AuditEvent(
        org_id=p.org_id, project_id=project_id or p.project_id,
        actor_id=p.id, actor_name=p.name, actor_role=p.role,
        action=action, entity_type=entity_type, entity_id=str(entity_id), detail=detail,
    ))
