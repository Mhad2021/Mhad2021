"""Clock in/out, breaks and time accounting."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.core.timeutil import utcnow
from app.models.enums import (
    ActivityState,
    BreakEndReason,
    BreakType,
    ClockOutReason,
    EventType,
)
from app.models import ActivityEvent
from app.services import activity, attendance
from app.services.attendance import AttendanceError
from app.services.policy import get_policy


def test_clock_in_opens_a_session_and_an_active_interval(db, employee, policy, device):
    session = attendance.clock_in(db, employee, device=device)
    db.commit()

    assert session.is_open
    assert session.current_state is ActivityState.ACTIVE
    interval = activity.open_interval(db, session)
    assert interval is not None and interval.state is ActivityState.ACTIVE

    events = db.query(ActivityEvent).filter(
        ActivityEvent.event_type == EventType.CLOCK_IN
    ).all()
    assert len(events) == 1


def test_cannot_clock_in_twice(db, employee, policy):
    attendance.clock_in(db, employee)
    db.commit()
    with pytest.raises(AttendanceError, match="already clocked in"):
        attendance.clock_in(db, employee)


def test_clock_out_closes_the_session_and_totals_the_time(db, employee, policy, rewind):
    session = attendance.clock_in(db, employee)
    db.commit()
    rewind(session, timedelta(hours=8))

    attendance.clock_out(db, session)
    db.commit()

    assert not session.is_open
    assert session.clock_out_reason is ClockOutReason.MANUAL
    # Eight hours of uninterrupted activity.
    assert 28790 <= session.active_seconds <= 28810
    assert session.worked_seconds == pytest.approx(28800, abs=10)


def _last_workday() -> "date":
    """Most recent Mon-Fri date, so schedule tests do not break at the weekend."""
    day = utcnow().date()
    while day.weekday() > 4:
        day -= timedelta(days=1)
    return day


def test_late_arrival_is_flagged_against_the_schedule(db, employee, policy, schedule):
    # Schedule starts 09:00 UTC with 10 minutes' grace; arrive at 09:25.
    today = _last_workday()
    late = datetime.combine(today, time(9, 25), tzinfo=timezone.utc)

    session = attendance.clock_in(db, employee, at=late)
    db.commit()

    assert session.is_late_arrival
    assert session.late_by_seconds == pytest.approx(25 * 60, abs=5)


def test_arrival_within_grace_is_not_late(db, employee, policy, schedule):
    today = _last_workday()
    session = attendance.clock_in(
        db, employee, at=datetime.combine(today, time(9, 8), tzinfo=timezone.utc)
    )
    db.commit()
    assert not session.is_late_arrival


def test_early_departure_is_flagged(db, employee, policy, schedule):
    today = _last_workday()
    session = attendance.clock_in(
        db, employee, at=datetime.combine(today, time(9, 0), tzinfo=timezone.utc)
    )
    db.commit()

    attendance.clock_out(
        db, session, at=datetime.combine(today, time(16, 30), tzinfo=timezone.utc)
    )
    db.commit()

    assert session.is_early_departure
    assert session.early_by_seconds == pytest.approx(90 * 60, abs=5)


class TestBreaks:
    def test_short_break_uses_the_configured_allowance(self, db, employee, policy):
        session = attendance.clock_in(db, employee)
        db.commit()

        period = attendance.start_break(db, session, BreakType.SHORT)
        db.commit()

        assert period.allowed_seconds == 600
        assert session.current_state is ActivityState.BREAK_SHORT

    def test_lunch_break_uses_the_lunch_allowance(self, db, employee, policy):
        session = attendance.clock_in(db, employee)
        db.commit()

        period = attendance.start_break(db, session, BreakType.LUNCH)
        db.commit()

        assert period.allowed_seconds == 3600
        assert session.current_state is ActivityState.BREAK_LUNCH

    def test_cannot_start_two_breaks_at_once(self, db, employee, policy):
        session = attendance.clock_in(db, employee)
        db.commit()
        attendance.start_break(db, session, BreakType.SHORT)
        db.commit()

        with pytest.raises(AttendanceError, match="already running"):
            attendance.start_break(db, session, BreakType.LUNCH)

    def test_daily_break_limit_is_enforced(self, db, employee, policy):
        session = attendance.clock_in(db, employee)
        db.commit()

        for _ in range(policy.short_breaks_per_day):
            period = attendance.start_break(db, session, BreakType.SHORT)
            attendance.end_break(db, session, period)
            db.commit()

        with pytest.raises(AttendanceError, match="already used all"):
            attendance.start_break(db, session, BreakType.SHORT)

    def test_ending_a_break_returns_to_active_and_records_duration(
        self, db, employee, policy, rewind
    ):
        session = attendance.clock_in(db, employee)
        db.commit()
        period = attendance.start_break(db, session, BreakType.SHORT)
        db.commit()
        rewind(session, timedelta(minutes=7))

        attendance.end_break(db, session, period)
        db.commit()

        assert period.ended_at is not None
        assert period.end_reason is BreakEndReason.MANUAL
        assert period.duration_seconds == pytest.approx(420, abs=5)
        assert period.overrun_seconds == 0
        assert session.current_state is ActivityState.ACTIVE
        assert session.short_break_seconds == pytest.approx(420, abs=5)

    def test_overrunning_a_break_records_the_overrun(self, db, employee, policy, rewind):
        session = attendance.clock_in(db, employee)
        db.commit()
        period = attendance.start_break(db, session, BreakType.SHORT)
        db.commit()
        rewind(session, timedelta(minutes=15))

        attendance.end_break(db, session, period)
        db.commit()

        assert period.duration_seconds == pytest.approx(900, abs=5)
        assert period.overrun_seconds == pytest.approx(300, abs=5)

    def test_clocking_out_closes_a_running_break(self, db, employee, policy, rewind):
        session = attendance.clock_in(db, employee)
        db.commit()
        period = attendance.start_break(db, session, BreakType.LUNCH)
        db.commit()
        rewind(session, timedelta(minutes=30))

        attendance.clock_out(db, session)
        db.commit()

        assert period.ended_at is not None
        assert period.end_reason is BreakEndReason.SESSION_END
        assert session.lunch_seconds == pytest.approx(1800, abs=5)


def test_break_time_is_excluded_from_worked_time(db, employee, policy, rewind):
    """Eight hours on the clock with an hour's lunch is seven hours worked."""
    session = attendance.clock_in(db, employee)
    db.commit()
    rewind(session, timedelta(hours=8))

    period = attendance.start_break(db, session, BreakType.LUNCH)
    db.commit()
    rewind(session, timedelta(hours=1))
    attendance.end_break(db, session, period)
    db.commit()

    attendance.clock_out(db, session)
    db.commit()

    assert session.total_seconds == pytest.approx(9 * 3600, abs=10)
    assert session.lunch_seconds == pytest.approx(3600, abs=10)
    assert session.worked_seconds == pytest.approx(8 * 3600, abs=10)


def test_interval_totals_reconcile_with_the_session_span(db, employee, policy, rewind):
    """Every second of the session must land in exactly one bucket."""
    session = attendance.clock_in(db, employee)
    db.commit()
    rewind(session, timedelta(hours=2))

    period = attendance.start_break(db, session, BreakType.SHORT)
    db.commit()
    rewind(session, timedelta(minutes=10))
    attendance.end_break(db, session, period)
    db.commit()
    rewind(session, timedelta(minutes=30))

    attendance.clock_out(db, session)
    db.commit()

    accounted = (
        session.active_seconds
        + session.idle_seconds
        + session.locked_seconds
        + session.lunch_seconds
        + session.short_break_seconds
        + session.offline_seconds
    )
    assert accounted == pytest.approx(session.total_seconds, abs=5)
