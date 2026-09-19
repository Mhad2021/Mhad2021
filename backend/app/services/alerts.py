"""Alert creation, de-duplication and fan-out to notification channels."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.timeutil import ensure_aware, humanize_duration, utcnow
from app.models import Alert, User, WorkSession
from app.models.enums import AlertSeverity, AlertType
from app.services.policy import resolve_alert_recipient

logger = logging.getLogger(__name__)

_DEFAULT_SEVERITY: dict[AlertType, AlertSeverity] = {
    AlertType.IDLE_NO_BREAK: AlertSeverity.WARNING,
    AlertType.BREAK_OVERRUN: AlertSeverity.WARNING,
    AlertType.AGENT_OFFLINE: AlertSeverity.CRITICAL,
    AlertType.AGENT_TERMINATED: AlertSeverity.CRITICAL,
    AlertType.LATE_ARRIVAL: AlertSeverity.INFO,
    AlertType.EARLY_DEPARTURE: AlertSeverity.INFO,
    AlertType.MISSING_CLOCK_OUT: AlertSeverity.INFO,
    AlertType.NO_SHOW: AlertSeverity.WARNING,
}


def build_message(
    alert_type: AlertType, employee: User, context: dict[str, Any]
) -> tuple[str, str]:
    """Return (title, body). Plain language a team leader can act on instantly."""
    name = employee.first_name or employee.full_name

    if alert_type is AlertType.IDLE_NO_BREAK:
        mins = max(1, int(context.get("idle_seconds", 0)) // 60)
        return (
            f"{name} is inactive",
            f"{name} has been inactive for {mins} minutes. "
            f"No break is currently active.",
        )

    if alert_type is AlertType.BREAK_OVERRUN:
        label = (
            "lunch break"
            if context.get("break_type") == "lunch"
            else "10-minute break"
        )
        allowed = humanize_duration(int(context.get("allowed_seconds", 0)))
        over = humanize_duration(int(context.get("overrun_seconds", 0)))
        return (
            f"{name} is over their break",
            f"{name} started a {label} ({allowed} allowed) and has not returned. "
            f"They are {over} over the limit.",
        )

    if alert_type is AlertType.AGENT_OFFLINE:
        mins = max(1, int(context.get("offline_seconds", 0)) // 60)
        return (
            f"{name}'s laptop is offline",
            f"{name} is still clocked in but their laptop stopped reporting "
            f"{mins} minutes ago. The machine may be shut down, asleep or "
            f"disconnected from the network.",
        )

    if alert_type is AlertType.AGENT_TERMINATED:
        return (
            f"{name}'s tracker stopped",
            f"{name} is clocked in but the tracking application reported that it "
            f"was closed or stopped. Activity is no longer being recorded.",
        )

    if alert_type is AlertType.LATE_ARRIVAL:
        mins = max(1, int(context.get("late_by_seconds", 0)) // 60)
        expected = context.get("scheduled_start", "their scheduled start")
        return (
            f"{name} arrived late",
            f"{name} clocked in {mins} minutes after {expected}.",
        )

    if alert_type is AlertType.EARLY_DEPARTURE:
        mins = max(1, int(context.get("early_by_seconds", 0)) // 60)
        return (
            f"{name} left early",
            f"{name} clocked out {mins} minutes before the end of their shift.",
        )

    if alert_type is AlertType.MISSING_CLOCK_OUT:
        hours = int(context.get("open_hours", 0))
        return (
            f"{name} never clocked out",
            f"{name}'s session has been open for {hours} hours and was closed "
            f"automatically. Please review and correct the hours if needed.",
        )

    if alert_type is AlertType.NO_SHOW:
        return (
            f"{name} has not clocked in",
            f"{name} was scheduled to start work and has not clocked in yet.",
        )

    return (f"Alert for {name}", f"An event was recorded for {name}.")


def raise_alert(
    db: Session,
    *,
    employee: User,
    alert_type: AlertType,
    dedup_key: str,
    context: Optional[dict[str, Any]] = None,
    session: Optional[WorkSession] = None,
    severity: Optional[AlertSeverity] = None,
    triggered_at: Optional[datetime] = None,
    recipient: Optional[User] = None,
) -> Optional[Alert]:
    """Create an alert unless one with the same dedup key already exists.

    Returns the new Alert, or None when it was suppressed as a duplicate.
    """
    context = context or {}
    triggered_at = ensure_aware(triggered_at or utcnow())

    if db.scalar(select(Alert.id).where(Alert.dedup_key == dedup_key)) is not None:
        return None

    recipient = recipient or resolve_alert_recipient(db, employee)
    title, body = build_message(alert_type, employee, context)

    alert = Alert(
        subject_id=employee.id,
        recipient_id=recipient.id if recipient else None,
        session_id=session.id if session else None,
        alert_type=alert_type,
        severity=severity or _DEFAULT_SEVERITY.get(alert_type, AlertSeverity.WARNING),
        title=title,
        message=body,
        context={
            **context,
            "employee_name": employee.full_name,
            "employee_code": employee.employee_code,
            "department": employee.department.name if employee.department else None,
        },
        triggered_at=triggered_at,
        dedup_key=dedup_key,
    )
    db.add(alert)

    try:
        db.flush()
    except IntegrityError:
        # Lost a race with another worker on the unique dedup index.
        db.rollback()
        logger.debug("Duplicate alert suppressed: %s", dedup_key)
        return None

    if recipient is None:
        logger.warning(
            "Alert %s for %s has no recipient — assign a team leader.",
            alert_type.value,
            employee.username,
        )

    # Queue deliveries for every matching channel.
    from app.services.notifications.engine import queue_alert

    queue_alert(db, alert, recipient)
    return alert


def resolve_alert(db: Session, alert: Alert, at: Optional[datetime] = None) -> None:
    if alert.resolved_at is None:
        alert.resolved_at = ensure_aware(at or utcnow())


def resolve_open_alerts(
    db: Session, *, subject_id: int, alert_type: AlertType, session_id: int | None = None
) -> int:
    """Close out live alerts once the underlying condition has cleared."""
    stmt = select(Alert).where(
        Alert.subject_id == subject_id,
        Alert.alert_type == alert_type,
        Alert.resolved_at.is_(None),
    )
    if session_id is not None:
        stmt = stmt.where(Alert.session_id == session_id)

    count = 0
    now = utcnow()
    for alert in db.scalars(stmt):
        alert.resolved_at = now
        count += 1
    return count


def acknowledge(
    db: Session, alert: Alert, actor: User, note: str | None = None
) -> Alert:
    alert.acknowledged_at = utcnow()
    alert.acknowledged_by_id = actor.id
    if note:
        alert.acknowledgement_note = note
    return alert


def scope_for(stmt, viewer: User):
    """Narrow an alert query to what this viewer is allowed to see.

    Admins see everything, team leaders see alerts addressed to them (plus any
    raised about them), employees see only their own.
    """
    from app.models.enums import Role

    if viewer.role is Role.ADMIN:
        return stmt
    if viewer.role is Role.TEAM_LEADER:
        return stmt.where(
            (Alert.recipient_id == viewer.id) | (Alert.subject_id == viewer.id)
        )
    return stmt.where(Alert.subject_id == viewer.id)


def query(
    db: Session,
    viewer: User,
    *,
    only_open: bool = True,
    alert_type: Optional[AlertType] = None,
    severity: Optional[AlertSeverity] = None,
    subject_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[Alert]]:
    """Return (total matching, page of alerts) for this viewer."""
    from sqlalchemy import func

    stmt = scope_for(select(Alert), viewer)
    if only_open:
        stmt = stmt.where(Alert.acknowledged_at.is_(None))
    if alert_type is not None:
        stmt = stmt.where(Alert.alert_type == alert_type)
    if severity is not None:
        stmt = stmt.where(Alert.severity == severity)
    if subject_id is not None:
        stmt = stmt.where(Alert.subject_id == subject_id)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        db.scalars(stmt.order_by(Alert.triggered_at.desc()).limit(limit).offset(offset))
    )
    return total, rows


def open_alerts_for(db: Session, recipient: User, limit: int = 50) -> list[Alert]:
    stmt = scope_for(select(Alert).where(Alert.acknowledged_at.is_(None)), recipient)
    return list(db.scalars(stmt.order_by(Alert.triggered_at.desc()).limit(limit)))
