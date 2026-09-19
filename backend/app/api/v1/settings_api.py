"""Admin settings: monitoring policy, notification channels, audit log."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from app.api.deps import AdminUser, DbSession, ManagerUser, client_ip
from app.core.crypto import encrypt_config, decrypt_config, redact
from app.core.timeutil import to_local
from app.models import AuditLog, Department, NotificationChannel, PolicySettings
from app.models.enums import AlertType, ChannelType
from app.schemas.admin import (
    ChannelCreate,
    ChannelOut,
    ChannelUpdate,
    PolicyOut,
    PolicyUpdate,
)
from app.schemas.common import ActionResult
from app.services import audit
from app.services.notifications import engine as notification_engine
from app.services.policy import ensure_global_policy, get_policy_for_department
from app.services.notifications.providers import build_provider

logger = logging.getLogger(__name__)
router = APIRouter(tags=["settings"])

_POLICY_FIELDS = list(PolicyUpdate.model_fields.keys())

# Config keys that must be encrypted rather than stored in the clear.
_SECRET_KEYS = {
    "webhook_url", "auth_token", "password", "api_key", "account_sid", "token",
}


# --------------------------------------------------------------------------- #
# Monitoring policy
# --------------------------------------------------------------------------- #
@router.get("/settings/policy", response_model=PolicyOut)
def get_policy_settings(
    viewer: ManagerUser, db: DbSession, department_id: Optional[int] = None
) -> PolicyOut:
    effective = get_policy_for_department(db, department_id)
    return PolicyOut(department_id=department_id, **effective.__dict__)


@router.patch("/settings/policy", response_model=PolicyOut)
def update_policy_settings(
    payload: PolicyUpdate,
    admin: AdminUser,
    request: Request,
    db: DbSession,
    department_id: Optional[int] = Query(default=None),
) -> PolicyOut:
    """Change inactivity limits, break durations and heartbeat timing.

    Omit ``department_id`` to edit the global policy; pass one to create or
    update an override for that department.
    """
    if department_id is None:
        row = ensure_global_policy(db)
        scope = "global"
    else:
        dept = db.get(Department, department_id)
        if dept is None:
            raise HTTPException(status_code=404, detail="Department not found")
        row = db.scalar(
            select(PolicySettings).where(PolicySettings.department_id == department_id)
        )
        if row is None:
            row = PolicySettings(department_id=department_id)
            db.add(row)
            db.flush()
        scope = dept.name

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No settings supplied")

    # Guard the one combination that would break offline detection outright.
    new_interval = changes.get("heartbeat_interval_seconds", row.heartbeat_interval_seconds)
    new_grace = changes.get("heartbeat_grace_seconds", row.heartbeat_grace_seconds)
    if new_grace <= new_interval * 2:
        raise HTTPException(
            status_code=422,
            detail=(
                "Heartbeat grace must be more than twice the heartbeat interval, "
                "otherwise a single dropped packet is reported as an offline laptop."
            ),
        )

    before = audit.model_snapshot(row, _POLICY_FIELDS)
    for field, value in changes.items():
        setattr(row, field, value)
    db.flush()

    audit.record(
        db,
        actor=admin,
        action="update_policy",
        entity_type="policy_settings",
        entity_id=row.id,
        summary=f"Updated {scope} monitoring policy: {', '.join(changes)}",
        before=before,
        after=audit.model_snapshot(row, _POLICY_FIELDS),
        ip_address=client_ip(request),
    )

    effective = get_policy_for_department(db, department_id)
    return PolicyOut(department_id=department_id, **effective.__dict__)


# --------------------------------------------------------------------------- #
# Notification channels
# --------------------------------------------------------------------------- #
def _split_config(config: dict) -> tuple[dict, dict]:
    """Separate secrets (encrypted) from display-safe values."""
    secret = {k: v for k, v in config.items() if k in _SECRET_KEYS}
    public = {k: v for k, v in config.items() if k not in _SECRET_KEYS}
    return secret, public


def _to_channel_out(channel: NotificationChannel) -> ChannelOut:
    merged = {**decrypt_config(channel.encrypted_config), **(channel.public_config or {})}
    return ChannelOut(
        id=channel.id,
        name=channel.name,
        channel_type=channel.channel_type.value,
        min_severity=channel.min_severity.value,
        alert_types=channel.alert_types,
        department_id=channel.department_id,
        is_enabled=channel.is_enabled,
        config_preview=redact(merged),
        last_success_at=channel.last_success_at,
        last_error_at=channel.last_error_at,
        last_error=channel.last_error,
    )


@router.get("/settings/channels", response_model=list[ChannelOut])
def list_channels(admin: AdminUser, db: DbSession) -> list[ChannelOut]:
    return [
        _to_channel_out(c)
        for c in db.scalars(select(NotificationChannel).order_by(NotificationChannel.name))
    ]


@router.get("/settings/channels/types")
def channel_types(admin: AdminUser) -> dict:
    """What each integration needs, so the UI can render the right form."""
    return {
        "types": [
            {
                "type": ChannelType.SLACK.value,
                "label": "Slack",
                "fields": [
                    {"key": "webhook_url", "label": "Incoming webhook URL",
                     "secret": True, "required": True,
                     "help": "Slack → Apps → Incoming Webhooks → Add to workspace"},
                ],
            },
            {
                "type": ChannelType.TEAMS.value,
                "label": "Microsoft Teams",
                "fields": [
                    {"key": "webhook_url", "label": "Incoming webhook URL",
                     "secret": True, "required": True,
                     "help": "Teams channel → Connectors → Incoming Webhook"},
                ],
            },
            {
                "type": ChannelType.EMAIL.value,
                "label": "Email (SMTP)",
                "fields": [
                    {"key": "host", "label": "SMTP host", "required": False,
                     "help": "Leave blank to use the server's global SMTP settings"},
                    {"key": "port", "label": "SMTP port", "required": False},
                    {"key": "username", "label": "SMTP username", "required": False},
                    {"key": "password", "label": "SMTP password", "secret": True,
                     "required": False},
                    {"key": "from_address", "label": "From address", "required": False},
                    {"key": "to_address", "label": "Fallback recipient", "required": False,
                     "help": "Used when the team leader has no email on file"},
                ],
            },
            {
                "type": ChannelType.WHATSAPP.value,
                "label": "WhatsApp (via Twilio)",
                "fields": [
                    {"key": "account_sid", "label": "Twilio Account SID",
                     "secret": True, "required": True},
                    {"key": "auth_token", "label": "Twilio Auth Token",
                     "secret": True, "required": True},
                    {"key": "from_number", "label": "WhatsApp sender number",
                     "required": True, "help": "In E.164 format, e.g. +14155238886"},
                ],
            },
            {
                "type": ChannelType.WEBHOOK.value,
                "label": "Generic webhook",
                "fields": [
                    {"key": "url", "label": "POST URL", "required": True},
                    {"key": "auth_token", "label": "Bearer token", "secret": True,
                     "required": False},
                ],
            },
        ],
        "alert_types": [
            {"value": t.value, "label": t.value.replace("_", " ").title()}
            for t in AlertType
        ],
    }


@router.post("/settings/channels", response_model=ChannelOut, status_code=201)
def create_channel(
    payload: ChannelCreate, admin: AdminUser, request: Request, db: DbSession
) -> ChannelOut:
    if db.scalar(
        select(NotificationChannel.id).where(NotificationChannel.name == payload.name)
    ):
        raise HTTPException(status_code=409, detail="A channel with that name exists")

    provider = build_provider(payload.channel_type, payload.config)
    problems = provider.validate()
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))

    secret, public = _split_config(payload.config)
    channel = NotificationChannel(
        name=payload.name,
        channel_type=payload.channel_type,
        encrypted_config=encrypt_config(secret) if secret else None,
        public_config=public,
        min_severity=payload.min_severity,
        alert_types=payload.alert_types,
        department_id=payload.department_id,
        is_enabled=payload.is_enabled,
    )
    db.add(channel)
    db.flush()

    audit.record(
        db,
        actor=admin,
        action="create_channel",
        entity_type="notification_channel",
        entity_id=channel.id,
        summary=f"Added {payload.channel_type.value} channel '{payload.name}'",
        after={"type": payload.channel_type.value, "name": payload.name},
        ip_address=client_ip(request),
    )
    return _to_channel_out(channel)


@router.patch("/settings/channels/{channel_id}", response_model=ChannelOut)
def update_channel(
    channel_id: int,
    payload: ChannelUpdate,
    admin: AdminUser,
    request: Request,
    db: DbSession,
) -> ChannelOut:
    channel = db.get(NotificationChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    changes = payload.model_dump(exclude_unset=True)
    if "config" in changes and changes["config"] is not None:
        merged = {
            **decrypt_config(channel.encrypted_config),
            **(channel.public_config or {}),
            **changes.pop("config"),
        }
        provider = build_provider(channel.channel_type, merged)
        problems = provider.validate()
        if problems:
            raise HTTPException(status_code=422, detail="; ".join(problems))
        secret, public = _split_config(merged)
        channel.encrypted_config = encrypt_config(secret) if secret else None
        channel.public_config = public

    for field, value in changes.items():
        setattr(channel, field, value)
    db.flush()

    audit.record(
        db,
        actor=admin,
        action="update_channel",
        entity_type="notification_channel",
        entity_id=channel.id,
        summary=f"Updated channel '{channel.name}'",
        ip_address=client_ip(request),
    )
    return _to_channel_out(channel)


@router.post("/settings/channels/{channel_id}/test", response_model=ActionResult)
def test_channel(
    channel_id: int, admin: AdminUser, request: Request, db: DbSession
) -> ActionResult:
    channel = db.get(NotificationChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    try:
        notification_engine.send_test(db, channel, admin)
    except Exception as exc:  # noqa: BLE001 - surface the provider's own message
        channel.last_error = f"{type(exc).__name__}: {exc}"[:800]
        from app.core.timeutil import utcnow

        channel.last_error_at = utcnow()
        raise HTTPException(
            status_code=502, detail=f"Test failed: {exc}"
        ) from exc

    audit.record(
        db,
        actor=admin,
        action="test_channel",
        entity_type="notification_channel",
        entity_id=channel.id,
        summary=f"Sent a test notification via '{channel.name}'",
        ip_address=client_ip(request),
    )
    return ActionResult(detail=f"Test notification sent via {channel.name}")


@router.delete("/settings/channels/{channel_id}", response_model=ActionResult)
def delete_channel(
    channel_id: int, admin: AdminUser, request: Request, db: DbSession
) -> ActionResult:
    channel = db.get(NotificationChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    name = channel.name
    db.delete(channel)
    audit.record(
        db,
        actor=admin,
        action="delete_channel",
        entity_type="notification_channel",
        entity_id=channel_id,
        summary=f"Removed channel '{name}'",
        ip_address=client_ip(request),
    )
    return ActionResult(detail=f"Channel '{name}' removed")


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
@router.get("/settings/audit")
def audit_log(
    admin: AdminUser,
    db: DbSession,
    entity_type: Optional[str] = None,
    actor_id: Optional[int] = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows = audit.query(
        db, entity_type=entity_type, actor_id=actor_id, limit=limit, offset=offset
    )
    return {
        "entries": [
            {
                "id": r.id,
                "actor": r.actor_label,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "summary": r.summary,
                "before": r.before,
                "after": r.after,
                "ip_address": r.ip_address,
                "at": to_local(r.occurred_at, admin.timezone).strftime("%d %b %Y, %H:%M:%S"),
            }
            for r in rows
        ]
    }
