"""Clock-in / clock-out and approved break handling."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timeutil import (
    combine_local,
    ensure_aware,
    local_date,
    seconds_between,
    utcnow,
)
from app.models import BreakPeriod, Device, User, WorkSession, WorkSchedule
from app.models.enums import (
    ActivityState,
    BreakEndReason,
    BreakType,
    ClockOutReason,
    EventSource,
    EventType,
)
from app.services import activity
from app.services.policy import EffectivePolicy, get_policy

logger = logging.getLogger(__name__)


class AttendanceError(Exception):
    """Raised for rule violations the caller should surface to the user."""


@dataclass
class ScheduleWindow:
    start_at: Optional[datetime]
    end_at: Optional[datetime]
    is_workday: bool


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
def get_open_session(db: Session, user_id: int) -> Optional[WorkSession]:
    return db.scalar(
        select(WorkSession)
        .where(WorkSession.user_id == user_id, WorkSession.clock_out_at.is_(None))
        .order_by(WorkSession.clock_in_at.desc())
        .limit(1)
    )


def get_open_break(db: Session, session_id: int) -> Optional[BreakPeriod]:
    return db.scalar(
        select(BreakPeriod)
        .where(BreakPeriod.session_id == session_id, BreakPeriod.ended_at.is_(None))
        .order_by(BreakPeriod.started_at.desc())
        .limit(1)
    )


def resolve_schedule(db: Session, user: User) -> Optional[WorkSchedule]:
    if user.schedule_id and user.schedule:
        return user.schedule
    if user.department and user.department.default_schedule:
        return user.department.default_schedule
    return None


def schedule_window(db: Session, user: User, work_date: date) -> ScheduleWindow:
    """The employee's expected start and end, as UTC instants."""
    schedule = resolve_schedule(db, user)
    if schedule is None:
        return ScheduleWindow(None, None, True)

    tz = schedule.timezone or user.timezone
    start_at = combine_local(work_date, schedule.start_time, tz)
    end_at = combine_local(work_date, schedule.end_time, tz)
    # Overnight shift (e.g. 22:00 -> 06:00): the end falls on the next day.
    if schedule.end_time <= schedule.start_time:
        end_at += timedelta(days=1)

    return ScheduleWindow(start_at, end_at, schedule.is_workday(work_date.weekday()))


# --------------------------------------------------------------------------- #
# Clock in / out
# --------------------------------------------------------------------------- #
def clock_in(
    db: Session,
    user: User,
    *,
    device: Optional[Device] = None,
    at: Optional[datetime] = None,
    source: EventSource = EventSource.AGENT,
) -> WorkSession:
    at = ensure_aware(at or utcnow())

    existing = get_open_session(db, user.id)
    if existing is not None:
        raise AttendanceError(
            "You are already clocked in. Clock out before starting a new session."
        )

    work_date = local_date(at, user.timezone)
    window = schedule_window(db, user, work_date)
    schedule = resolve_schedule(db, user)

    session = WorkSession(
        user_id=user.id,
        device_id=device.id if device else None,
        work_date=work_date,
        clock_in_at=at,
        scheduled_start_at=window.start_at,
        scheduled_end_at=window.end_at,
        last_heartbeat_at=at,
        current_state=ActivityState.ACTIVE,
        state_since=at,
    )

    if window.start_at is not None and schedule is not None and window.is_workday:
        grace = timedelta(minutes=schedule.grace_late_minutes)
        if at > window.start_at + grace:
            session.is_late_arrival = True
            session.late_by_seconds = seconds_between(window.start_at, at)

    db.add(session)
    db.flush()

    activity.start_interval(db, session, ActivityState.ACTIVE, at)
    activity.log_event(
        db,
        user_id=user.id,
        session_id=session.id,
        device_id=device.id if device else None,
        event_type=EventType.CLOCK_IN,
        occurred_at=at,
        source=source,
        payload={"work_date": work_date.isoformat()},
        message=f"{user.full_name} clocked in",
    )

    if session.is_late_arrival:
        activity.log_event(
            db,
            user_id=user.id,
            session_id=session.id,
            event_type=EventType.LATE_ARRIVAL,
            occurred_at=at,
            source=EventSource.SERVER,
            payload={"late_by_seconds": session.late_by_seconds},
            message=f"Late arrival by {session.late_by_seconds // 60} minutes",
        )

    db.flush()
    return session


def clock_out(
    db: Session,
    session: WorkSession,
    *,
    at: Optional[datetime] = None,
    reason: ClockOutReason = ClockOutReason.MANUAL,
    actor: Optional[User] = None,
    source: EventSource = EventSource.AGENT,
) -> WorkSession:
    at = ensure_aware(at or utcnow())
    if session.clock_out_at is not None:
        raise AttendanceError("This session is already closed.")
    if at < ensure_aware(session.clock_in_at):
        at = ensure_aware(session.clock_in_at)

    user = db.get(User, session.user_id)

    # Close any running break first so its duration is accounted for.
    running = get_open_break(db, session.id)
    if running is not None:
        end_break(
            db,
            session,
            running,
            at=at,
            reason=BreakEndReason.SESSION_END,
            transition_after=False,
        )

    current = activity.open_interval(db, session)
    if current is not None:
        activity.close_interval(db, current, at)

    session.clock_out_at = at
    session.clock_out_reason = reason
    session.current_state = ActivityState.OFFLINE
    session.state_since = at
    session.is_offline = False
    session.offline_since = None

    schedule = resolve_schedule(db, user) if user else None
    if session.scheduled_end_at is not None and schedule is not None:
        grace = timedelta(minutes=schedule.grace_early_minutes)
        if at < ensure_aware(session.scheduled_end_at) - grace:
            session.is_early_departure = True
            session.early_by_seconds = seconds_between(at, session.scheduled_end_at)

    if actor is not None and actor.id != session.user_id:
        session.edited_by_id = actor.id

    activity.log_event(
        db,
        user_id=session.user_id,
        session_id=session.id,
        device_id=session.device_id,
        event_type=EventType.CLOCK_OUT,
        occurred_at=at,
        source=source,
        payload={
            "reason": reason.value,
            "worked_seconds": session.worked_seconds,
            "active_seconds": session.active_seconds,
            "idle_seconds": session.idle_seconds,
        },
        message=f"{user.full_name if user else 'Employee'} clocked out ({reason.value})",
    )

    if session.is_early_departure:
        activity.log_event(
            db,
            user_id=session.user_id,
            session_id=session.id,
            event_type=EventType.EARLY_DEPARTURE,
            occurred_at=at,
            source=EventSource.SERVER,
            payload={"early_by_seconds": session.early_by_seconds},
            message=f"Early departure by {session.early_by_seconds // 60} minutes",
        )

    db.flush()
    return session


# --------------------------------------------------------------------------- #
# Breaks
# --------------------------------------------------------------------------- #
def breaks_taken_today(
    db: Session, user_id: int, work_date: date, break_type: BreakType
) -> int:
    return (
        db.scalar(
            select(func.count(BreakPeriod.id))
            .join(WorkSession, WorkSession.id == BreakPeriod.session_id)
            .where(
                BreakPeriod.user_id == user_id,
                BreakPeriod.break_type == break_type,
                WorkSession.work_date == work_date,
            )
        )
        or 0
    )


def start_break(
    db: Session,
    session: WorkSession,
    break_type: BreakType,
    *,
    at: Optional[datetime] = None,
    policy: Optional[EffectivePolicy] = None,
    source: EventSource = EventSource.AGENT,
) -> BreakPeriod:
    at = ensure_aware(at or utcnow())
    user = db.get(User, session.user_id)
    policy = policy or get_policy(db, user)

    if session.clock_out_at is not None:
        raise AttendanceError("You must be clocked in to start a break.")

    if get_open_break(db, session.id) is not None:
        raise AttendanceError("A break is already running. End it before starting another.")

    limit = policy.daily_limit_for(break_type)
    taken = breaks_taken_today(db, session.user_id, session.work_date, break_type)
    label = "lunch break" if break_type is BreakType.LUNCH else "10-minute break"
    if limit > 0 and taken >= limit:
        raise AttendanceError(
            f"You have already used all {limit} {label}(s) allowed today. "
            "Ask your team leader if you need another."
        )

    allowance = policy.allowance_for(break_type)
    period = BreakPeriod(
        session_id=session.id,
        user_id=session.user_id,
        break_type=break_type,
        started_at=at,
        allowed_seconds=allowance,
    )
    db.add(period)
    db.flush()

    activity.transition(
        db,
        session,
        ActivityState.BREAK_LUNCH
        if break_type is BreakType.LUNCH
        else ActivityState.BREAK_SHORT,
        at=at,
    )
    activity.log_event(
        db,
        user_id=session.user_id,
        session_id=session.id,
        device_id=session.device_id,
        event_type=EventType.BREAK_START,
        occurred_at=at,
        source=source,
        payload={"break_type": break_type.value, "allowed_seconds": allowance},
        message=f"Started {label} ({allowance // 60} min allowed)",
    )
    db.flush()
    return period


def end_break(
    db: Session,
    session: WorkSession,
    period: BreakPeriod,
    *,
    at: Optional[datetime] = None,
    reason: BreakEndReason = BreakEndReason.MANUAL,
    transition_after: bool = True,
    source: EventSource = EventSource.AGENT,
) -> BreakPeriod:
    at = ensure_aware(at or utcnow())
    if period.ended_at is not None:
        return period
    if at < ensure_aware(period.started_at):
        at = ensure_aware(period.started_at)

    period.ended_at = at
    period.end_reason = reason
    period.duration_seconds = seconds_between(period.started_at, at)
    period.overrun_seconds = max(0, period.duration_seconds - period.allowed_seconds)

    if transition_after and session.clock_out_at is None:
        activity.transition(db, session, ActivityState.ACTIVE, at=at)

    label = "lunch break" if period.break_type is BreakType.LUNCH else "10-minute break"
    activity.log_event(
        db,
        user_id=session.user_id,
        session_id=session.id,
        device_id=session.device_id,
        event_type=EventType.BREAK_END,
        occurred_at=at,
        source=source,
        payload={
            "break_type": period.break_type.value,
            "duration_seconds": period.duration_seconds,
            "overrun_seconds": period.overrun_seconds,
            "reason": reason.value,
        },
        message=f"Ended {label} after {period.duration_seconds // 60} min",
    )
    db.flush()
    return period


def break_remaining_seconds(period: BreakPeriod, now: Optional[datetime] = None) -> int:
    """Seconds left on the allowance; negative once overrunning."""
    elapsed = seconds_between(period.started_at, now or utcnow())
    return period.allowed_seconds - elapsed
