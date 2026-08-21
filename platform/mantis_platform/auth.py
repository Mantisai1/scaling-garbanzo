"""API-key authentication, role enforcement, and tenant scoping."""
import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .db import get_db
from .models import ApiKey, ROLES

ROLE_RANK = {
    "viewer": 0, "ingest": 0, "reviewer": 1, "engineer": 2,
    "data_approver": 3, "release_approver": 3, "project_admin": 4, "org_admin": 5,
}


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_key(role: str) -> tuple[str, str]:
    raw = f"mk_{role[:3]}_{secrets.token_urlsafe(32)}"
    return raw, raw[:12]


@dataclass
class Principal:
    id: str
    name: str
    role: str
    org_id: str
    project_id: str | None
    environment_id: str | None

    def require(self, *roles: str):
        """Allow if the principal has one of the named roles, or outranks all of them (admins)."""
        if self.role in roles:
            return
        needed = min(ROLE_RANK[r] for r in roles)
        if self.role in ("org_admin", "project_admin") and ROLE_RANK[self.role] >= needed:
            return  # admins can do anything at or below their rank
        # Approver roles (rank 3) are separation-of-duty roles: only exact match or admins satisfy them.
        # Everything below (viewer/reviewer/engineer) is satisfied by any higher-ranked non-ingest role.
        if self.role != "ingest" and needed <= ROLE_RANK["engineer"] and ROLE_RANK[self.role] >= needed:
            return
        raise HTTPException(403, f"role '{self.role}' cannot perform this action; needs one of {roles}")

    def scope_project(self, project_id: str | None = None) -> str:
        """Resolve the project this request operates on. Project-scoped keys cannot escape their project."""
        if self.project_id:
            if project_id and project_id != self.project_id:
                raise HTTPException(403, "key is scoped to a different project")
            return self.project_id
        if not project_id:
            raise HTTPException(400, "project_id required for org-scoped keys")
        return project_id


def get_principal(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Principal:
    raw = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None)
    if not raw:
        raise HTTPException(401, "missing API key")
    key = db.query(ApiKey).filter(ApiKey.key_hash == hash_key(raw), ApiKey.revoked_at.is_(None)).first()
    if not key:
        raise HTTPException(401, "invalid or revoked API key")
    return Principal(key.id, key.name, key.role, key.org_id, key.project_id, key.environment_id)


def validate_role(role: str):
    if role not in ROLES:
        raise HTTPException(400, f"unknown role {role}; valid: {ROLES}")
