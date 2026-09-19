"""Audit log writer."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.models import AuditLog, User

# Never copy these into the audit log, even if they appear on a payload.
_SENSITIVE = {"password", "password_hash", "token", "token_hash", "secret", "code"}


def _scrub(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not data:
        return None
    return {
        k: ("••••" if any(s in k.lower() for s in _SENSITIVE) else v)
        for k, v in data.items()
    }


def record(
    db: Session,
    *,
    actor: Optional[User],
    action: str,
    entity_type: str,
    entity_id: Optional[str | int] = None,
    summary: Optional[str] = None,
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor.id if actor else None,
        actor_label=(
            f"{actor.full_name} ({actor.username})" if actor else "system"
        ),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        summary=summary,
        before=_scrub(before),
        after=_scrub(after),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:255] or None,
        occurred_at=utcnow(),
    )
    db.add(entry)
    return entry


def model_snapshot(obj: Any, fields: list[str]) -> dict[str, Any]:
    """Pull a comparable dict off a model for before/after diffing."""
    snapshot: dict[str, Any] = {}
    for name in fields:
        value = getattr(obj, name, None)
        if hasattr(value, "value"):          # enum
            value = value.value
        elif hasattr(value, "isoformat"):    # date / datetime / time
            value = value.isoformat()
        snapshot[name] = value
    return snapshot


def query(
    db: Session,
    *,
    entity_type: str | None = None,
    actor_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[AuditLog]:
    stmt = select(AuditLog)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    return list(
        db.scalars(
            stmt.order_by(AuditLog.occurred_at.desc()).limit(limit).offset(offset)
        )
    )


def recent(db: Session, limit: int = 100, entity_type: str | None = None):
    return query(db, entity_type=entity_type, limit=limit)
