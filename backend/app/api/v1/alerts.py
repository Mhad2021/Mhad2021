"""Alert inbox and acknowledgement."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, ManagerUser, client_ip
from app.core.timeutil import to_local
from app.models import Alert, NotificationDelivery
from app.models.enums import AlertSeverity, AlertType, Role
from app.schemas.common import ActionResult
from app.services import alerts as alert_service
from app.services import audit

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AcknowledgeRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=1000)


def _serialise(alert: Alert, tz: str, db) -> dict:
    deliveries = db.scalars(
        select(NotificationDelivery).where(NotificationDelivery.alert_id == alert.id)
    )
    return {
        "id": alert.id,
        "type": alert.alert_type.value,
        "severity": alert.severity.value,
        "title": alert.title,
        "message": alert.message,
        "subject_id": alert.subject_id,
        "subject_name": alert.subject.full_name if alert.subject else None,
        "recipient_id": alert.recipient_id,
        "recipient_name": alert.recipient.full_name if alert.recipient else None,
        "session_id": alert.session_id,
        "triggered_at": to_local(alert.triggered_at, tz).isoformat(),
        "triggered_display": to_local(alert.triggered_at, tz).strftime("%d %b, %H:%M"),
        "acknowledged_at": (
            to_local(alert.acknowledged_at, tz).isoformat()
            if alert.acknowledged_at
            else None
        ),
        "acknowledged_by": (
            alert.acknowledged_by_id if alert.acknowledged_by_id else None
        ),
        "resolved_at": (
            to_local(alert.resolved_at, tz).isoformat() if alert.resolved_at else None
        ),
        "is_open": alert.is_open,
        "context": alert.context or {},
        "deliveries": [
            {
                "channel": d.channel_type.value,
                "status": d.status.value,
                "attempts": d.attempts,
                "error": d.last_error,
            }
            for d in deliveries
        ],
    }


@router.get("")
def list_alerts(
    viewer: CurrentUser,
    db: DbSession,
    only_open: bool = True,
    alert_type: Optional[AlertType] = None,
    severity: Optional[AlertSeverity] = None,
    subject_id: Optional[int] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    total, rows = alert_service.query(
        db,
        viewer,
        only_open=only_open,
        alert_type=alert_type,
        severity=severity,
        subject_id=subject_id,
        limit=limit,
        offset=offset,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "alerts": [_serialise(a, viewer.timezone, db) for a in rows],
    }


@router.get("/summary")
def alert_summary(viewer: ManagerUser, db: DbSession) -> dict:
    """Unacknowledged counts by type, for the dashboard header."""
    stmt = alert_service.scope_for(
        select(Alert.alert_type, func.count(Alert.id)).where(
            Alert.acknowledged_at.is_(None)
        ),
        viewer,
    ).group_by(Alert.alert_type)

    counts = {row[0].value: row[1] for row in db.execute(stmt).all()}
    return {"total": sum(counts.values()), "by_type": counts}


@router.get("/{alert_id}")
def get_alert(alert_id: int, viewer: CurrentUser, db: DbSession) -> dict:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")

    permitted = (
        viewer.role is Role.ADMIN
        or alert.recipient_id == viewer.id
        or alert.subject_id == viewer.id
    )
    if not permitted:
        raise HTTPException(status_code=403, detail="No access to this alert")

    return _serialise(alert, viewer.timezone, db)


@router.post("/{alert_id}/acknowledge", response_model=ActionResult)
def acknowledge(
    alert_id: int,
    payload: AcknowledgeRequest,
    viewer: ManagerUser,
    request: Request,
    db: DbSession,
) -> ActionResult:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    if viewer.role is not Role.ADMIN and alert.recipient_id != viewer.id:
        raise HTTPException(status_code=403, detail="This alert is not addressed to you")
    if alert.acknowledged_at is not None:
        return ActionResult(ok=True, detail="Already acknowledged")

    alert_service.acknowledge(db, alert, viewer, payload.note)
    audit.record(
        db,
        actor=viewer,
        action="acknowledge_alert",
        entity_type="alert",
        entity_id=alert.id,
        summary=f"Acknowledged: {alert.title}",
        ip_address=client_ip(request),
    )
    return ActionResult(detail="Alert acknowledged")


@router.post("/acknowledge-all", response_model=ActionResult)
def acknowledge_all(
    viewer: ManagerUser, request: Request, db: DbSession
) -> ActionResult:
    stmt = alert_service.scope_for(
        select(Alert).where(Alert.acknowledged_at.is_(None)), viewer
    )
    count = 0
    for alert in db.scalars(stmt):
        if viewer.role is not Role.ADMIN and alert.recipient_id != viewer.id:
            continue
        alert_service.acknowledge(db, alert, viewer)
        count += 1

    audit.record(
        db,
        actor=viewer,
        action="acknowledge_all_alerts",
        entity_type="alert",
        summary=f"Acknowledged {count} alerts",
        ip_address=client_ip(request),
    )
    return ActionResult(detail=f"Acknowledged {count} alerts")
