"""Activity interval bookkeeping.

The server keeps exactly one open ``ActivityInterval`` per open work session.
Every state change closes the current interval and opens the next one, adding
the elapsed duration to the matching total on the session. That makes active,
idle, break and offline totals exact without storing every heartbeat.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import ensure_aware, seconds_between, utcnow
from app.models import ActivityEvent, ActivityInterval, BreakPeriod, WorkSession
from app.models.enums import ActivityState, BreakType, EventSource, EventType

logger = logging.getLogger(__name__)

# Which session counter each state accumulates into.
_TOTAL_FIELD: dict[ActivityState, str] = {
    ActivityState.ACTIVE: "active_seconds",
    ActivityState.IDLE: "idle_seconds",
    ActivityState.LOCKED: "locked_seconds",
    ActivityState.BREAK_LUNCH: "lunch_seconds",
    ActivityState.BREAK_SHORT: "short_break_seconds",
    ActivityState.OFFLINE: "offline_seconds",
}


def open_interval(db: Session, session: WorkSession) -> Optional[ActivityInterval]:
    return db.scalar(
        select(ActivityInterval)
        .where(
            ActivityInterval.session_id == session.id,
            ActivityInterval.ended_at.is_(None),
        )
        .order_by(ActivityInterval.started_at.desc())
        .limit(1)
    )


def close_interval(
    db: Session, interval: ActivityInterval, at: datetime | None = None
) -> int:
    """Close an interval and fold its duration into the session totals."""
    at = ensure_aware(at or utcnow())
    started = ensure_aware(interval.started_at)
    # Guard against clock skew producing negative or absurd durations.
    if at < started:
        at = started
    duration = int((at - started).total_seconds())

    interval.ended_at = at
    interval.duration_seconds = duration

    session = db.get(WorkSession, interval.session_id)
    if session is not None:
        field = _TOTAL_FIELD.get(interval.state)
        if field:
            setattr(session, field, getattr(session, field, 0) + duration)
    return duration


def start_interval(
    db: Session, session: WorkSession, state: ActivityState, at: datetime
) -> ActivityInterval:
    interval = ActivityInterval(
        session_id=session.id,
        user_id=session.user_id,
        state=state,
        started_at=ensure_aware(at),
        duration_seconds=0,
    )
    db.add(interval)
    session.current_state = state
    session.state_since = ensure_aware(at)
    return interval


def transition(
    db: Session,
    session: WorkSession,
    new_state: ActivityState,
    *,
    at: datetime | None = None,
    effective_from: datetime | None = None,
) -> bool:
    """Move the session into ``new_state``.

    ``effective_from`` backdates the transition — used when a heartbeat reveals
    the user has *already* been idle for N seconds, so the idle run is recorded
    from when input actually stopped rather than from when we noticed.

    Returns True when a transition happened.
    """
    at = ensure_aware(at or utcnow())
    boundary = ensure_aware(effective_from or at)

    current = open_interval(db, session)
    if current is not None and current.state is new_state:
        return False

    if current is not None:
        # Never backdate before the interval actually opened.
        boundary = max(boundary, ensure_aware(current.started_at))
        boundary = min(boundary, at)
        close_interval(db, current, boundary)
    else:
        boundary = min(boundary, at)
        boundary = max(boundary, ensure_aware(session.clock_in_at))

    start_interval(db, session, new_state, boundary)
    db.flush()
    return True


def desired_state(
    session: WorkSession,
    *,
    open_break: Optional[BreakPeriod],
    idle_seconds: int,
    is_locked: bool,
    idle_threshold: int,
    treat_lock_as_idle: bool,
) -> ActivityState:
    """The state a session should be in, given the latest heartbeat."""
    if open_break is not None:
        return (
            ActivityState.BREAK_LUNCH
            if open_break.break_type is BreakType.LUNCH
            else ActivityState.BREAK_SHORT
        )
    if is_locked and treat_lock_as_idle:
        return ActivityState.LOCKED
    if idle_seconds >= idle_threshold:
        return ActivityState.IDLE
    return ActivityState.ACTIVE


def log_event(
    db: Session,
    *,
    user_id: int,
    event_type: EventType,
    occurred_at: datetime | None = None,
    session_id: Optional[int] = None,
    device_id: Optional[int] = None,
    source: EventSource = EventSource.AGENT,
    payload: Optional[dict[str, Any]] = None,
    message: Optional[str] = None,
) -> ActivityEvent:
    """Append to the immutable timeline."""
    now = utcnow()
    event = ActivityEvent(
        user_id=user_id,
        session_id=session_id,
        device_id=device_id,
        event_type=event_type,
        source=source,
        occurred_at=ensure_aware(occurred_at or now),
        recorded_at=now,
        payload=payload,
        message=message,
    )
    db.add(event)
    return event


def recompute_totals(db: Session, session: WorkSession) -> None:
    """Rebuild session totals from its intervals.

    Used after a manager edits a session and as a self-healing repair path if a
    process died between closing an interval and committing the totals.
    """
    totals = {field: 0 for field in _TOTAL_FIELD.values()}
    intervals = db.scalars(
        select(ActivityInterval).where(ActivityInterval.session_id == session.id)
    )
    now = utcnow()
    for interval in intervals:
        field = _TOTAL_FIELD.get(interval.state)
        if not field:
            continue
        if interval.ended_at is not None:
            totals[field] += interval.duration_seconds
        else:
            totals[field] += seconds_between(interval.started_at, now)

    for field, value in totals.items():
        setattr(session, field, value)


def live_totals(db: Session, session: WorkSession) -> dict[str, int]:
    """Session totals including the still-open interval, for live display."""
    totals = {field: getattr(session, field, 0) for field in _TOTAL_FIELD.values()}
    current = open_interval(db, session)
    if current is not None:
        field = _TOTAL_FIELD.get(current.state)
        if field:
            totals[field] += seconds_between(current.started_at)
    return totals
