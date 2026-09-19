"""Realistic demo data, so a fresh install shows a working system.

``seed-demo`` creates people but no attendance, which leaves every dashboard
empty — a poor way to evaluate the product. This builds a plausible fortnight
of history and leaves four employees in four different live states, so the
board, the reports and the alerts page all have something real in them.

Nothing here is used in production; it is an evaluation aid.
"""
from __future__ import annotations

import logging
import random
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.core.timeutil import utcnow
from app.models import (
    ActivityEvent,
    ActivityInterval,
    Alert,
    BreakPeriod,
    Department,
    Device,
    NotificationDelivery,
    User,
    WorkSchedule,
    WorkSession,
)
from app.models.enums import ActivityState, BreakType, ClockOutReason, Role
from app.services import activity, attendance
from app.services.policy import ensure_global_policy

logger = logging.getLogger(__name__)

# A small team, which is what most first installs look like.
TEAM = [
    ("EMP-001", "john.doe", "John Doe", "john@example.com"),
    ("EMP-002", "sara.khan", "Sara Khan", "sara@example.com"),
    ("EMP-003", "marco.rossi", "Marco Rossi", "marco@example.com"),
    ("EMP-004", "amina.osei", "Amina Osei", "amina@example.com"),
]


def _workdays_back(count: int, skip_today: bool = True) -> list[date]:
    """The last ``count`` Mon-Fri dates, most recent last."""
    days: list[date] = []
    cursor = utcnow().date() - (timedelta(days=1) if skip_today else timedelta())
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


def _simulate_day(
    db: Session,
    user: User,
    device: Device,
    day: date,
    rng: random.Random,
) -> WorkSession | None:
    """One finished working day, built through the real service functions so
    the totals reconcile exactly as they would in production."""

    # Roughly one day in twelve is an absence.
    if rng.random() < 0.08:
        return None

    start_minute = rng.choice([-8, -3, 0, 2, 5, 9, 14, 22, 35])
    clock_in = _at(day, 9, 0) + timedelta(minutes=start_minute)
    session = attendance.clock_in(db, user, device=device, at=clock_in)
    db.flush()

    cursor = clock_in

    # A couple of idle stretches through the morning — stepping away from the
    # desk, a meeting, a phone call.
    for _ in range(rng.randint(0, 2)):
        cursor += timedelta(minutes=rng.randint(40, 110))
        idle_for = rng.randint(4, 16)
        activity.transition(db, session, ActivityState.IDLE, at=cursor)
        cursor += timedelta(minutes=idle_for)
        activity.transition(db, session, ActivityState.ACTIVE, at=cursor)

    # A short break most days.
    if rng.random() < 0.6:
        cursor += timedelta(minutes=rng.randint(20, 50))
        period = attendance.start_break(db, session, BreakType.SHORT, at=cursor)
        cursor += timedelta(minutes=rng.randint(6, 13))
        attendance.end_break(db, session, period, at=cursor)

    # Lunch, always.
    lunch_start = max(cursor, _at(day, 12, 15)) + timedelta(
        minutes=rng.randint(0, 45)
    )
    period = attendance.start_break(db, session, BreakType.LUNCH, at=lunch_start)
    cursor = lunch_start + timedelta(minutes=rng.choice([30, 35, 40, 45, 50, 60, 68]))
    attendance.end_break(db, session, period, at=cursor)

    # An afternoon idle stretch.
    if rng.random() < 0.5:
        cursor += timedelta(minutes=rng.randint(45, 120))
        activity.transition(db, session, ActivityState.IDLE, at=cursor)
        cursor += timedelta(minutes=rng.randint(5, 18))
        activity.transition(db, session, ActivityState.ACTIVE, at=cursor)

    end_minute = rng.choice([-95, -40, -12, 0, 3, 8, 15, 28, 45])
    clock_out = max(cursor + timedelta(minutes=20), _at(day, 18, 0) + timedelta(minutes=end_minute))
    attendance.clock_out(db, session, at=clock_out, reason=ClockOutReason.MANUAL)
    db.flush()
    return session


def _open_live_session(
    db: Session,
    user: User,
    device: Device,
    *,
    state: str,
    rng: random.Random,
) -> WorkSession:
    """Leave an employee mid-day in a specific state, as the board would show."""
    now = utcnow()
    today = now.date()

    clock_in = now - timedelta(hours=rng.randint(3, 6), minutes=rng.randint(0, 50))
    session = attendance.clock_in(db, user, device=device, at=clock_in)
    session.work_date = today
    db.flush()

    # Some earlier activity so the day's totals are not all one block.
    mid = clock_in + timedelta(hours=1, minutes=40)
    activity.transition(db, session, ActivityState.IDLE, at=mid)
    activity.transition(db, session, ActivityState.ACTIVE, at=mid + timedelta(minutes=7))

    if state == "active":
        session.last_heartbeat_at = now
        activity.transition(db, session, ActivityState.ACTIVE, at=now - timedelta(minutes=2))

    elif state == "idle":
        # Past the ten-minute threshold, so the monitor raises an alert.
        idle_since = now - timedelta(minutes=14)
        session.last_heartbeat_at = now
        activity.transition(db, session, ActivityState.IDLE, at=idle_since)

    elif state == "on_break":
        started = now - timedelta(minutes=26)
        attendance.start_break(db, session, BreakType.LUNCH, at=started)
        session.last_heartbeat_at = now

    elif state == "offline":
        # The laptop stopped reporting twenty minutes ago.
        last_beat = now - timedelta(minutes=20)
        session.last_heartbeat_at = last_beat

    db.flush()
    return session


def _reset(db: Session) -> None:
    """Clear previous demo data so the command can be re-run."""
    for model in (
        NotificationDelivery, Alert, ActivityEvent, ActivityInterval,
        BreakPeriod, WorkSession,
    ):
        db.execute(delete(model))
    db.flush()


def generate(db: Session, *, weeks: int = 2, seed: int = 20260919) -> dict[str, int]:
    """Build the demo organisation, its history, and today's live states."""
    rng = random.Random(seed)
    ensure_global_policy(db)
    _reset(db)

    schedule = db.scalar(select(WorkSchedule).limit(1))
    if schedule is None:
        schedule = WorkSchedule(
            name="Office hours", start_time=time(9, 0), end_time=time(18, 0),
            timezone="UTC", workdays="1111100",
        )
        db.add(schedule)
        db.flush()

    leader = db.scalar(select(User).where(User.role == Role.TEAM_LEADER))
    if leader is None:
        leader = User(
            employee_code="TL-001", username="demo.leader",
            email="leader@example.com", full_name="Priya Raman",
            password_hash=hash_password("DemoPass2024"), role=Role.TEAM_LEADER,
            schedule_id=schedule.id, must_change_password=False,
        )
        db.add(leader)
        db.flush()

    department = db.scalar(select(Department).limit(1))
    if department is None:
        department = Department(
            name="Customer Support", default_team_leader_id=leader.id,
            default_schedule_id=schedule.id,
        )
        db.add(department)
        db.flush()
    leader.department_id = department.id

    employees: list[User] = []
    for code, username, full_name, email in TEAM:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(
                employee_code=code, username=username, email=email,
                full_name=full_name, password_hash=hash_password("DemoPass2024"),
                role=Role.EMPLOYEE, must_change_password=False,
            )
            db.add(user)
        user.department_id = department.id
        user.team_leader_id = leader.id
        user.schedule_id = schedule.id
        db.flush()
        employees.append(user)

        if not db.scalar(select(Device).where(Device.user_id == user.id)):
            db.add(
                Device(
                    user_id=user.id, device_uid=f"DEMO-{code}",
                    token_hash=f"demo-not-a-real-token-{code}",
                    hostname=f"WKS-{username.split('.')[0].upper()}",
                    platform="Windows", os_version="11 (22631)",
                    agent_version="1.0.0", last_seen_at=utcnow(),
                )
            )
    db.flush()

    days = _workdays_back(weeks * 5)
    sessions = 0
    for user in employees:
        device = db.scalar(select(Device).where(Device.user_id == user.id))
        for day in days:
            if _simulate_day(db, user, device, day, rng) is not None:
                sessions += 1
        db.flush()

    # Today: one employee in each live state.
    live_states = ["active", "idle", "on_break", "offline"]
    for user, state in zip(employees, live_states, strict=False):
        device = db.scalar(select(Device).where(Device.user_id == user.id))
        _open_live_session(db, user, device, state=state, rng=rng)
    db.commit()

    # Let the monitor raise the alerts the live states imply.
    from app.services import monitor

    monitor.run_tick(db)
    db.commit()

    from sqlalchemy import func

    return {
        "employees": len(employees),
        "days": len(days),
        "sessions": sessions,
        "alerts": db.scalar(select(func.count(Alert.id))) or 0,
    }
