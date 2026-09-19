"""Daily, weekly and monthly reporting."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.core.timeutil import utcnow
from app.models.enums import BreakType
from app.services import attendance, reporting


def _workday(offset_days: int = 0) -> date:
    day = utcnow().date() - timedelta(days=offset_days)
    while day.weekday() > 4:
        day -= timedelta(days=1)
    return day


def _completed_day(db, employee, day: date, start=time(9, 0), end=time(17, 30),
                   lunch_minutes: int = 45):
    """Record one finished working day for the employee."""
    clock_in = datetime.combine(day, start, tzinfo=UTC)
    clock_out = datetime.combine(day, end, tzinfo=UTC)

    session = attendance.clock_in(db, employee, at=clock_in)
    db.commit()

    lunch_start = clock_in + timedelta(hours=3)
    period = attendance.start_break(db, session, BreakType.LUNCH, at=lunch_start)
    attendance.end_break(
        db, session, period, at=lunch_start + timedelta(minutes=lunch_minutes)
    )
    db.commit()

    attendance.clock_out(db, session, at=clock_out)
    db.commit()
    return session


def test_a_day_with_no_session_reports_as_absent(db, employee, policy):
    row = reporting.daily_row(db, employee, _workday())
    assert row.is_absent
    assert row.worked_seconds == 0


def test_daily_row_totals_a_completed_day(db, employee, policy, schedule):
    day = _workday()
    _completed_day(db, employee, day)

    row = reporting.daily_row(db, employee, day)

    assert not row.is_absent
    assert row.session_count == 1
    assert row.lunch_count == 1
    assert row.lunch_seconds == pytest.approx(45 * 60, abs=5)
    # 09:00-17:30 is 8.5 hours, less 45 minutes of lunch.
    assert row.worked_seconds == pytest.approx(8.5 * 3600 - 45 * 60, abs=10)


def test_late_arrival_and_early_departure_show_in_the_row(db, employee, policy, schedule):
    day = _workday()
    _completed_day(db, employee, day, start=time(9, 40), end=time(16, 0))

    row = reporting.daily_row(db, employee, day)

    assert row.is_late
    assert row.late_by_seconds == pytest.approx(40 * 60, abs=5)
    assert row.is_early_departure


def test_multiple_sessions_in_one_day_are_combined(db, employee, policy, schedule):
    day = _workday()
    morning_in = datetime.combine(day, time(9, 0), tzinfo=UTC)
    session = attendance.clock_in(db, employee, at=morning_in)
    db.commit()
    attendance.clock_out(db, session, at=morning_in + timedelta(hours=3))
    db.commit()

    afternoon_in = datetime.combine(day, time(14, 0), tzinfo=UTC)
    session = attendance.clock_in(db, employee, at=afternoon_in)
    db.commit()
    attendance.clock_out(db, session, at=afternoon_in + timedelta(hours=4))
    db.commit()

    row = reporting.daily_row(db, employee, day)

    assert row.session_count == 2
    assert row.worked_seconds == pytest.approx(7 * 3600, abs=10)
    assert row.clock_in_at.hour == 9
    assert row.clock_out_at.hour == 18


def test_activity_rate_is_active_over_worked(db, employee, policy, schedule):
    day = _workday()
    _completed_day(db, employee, day, lunch_minutes=60)

    row = reporting.daily_row(db, employee, day)

    assert 0 < row.activity_rate <= 100


def test_period_summary_aggregates_across_days(db, employee, policy, schedule):
    days = [_workday(offset_days=n) for n in range(0, 3)]
    for day in set(days):
        _completed_day(db, employee, day)

    start, end = min(days), max(days)
    summary = reporting.period_summary(db, employee, start, end)

    assert summary.days_present == len(set(days))
    assert summary.worked_seconds > 0
    assert summary.avg_daily_seconds == pytest.approx(
        summary.worked_seconds // summary.days_present, abs=1
    )


def test_non_working_days_are_not_counted_as_absences(db, employee, policy, schedule):
    """A Mon-Fri schedule should not show Saturday and Sunday as absent."""
    monday = _workday()
    while monday.weekday() != 0:
        monday -= timedelta(days=1)
    sunday = monday + timedelta(days=6)

    summary = reporting.period_summary(db, employee, monday, sunday)

    assert len(summary.days) == 5
    assert summary.days_absent == 5  # no sessions recorded, but only weekdays counted


def test_department_rollup_groups_by_department(db, employee, policy, schedule, leader):
    day = _workday()
    _completed_day(db, employee, day)

    summary = reporting.period_summary(db, employee, day, day)
    rollup = reporting.department_rollup([summary])

    assert len(rollup) == 1
    assert rollup[0]["department"] == "Support"
    assert rollup[0]["employees"] == 1


def test_daily_csv_has_a_header_and_one_row_per_employee(db, employee, policy, schedule):
    day = _workday()
    _completed_day(db, employee, day)

    csv_text = reporting.daily_csv([reporting.daily_row(db, employee, day)])
    lines = csv_text.strip().splitlines()

    assert lines[0].startswith("Date,Employee code,Name")
    assert len(lines) == 2
    assert "John Doe" in lines[1]


def test_period_csv_exports_the_summary(db, employee, policy, schedule):
    day = _workday()
    _completed_day(db, employee, day)

    csv_text = reporting.period_csv(
        [reporting.period_summary(db, employee, day, day)]
    )

    assert "Employee code,Name,Department" in csv_text
    assert "John Doe" in csv_text


def test_scope_limits_what_a_team_leader_can_report_on(
    db, employee, leader, policy, schedule
):

    visible = reporting.scope_users(db, leader)
    assert employee.id in [u.id for u in visible]

    own_only = reporting.scope_users(db, employee)
    assert [u.id for u in own_only] == [employee.id]
