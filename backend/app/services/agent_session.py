"""Heartbeat processing and agent-reported events."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.timeutil import ensure_aware, seconds_between, utcnow
from app.models import Device, User, WorkSession
from app.models.enums import (
    ActivityState,
    AlertType,
    BreakType,
    EventSource,
    EventType,
    PresenceState,
)
from app.services import activity, alerts, attendance, presence
from app.services.policy import EffectivePolicy, get_policy

logger = logging.getLogger(__name__)

# Reject heartbeats whose client clock is absurdly far from ours; trust the
# server clock instead. Prevents a tampered client from rewriting history.
MAX_CLOCK_SKEW = timedelta(minutes=5)


def _sanitise_timestamp(reported: Optional[datetime], now: datetime) -> datetime:
    if reported is None:
        return now
    reported = ensure_aware(reported)
    if abs(reported - now) > MAX_CLOCK_SKEW:
        logger.debug("Ignoring agent timestamp %s (skew beyond limit)", reported)
        return now
    return reported


def touch_device(
    db: Session,
    device: Device,
    *,
    hostname: str | None = None,
    platform: str | None = None,
    os_version: str | None = None,
    agent_version: str | None = None,
    ip_address: str | None = None,
) -> None:
    device.last_seen_at = utcnow()
    if hostname:
        device.hostname = hostname[:160]
    if platform:
        device.platform = platform[:64]
    if os_version:
        device.os_version = os_version[:120]
    if agent_version:
        device.agent_version = agent_version[:32]
    if ip_address:
        device.last_ip = ip_address[:64]


def handle_reconnect(
    db: Session, session: WorkSession, now: datetime, policy: EffectivePolicy
) -> None:
    """The agent is reporting again after a gap the monitor flagged as offline."""
    if not session.is_offline:
        return

    gap_seconds = seconds_between(session.offline_since or session.clock_in_at, now)
    session.is_offline = False
    offline_since = session.offline_since
    session.offline_since = None

    activity.log_event(
        db,
        user_id=session.user_id,
        session_id=session.id,
        device_id=session.device_id,
        event_type=EventType.RECONNECTED,
        occurred_at=now,
        source=EventSource.SERVER,
        payload={
            "offline_seconds": gap_seconds,
            "offline_since": ensure_aware(offline_since).isoformat()
            if offline_since
            else None,
        },
        message=f"Laptop reconnected after {gap_seconds // 60} minutes offline",
    )
    alerts.resolve_open_alerts(
        db,
        subject_id=session.user_id,
        alert_type=AlertType.AGENT_OFFLINE,
        session_id=session.id,
    )
    logger.info("Session %s reconnected after %ss offline", session.id, gap_seconds)


def process_heartbeat(
    db: Session,
    user: User,
    device: Device,
    *,
    idle_seconds: int,
    is_locked: bool = False,
    reported_at: Optional[datetime] = None,
    agent_version: Optional[str] = None,
    hostname: Optional[str] = None,
    platform: Optional[str] = None,
    os_version: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> dict[str, Any]:
    """Record one heartbeat and return the directives the agent should follow.

    The agent reports raw signals only (seconds since last input, lock state).
    Every decision — what state that means, whether to alert — is made here.
    """
    now = utcnow()
    reported_at = _sanitise_timestamp(reported_at, now)
    idle_seconds = max(0, min(int(idle_seconds), 86400))

    touch_device(
        db,
        device,
        hostname=hostname,
        platform=platform,
        os_version=os_version,
        agent_version=agent_version,
        ip_address=ip_address,
    )

    policy = get_policy(db, user)
    session = attendance.get_open_session(db, user.id)

    if session is None:
        db.flush()
        return {
            "state": PresenceState.CLOCKED_OUT.value,
            "clocked_in": False,
            "session_id": None,
            "break": None,
            "config": policy.as_agent_config(),
            "server_time": now.isoformat(),
        }

    handle_reconnect(db, session, now, policy)

    open_break = attendance.get_open_break(db, session.id)
    target = activity.desired_state(
        session,
        open_break=open_break,
        idle_seconds=idle_seconds,
        is_locked=is_locked,
        idle_threshold=policy.idle_threshold_seconds,
        treat_lock_as_idle=policy.treat_lock_as_idle,
    )

    # Backdate an idle run to when input actually stopped, so active time is not
    # over-counted by up to one heartbeat interval.
    effective_from = None
    if target is ActivityState.IDLE and session.current_state is not ActivityState.IDLE:
        effective_from = now - timedelta(seconds=idle_seconds)

    changed = activity.transition(
        db, session, target, at=now, effective_from=effective_from
    )

    if changed and target is ActivityState.IDLE:
        activity.log_event(
            db,
            user_id=user.id,
            session_id=session.id,
            device_id=device.id,
            event_type=EventType.IDLE_START,
            occurred_at=effective_from or now,
            payload={"idle_seconds": idle_seconds},
            message="Keyboard and mouse inactive",
        )
    elif changed and target is ActivityState.ACTIVE and session.current_state is ActivityState.ACTIVE:
        activity.log_event(
            db,
            user_id=user.id,
            session_id=session.id,
            device_id=device.id,
            event_type=EventType.IDLE_END,
            occurred_at=now,
            message="Activity resumed",
        )
        alerts.resolve_open_alerts(
            db,
            subject_id=user.id,
            alert_type=AlertType.IDLE_NO_BREAK,
            session_id=session.id,
        )

    session.last_heartbeat_at = now
    if session.device_id != device.id:
        session.device_id = device.id
    db.flush()

    state, since, detail, _ = presence.compute_state(db, user, session, policy, now)
    totals = activity.live_totals(db, session)

    break_payload = None
    if open_break is not None:
        break_payload = {
            "id": open_break.id,
            "type": open_break.break_type.value,
            "started_at": ensure_aware(open_break.started_at).isoformat(),
            "allowed_seconds": open_break.allowed_seconds,
            "remaining_seconds": attendance.break_remaining_seconds(open_break, now),
        }

    return {
        "state": state.value,
        "detail": detail,
        "clocked_in": True,
        "session_id": session.id,
        "clock_in_at": ensure_aware(session.clock_in_at).isoformat(),
        "state_since": since.isoformat() if since else None,
        "break": break_payload,
        "totals": {
            "active_seconds": totals["active_seconds"],
            "idle_seconds": totals["idle_seconds"] + totals["locked_seconds"],
            "break_seconds": totals["lunch_seconds"] + totals["short_break_seconds"],
            "worked_seconds": max(
                0,
                seconds_between(session.clock_in_at, now)
                - totals["lunch_seconds"]
                - totals["short_break_seconds"]
                - totals["offline_seconds"],
            ),
        },
        "breaks_used": {
            "lunch": attendance.breaks_taken_today(
                db, user.id, session.work_date, BreakType.LUNCH
            ),
            "short": attendance.breaks_taken_today(
                db, user.id, session.work_date, BreakType.SHORT
            ),
        },
        "config": policy.as_agent_config(),
        "server_time": now.isoformat(),
    }


def record_agent_event(
    db: Session,
    user: User,
    device: Device,
    *,
    event_type: EventType,
    occurred_at: Optional[datetime] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Handle a discrete event the agent reports: lock, suspend, shutdown, stop.

    These are advisory. The authoritative signal for "this laptop stopped
    reporting" is always the missing heartbeat, which the agent cannot fake by
    staying silent.
    """
    now = utcnow()
    occurred_at = _sanitise_timestamp(occurred_at, now)
    session = attendance.get_open_session(db, user.id)

    activity.log_event(
        db,
        user_id=user.id,
        session_id=session.id if session else None,
        device_id=device.id,
        event_type=event_type,
        occurred_at=occurred_at,
        source=EventSource.AGENT,
        payload=payload,
        message=_EVENT_MESSAGES.get(event_type),
    )

    if session is None:
        db.flush()
        return

    policy = get_policy(db, user)
    open_break = attendance.get_open_break(db, session.id)

    if event_type is EventType.SCREEN_LOCKED and policy.treat_lock_as_idle:
        if open_break is None:
            activity.transition(db, session, ActivityState.LOCKED, at=occurred_at)

    elif event_type is EventType.SCREEN_UNLOCKED:
        if open_break is None and session.current_state is ActivityState.LOCKED:
            activity.transition(db, session, ActivityState.ACTIVE, at=occurred_at)

    elif event_type in (
        EventType.SYSTEM_SUSPEND,
        EventType.SYSTEM_SHUTDOWN,
        EventType.AGENT_STOPPED,
    ):
        # A clean shutdown notice lets us start the offline run immediately
        # instead of waiting out the heartbeat grace period.
        if not session.is_offline:
            session.is_offline = True
            session.offline_since = occurred_at
            if open_break is None:
                activity.transition(
                    db, session, ActivityState.OFFLINE, at=occurred_at
                )

        if event_type is EventType.AGENT_STOPPED:
            alerts.raise_alert(
                db,
                employee=user,
                alert_type=AlertType.AGENT_TERMINATED,
                dedup_key=f"agent_stopped:{session.id}:{int(occurred_at.timestamp()) // 300}",
                session=session,
                context={
                    "reason": (payload or {}).get("reason", "unknown"),
                    "device": device.hostname,
                },
                triggered_at=occurred_at,
            )

    elif event_type is EventType.SYSTEM_RESUME:
        handle_reconnect(db, session, now, policy)

    db.flush()


_EVENT_MESSAGES = {
    EventType.SCREEN_LOCKED: "Screen locked",
    EventType.SCREEN_UNLOCKED: "Screen unlocked",
    EventType.SYSTEM_SUSPEND: "Laptop went to sleep",
    EventType.SYSTEM_RESUME: "Laptop woke up",
    EventType.SYSTEM_SHUTDOWN: "Laptop shutting down",
    EventType.AGENT_STARTED: "Tracking app started",
    EventType.AGENT_STOPPED: "Tracking app stopped",
}
