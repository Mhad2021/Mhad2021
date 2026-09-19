"""Attendance reporting: daily rows, period rollups and CSV export."""
from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.timeutil import (
    ensure_aware,
    format_hours,
    humanize_duration,
    month_bounds,
    to_local,
    week_bounds,
)
from app.models import BreakPeriod, Department, User, WorkSession
from app.models.enums import BreakType, Role
from app.services import attendance


@dataclass
class DailyRow:
    user_id: int
    full_name: str
    employee_code: str
    department: Optional[str]
    work_date: date
    clock_in_at: Optional[datetime] = None
    clock_out_at: Optional[datetime] = None
    scheduled_start_at: Optional[datetime] = None
    scheduled_end_at: Optional[datetime] = None
    active_seconds: int = 0
    idle_seconds: int = 0
    lunch_seconds: int = 0
    short_break_seconds: int = 0
    offline_seconds: int = 0
    worked_seconds: int = 0
    total_span_seconds: int = 0
    is_late: bool = False
    late_by_seconds: int = 0
    is_early_departure: bool = False
    early_by_seconds: int = 0
    session_count: int = 0
    lunch_count: int = 0
    short_break_count: int = 0
    is_absent: bool = False
    was_edited: bool = False
    tz: str = "UTC"

    @property
    def break_seconds(self) -> int:
        return self.lunch_seconds + self.short_break_seconds

    @property
    def activity_rate(self) -> float:
        """Active time as a share of worked time."""
        if self.worked_seconds <= 0:
            return 0.0
        return round(100 * self.active_seconds / self.worked_seconds, 1)

    def _fmt(self, value: Optional[datetime]) -> str:
        return to_local(value, self.tz).strftime("%H:%M") if value else "—"

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "full_name": self.full_name,
            "employee_code": self.employee_code,
            "department": self.department,
            "date": self.work_date.isoformat(),
            "clock_in": self._fmt(self.clock_in_at),
            "clock_out": self._fmt(self.clock_out_at),
            "scheduled_start": self._fmt(self.scheduled_start_at),
            "scheduled_end": self._fmt(self.scheduled_end_at),
            "worked_hours": format_hours(self.worked_seconds),
            "worked_display": humanize_duration(self.worked_seconds),
            "active_hours": format_hours(self.active_seconds),
            "active_display": humanize_duration(self.active_seconds),
            "idle_hours": format_hours(self.idle_seconds),
            "idle_display": humanize_duration(self.idle_seconds),
            "lunch_display": humanize_duration(self.lunch_seconds),
            "break_display": humanize_duration(self.short_break_seconds),
            "offline_display": humanize_duration(self.offline_seconds),
            "activity_rate": self.activity_rate,
            "is_late": self.is_late,
            "late_by": humanize_duration(self.late_by_seconds) if self.is_late else "",
            "is_early_departure": self.is_early_departure,
            "early_by": (
                humanize_duration(self.early_by_seconds)
                if self.is_early_departure
                else ""
            ),
            "is_absent": self.is_absent,
            "sessions": self.session_count,
            "lunch_count": self.lunch_count,
            "break_count": self.short_break_count,
            "was_edited": self.was_edited,
        }


@dataclass
class PeriodSummary:
    user_id: int
    full_name: str
    employee_code: str
    department: Optional[str]
    start: date
    end: date
    days: list[DailyRow] = field(default_factory=list)

    @property
    def days_present(self) -> int:
        return sum(1 for d in self.days if not d.is_absent)

    @property
    def days_absent(self) -> int:
        return sum(1 for d in self.days if d.is_absent)

    @property
    def late_days(self) -> int:
        return sum(1 for d in self.days if d.is_late)

    @property
    def early_departures(self) -> int:
        return sum(1 for d in self.days if d.is_early_departure)

    @property
    def worked_seconds(self) -> int:
        return sum(d.worked_seconds for d in self.days)

    @property
    def active_seconds(self) -> int:
        return sum(d.active_seconds for d in self.days)

    @property
    def idle_seconds(self) -> int:
        return sum(d.idle_seconds for d in self.days)

    @property
    def lunch_seconds(self) -> int:
        return sum(d.lunch_seconds for d in self.days)

    @property
    def break_seconds(self) -> int:
        return sum(d.short_break_seconds for d in self.days)

    @property
    def activity_rate(self) -> float:
        if self.worked_seconds <= 0:
            return 0.0
        return round(100 * self.active_seconds / self.worked_seconds, 1)

    @property
    def avg_daily_seconds(self) -> int:
        present = self.days_present
        return self.worked_seconds // present if present else 0

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "full_name": self.full_name,
            "employee_code": self.employee_code,
            "department": self.department,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days_present": self.days_present,
            "days_absent": self.days_absent,
            "late_days": self.late_days,
            "early_departures": self.early_departures,
            "worked_hours": format_hours(self.worked_seconds),
            "active_hours": format_hours(self.active_seconds),
            "idle_hours": format_hours(self.idle_seconds),
            "lunch_hours": format_hours(self.lunch_seconds),
            "break_hours": format_hours(self.break_seconds),
            "activity_rate": self.activity_rate,
            "avg_daily_hours": format_hours(self.avg_daily_seconds),
        }


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _empty_row(user: User, day: date) -> DailyRow:
    return DailyRow(
        user_id=user.id,
        full_name=user.full_name,
        employee_code=user.employee_code,
        department=user.department.name if user.department else None,
        work_date=day,
        is_absent=True,
        tz=user.timezone or "UTC",
    )


def daily_row(db: Session, user: User, day: date) -> DailyRow:
    """Roll every session on ``day`` into a single attendance row."""
    sessions = list(
        db.scalars(
            select(WorkSession)
            .where(WorkSession.user_id == user.id, WorkSession.work_date == day)
            .order_by(WorkSession.clock_in_at)
        )
    )
    if not sessions:
        return _empty_row(user, day)

    row = DailyRow(
        user_id=user.id,
        full_name=user.full_name,
        employee_code=user.employee_code,
        department=user.department.name if user.department else None,
        work_date=day,
        session_count=len(sessions),
        tz=user.timezone or "UTC",
    )

    first, last = sessions[0], sessions[-1]
    row.clock_in_at = ensure_aware(first.clock_in_at)
    row.clock_out_at = ensure_aware(last.clock_out_at) if last.clock_out_at else None
    row.scheduled_start_at = first.scheduled_start_at
    row.scheduled_end_at = last.scheduled_end_at
    row.is_late = first.is_late_arrival
    row.late_by_seconds = first.late_by_seconds
    row.is_early_departure = last.is_early_departure
    row.early_by_seconds = last.early_by_seconds

    for session in sessions:
        row.active_seconds += session.active_seconds
        row.idle_seconds += session.idle_seconds + session.locked_seconds
        row.lunch_seconds += session.lunch_seconds
        row.short_break_seconds += session.short_break_seconds
        row.offline_seconds += session.offline_seconds
        row.total_span_seconds += session.total_seconds
        row.worked_seconds += session.worked_seconds
        if session.edited_by_id is not None:
            row.was_edited = True

    counts = db.execute(
        select(BreakPeriod.break_type, BreakPeriod.id)
        .join(WorkSession, WorkSession.id == BreakPeriod.session_id)
        .where(BreakPeriod.user_id == user.id, WorkSession.work_date == day)
    ).all()
    row.lunch_count = sum(1 for bt, _ in counts if bt is BreakType.LUNCH)
    row.short_break_count = sum(1 for bt, _ in counts if bt is BreakType.SHORT)

    return row


def period_summary(
    db: Session, user: User, start: date, end: date, include_absent: bool = True
) -> PeriodSummary:
    summary = PeriodSummary(
        user_id=user.id,
        full_name=user.full_name,
        employee_code=user.employee_code,
        department=user.department.name if user.department else None,
        start=start,
        end=end,
    )

    schedule = attendance.resolve_schedule(db, user)
    from app.core.timeutil import daterange

    for day in daterange(start, end):
        # Skip non-working days entirely so they don't inflate the absence count.
        if schedule is not None and not schedule.is_workday(day.weekday()):
            row = daily_row(db, user, day)
            if row.is_absent:
                continue
            summary.days.append(row)
            continue

        row = daily_row(db, user, day)
        if row.is_absent and not include_absent:
            continue
        summary.days.append(row)

    return summary


def scope_users(
    db: Session, viewer: User, department_id: Optional[int] = None
) -> list[User]:
    """Employees this viewer is allowed to report on."""
    stmt = (
        select(User)
        .options(selectinload(User.department), selectinload(User.schedule))
        .where(User.is_active.is_(True))
    )

    if viewer.role is Role.TEAM_LEADER:
        stmt = stmt.where(
            (User.team_leader_id == viewer.id)
            | (User.id == viewer.id)
            | (
                User.department_id.in_(
                    select(Department.id).where(
                        Department.default_team_leader_id == viewer.id
                    )
                )
            )
        )
    elif viewer.role is Role.EMPLOYEE:
        stmt = stmt.where(User.id == viewer.id)

    if department_id is not None:
        stmt = stmt.where(User.department_id == department_id)

    return list(db.scalars(stmt.order_by(User.full_name)))


def daily_report(
    db: Session, viewer: User, day: date, department_id: Optional[int] = None
) -> list[DailyRow]:
    return [
        daily_row(db, user, day) for user in scope_users(db, viewer, department_id)
    ]


def weekly_report(
    db: Session, viewer: User, anchor: date, department_id: Optional[int] = None
) -> tuple[date, date, list[PeriodSummary]]:
    start, end = week_bounds(anchor)
    return (
        start,
        end,
        [
            period_summary(db, user, start, end)
            for user in scope_users(db, viewer, department_id)
        ],
    )


def monthly_report(
    db: Session, viewer: User, anchor: date, department_id: Optional[int] = None
) -> tuple[date, date, list[PeriodSummary]]:
    start, end = month_bounds(anchor)
    return (
        start,
        end,
        [
            period_summary(db, user, start, end)
            for user in scope_users(db, viewer, department_id)
        ],
    )


def department_rollup(summaries: Iterable[PeriodSummary]) -> list[dict]:
    """Aggregate per-employee summaries into per-department totals."""
    buckets: dict[str, dict] = {}
    for summary in summaries:
        key = summary.department or "Unassigned"
        bucket = buckets.setdefault(
            key,
            {
                "department": key,
                "employees": 0,
                "worked_seconds": 0,
                "active_seconds": 0,
                "idle_seconds": 0,
                "late_days": 0,
                "days_absent": 0,
                "early_departures": 0,
            },
        )
        bucket["employees"] += 1
        bucket["worked_seconds"] += summary.worked_seconds
        bucket["active_seconds"] += summary.active_seconds
        bucket["idle_seconds"] += summary.idle_seconds
        bucket["late_days"] += summary.late_days
        bucket["days_absent"] += summary.days_absent
        bucket["early_departures"] += summary.early_departures

    rows = []
    for bucket in buckets.values():
        worked = bucket["worked_seconds"]
        rows.append(
            {
                **bucket,
                "worked_hours": format_hours(worked),
                "active_hours": format_hours(bucket["active_seconds"]),
                "idle_hours": format_hours(bucket["idle_seconds"]),
                "activity_rate": (
                    round(100 * bucket["active_seconds"] / worked, 1) if worked else 0.0
                ),
                "avg_hours_per_employee": format_hours(
                    worked // bucket["employees"] if bucket["employees"] else 0
                ),
            }
        )
    return sorted(rows, key=lambda r: r["department"])


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #
DAILY_CSV_HEADERS = [
    "Date", "Employee code", "Name", "Department", "Clock in", "Clock out",
    "Scheduled start", "Scheduled end", "Late", "Late by", "Early departure",
    "Early by", "Worked hours", "Active hours", "Idle hours", "Lunch", "Breaks",
    "Offline", "Activity %", "Sessions", "Absent", "Edited",
]

PERIOD_CSV_HEADERS = [
    "Employee code", "Name", "Department", "Period start", "Period end",
    "Days present", "Days absent", "Late days", "Early departures",
    "Worked hours", "Active hours", "Idle hours", "Lunch hours", "Break hours",
    "Activity %", "Avg daily hours",
]


def daily_csv(rows: list[DailyRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(DAILY_CSV_HEADERS)
    for row in rows:
        d = row.to_dict()
        writer.writerow([
            d["date"], d["employee_code"], d["full_name"], d["department"] or "",
            d["clock_in"], d["clock_out"], d["scheduled_start"], d["scheduled_end"],
            "Yes" if d["is_late"] else "", d["late_by"],
            "Yes" if d["is_early_departure"] else "", d["early_by"],
            d["worked_hours"], d["active_hours"], d["idle_hours"],
            d["lunch_display"], d["break_display"], d["offline_display"],
            d["activity_rate"], d["sessions"],
            "Yes" if d["is_absent"] else "", "Yes" if d["was_edited"] else "",
        ])
    return buffer.getvalue()


def period_csv(summaries: list[PeriodSummary]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(PERIOD_CSV_HEADERS)
    for summary in summaries:
        d = summary.to_dict()
        writer.writerow([
            d["employee_code"], d["full_name"], d["department"] or "",
            d["start"], d["end"], d["days_present"], d["days_absent"],
            d["late_days"], d["early_departures"], d["worked_hours"],
            d["active_hours"], d["idle_hours"], d["lunch_hours"],
            d["break_hours"], d["activity_rate"], d["avg_daily_hours"],
        ])
    return buffer.getvalue()
