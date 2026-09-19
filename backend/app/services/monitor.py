"""Background monitoring.

Runs every ``MONITOR_INTERVAL_SECONDS`` and is the only place alerts about
*absence of signal* are raised. The desktop agent cannot suppress any of this
by being closed — that is precisely what a missing heartbeat looks like here.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.timeutil import ensure_aware, seconds_between, utcnow
from app.models import User, WorkSession
from app.models.enums import (
    ActivityState,
    AlertType,
    BreakEndReason,
    ClockOutReason,
    EventSource,
    EventType,
)
from app.services import activity, alerts, attendance
from app.services.policy import EffectivePolicy, get_policy_for_department

logger = logging.getLogger(__name__)


def _open_sessions(db: Session) -> list[WorkSession]:
    return list(
        db.scalars(
            select(WorkSession)
            .options(
                selectinload(WorkSession.user).selectinload(User.department),
                selectinload(WorkSession.user).selectinload(User.team_leader),
            )
            .where(WorkSession.clock_out_at.is_(None))
        )
    )


def _policy_for(
    db: Session, user: User, cache: dict[Optional[int], EffectivePolicy]
) -> EffectivePolicy:
    if user.department_id not in cache:
        cache[user.department_id] = get_policy_for_department(db, user.department_id)
    return cache[user.department_id]


# --------------------------------------------------------------------------- #
# 1. Missing heartbeat → laptop off, network lost, or agent killed
# --------------------------------------------------------------------------- #
def detect_offline(
    db: Session, session: WorkSession, policy: EffectivePolicy, now: datetime
) -> None:
    last_beat = ensure_aware(session.last_heartbeat_at or session.clock_in_at)
    age = seconds_between(last_beat, now)

    if age <= policy.heartbeat_grace_seconds:
        return

    if not session.is_offline:
        # Backdate the offline run to the last confirmed heartbeat so the gap is
        # not counted as worked time.
        session.is_offline = True
        session.offline_since = last_beat

        open_break = attendance.get_open_break(db, session.id)
        if open_break is None:
            activity.transition(
                db, session, ActivityState.OFFLINE, at=now, effective_from=last_beat
            )

        activity.log_event(
            db,
            user_id=session.user_id,
            session_id=session.id,
            device_id=session.device_id,
            event_type=EventType.HEARTBEAT_LOST,
            occurred_at=last_beat,
            source=EventSource.SERVER,
            payload={"grace_seconds": policy.heartbeat_grace_seconds},
            message=(
                "Heartbeat stopped — laptop shut down, asleep, disconnected, or "
                "the tracking app was terminated"
            ),
        )
        logger.info(
            "Session %s (user %s) went offline at %s",
            session.id,
            session.user_id,
            last_beat.isoformat(),
        )

    offline_for = seconds_between(session.offline_since or last_beat, now)
    if offline_for >= policy.offline_alert_after_seconds:
        alerts.raise_alert(
            db,
            employee=session.user,
            alert_type=AlertType.AGENT_OFFLINE,
            dedup_key=f"offline:{session.id}:{int(ensure_aware(session.offline_since).timestamp())}",
            session=session,
            context={
                "offline_seconds": offline_for,
                "offline_since": ensure_aware(session.offline_since).isoformat(),
                "last_heartbeat": last_beat.isoformat(),
            },
            triggered_at=now,
        )


# --------------------------------------------------------------------------- #
# 2. Idle past the threshold with no approved break
# --------------------------------------------------------------------------- #
def check_idle(
    db: Session, session: WorkSession, policy: EffectivePolicy, now: datetime
) -> None:
    if session.is_offline:
        return
    if session.current_state not in (ActivityState.IDLE, ActivityState.LOCKED):
        # Condition cleared — close out any live idle alerts for this session.
        alerts.resolve_open_alerts(
            db,
            subject_id=session.user_id,
            alert_type=AlertType.IDLE_NO_BREAK,
            session_id=session.id,
        )
        return

    # A running break is explicit permission to be away. Never alert during one.
    if attendance.get_open_break(db, session.id) is not None:
        return

    since = ensure_aware(session.state_since or session.clock_in_at)
    idle_for = seconds_between(since, now)
    if idle_for < policy.idle_threshold_seconds:
        return

    # Re-alert periodically while the employee stays away, without spamming on
    # every 30-second tick: one alert per re-alert window.
    window = max(policy.idle_realert_seconds, 60)
    bucket = (idle_for - policy.idle_threshold_seconds) // window

    alerts.raise_alert(
        db,
        employee=session.user,
        alert_type=AlertType.IDLE_NO_BREAK,
        dedup_key=f"idle:{session.id}:{int(since.timestamp())}:{bucket}",
        session=session,
        context={
            "idle_seconds": idle_for,
            "idle_since": since.isoformat(),
            "threshold_seconds": policy.idle_threshold_seconds,
            "locked": session.current_state is ActivityState.LOCKED,
            "repeat": int(bucket),
        },
        triggered_at=now,
    )


# --------------------------------------------------------------------------- #
# 3. Break ran past its allowance
# --------------------------------------------------------------------------- #
def check_break_overrun(
    db: Session, session: WorkSession, policy: EffectivePolicy, now: datetime
) -> None:
    period = attendance.get_open_break(db, session.id)
    if period is None:
        return

    elapsed = seconds_between(period.started_at, now)
    limit = period.allowed_seconds + policy.break_overrun_grace_seconds
    if elapsed < limit:
        return

    overrun = elapsed - period.allowed_seconds

    if not period.overrun_alert_sent:
        period.overrun_alert_sent = True
        activity.log_event(
            db,
            user_id=session.user_id,
            session_id=session.id,
            event_type=EventType.BREAK_OVERRUN,
            occurred_at=now,
            source=EventSource.SERVER,
            payload={
                "break_type": period.break_type.value,
                "allowed_seconds": period.allowed_seconds,
                "overrun_seconds": overrun,
            },
            message=f"Break exceeded its {period.allowed_seconds // 60} minute allowance",
        )
        alerts.raise_alert(
            db,
            employee=session.user,
            alert_type=AlertType.BREAK_OVERRUN,
            dedup_key=f"break_overrun:{period.id}",
            session=session,
            context={
                "break_type": period.break_type.value,
                "allowed_seconds": period.allowed_seconds,
                "overrun_seconds": overrun,
                "started_at": ensure_aware(period.started_at).isoformat(),
            },
            triggered_at=now,
        )

    if policy.auto_end_expired_breaks:
        attendance.end_break(
            db,
            session,
            period,
            at=now,
            reason=BreakEndReason.AUTO_EXPIRED,
            transition_after=False,
            source=EventSource.SERVER,
        )
        # They did not press "End Break", so assume still away; the next
        # heartbeat flips this back to ACTIVE if they are in fact at the desk.
        activity.transition(db, session, ActivityState.IDLE, at=now)


# --------------------------------------------------------------------------- #
# 4. Sessions nobody ever closed
# --------------------------------------------------------------------------- #
def check_forgotten_session(
    db: Session, session: WorkSession, policy: EffectivePolicy, now: datetime
) -> None:
    open_for = seconds_between(session.clock_in_at, now)
    if open_for < policy.auto_clock_out_after_hours * 3600:
        return

    # Close at the last confirmed heartbeat, not "now" — that is the last moment
    # we know the laptop was actually being used.
    close_at = ensure_aware(session.last_heartbeat_at or session.clock_in_at)

    attendance.clock_out(
        db,
        session,
        at=close_at,
        reason=ClockOutReason.AUTO_SCHEDULE,
        source=EventSource.SERVER,
    )
    alerts.raise_alert(
        db,
        employee=session.user,
        alert_type=AlertType.MISSING_CLOCK_OUT,
        dedup_key=f"missing_clockout:{session.id}",
        session=session,
        context={
            "open_hours": open_for // 3600,
            "closed_at": close_at.isoformat(),
            "auto_closed": True,
        },
        triggered_at=now,
    )
    logger.info("Auto-closed forgotten session %s at %s", session.id, close_at)


# --------------------------------------------------------------------------- #
# Tick
# --------------------------------------------------------------------------- #
def run_tick(db: Session, now: Optional[datetime] = None) -> dict[str, int]:
    """One monitoring pass over every open session."""
    now = now or utcnow()
    cache: dict[Optional[int], EffectivePolicy] = {}
    stats = {"sessions": 0, "offline": 0, "idle": 0, "overrun": 0, "auto_closed": 0}

    for session in _open_sessions(db):
        if session.user is None or not session.user.is_active:
            continue
        stats["sessions"] += 1
        policy = _policy_for(db, session.user, cache)

        try:
            check_forgotten_session(db, session, policy, now)
            if session.clock_out_at is not None:
                stats["auto_closed"] += 1
                continue

            detect_offline(db, session, policy, now)
            if session.is_offline:
                stats["offline"] += 1

            check_break_overrun(db, session, policy, now)
            if session.current_state in (ActivityState.IDLE, ActivityState.LOCKED):
                stats["idle"] += 1
            check_idle(db, session, policy, now)
        except Exception:  # noqa: BLE001 - one bad session must not stop the tick
            logger.exception("Monitor failed for session %s", session.id)
            db.rollback()
            continue

        db.commit()

    return stats


def run_notification_drain(db: Session) -> tuple[int, int]:
    from app.services.notifications.engine import process_pending

    return process_pending(db)
