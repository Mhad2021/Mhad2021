"""Resolve the effective monitoring policy for a user.

Precedence: department override -> global row -> hardcoded defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Department, PolicySettings, User
from app.models.enums import BreakType


@dataclass(frozen=True)
class EffectivePolicy:
    idle_threshold_seconds: int = 600
    idle_realert_seconds: int = 1800
    treat_lock_as_idle: bool = True
    short_break_max_seconds: int = 600
    lunch_break_max_seconds: int = 3600
    break_overrun_grace_seconds: int = 120
    short_breaks_per_day: int = 2
    lunch_breaks_per_day: int = 1
    auto_end_expired_breaks: bool = True
    heartbeat_interval_seconds: int = 45
    heartbeat_grace_seconds: int = 150
    offline_alert_after_seconds: int = 600
    auto_clock_out_after_hours: int = 14
    alert_on_late_arrival: bool = True
    alert_on_early_departure: bool = True

    def allowance_for(self, break_type: BreakType) -> int:
        return (
            self.lunch_break_max_seconds
            if break_type is BreakType.LUNCH
            else self.short_break_max_seconds
        )

    def daily_limit_for(self, break_type: BreakType) -> int:
        return (
            self.lunch_breaks_per_day
            if break_type is BreakType.LUNCH
            else self.short_breaks_per_day
        )

    def as_agent_config(self) -> dict:
        """The subset the desktop agent needs to render countdowns locally."""
        return {
            "idle_threshold_seconds": self.idle_threshold_seconds,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "short_break_max_seconds": self.short_break_max_seconds,
            "lunch_break_max_seconds": self.lunch_break_max_seconds,
            "short_breaks_per_day": self.short_breaks_per_day,
            "lunch_breaks_per_day": self.lunch_breaks_per_day,
            "treat_lock_as_idle": self.treat_lock_as_idle,
        }


_POLICY_FIELDS = {f.name for f in fields(EffectivePolicy)}


def _row_to_kwargs(row: PolicySettings | None) -> dict:
    if row is None:
        return {}
    return {
        name: getattr(row, name)
        for name in _POLICY_FIELDS
        if getattr(row, name, None) is not None
    }


def get_policy(db: Session, user: Optional[User] = None) -> EffectivePolicy:
    """Effective policy for ``user`` (or the global policy when user is None)."""
    global_row = db.scalar(
        select(PolicySettings).where(PolicySettings.department_id.is_(None))
    )
    merged = _row_to_kwargs(global_row)

    if user is not None and user.department_id is not None:
        dept_row = db.scalar(
            select(PolicySettings).where(
                PolicySettings.department_id == user.department_id
            )
        )
        merged.update(_row_to_kwargs(dept_row))

    return EffectivePolicy(**merged)


def get_policy_for_department(db: Session, department_id: int | None) -> EffectivePolicy:
    global_row = db.scalar(
        select(PolicySettings).where(PolicySettings.department_id.is_(None))
    )
    merged = _row_to_kwargs(global_row)
    if department_id is not None:
        dept_row = db.scalar(
            select(PolicySettings).where(PolicySettings.department_id == department_id)
        )
        merged.update(_row_to_kwargs(dept_row))
    return EffectivePolicy(**merged)


def ensure_global_policy(db: Session) -> PolicySettings:
    """Create the global policy row if the install has none yet."""
    row = db.scalar(select(PolicySettings).where(PolicySettings.department_id.is_(None)))
    if row is None:
        row = PolicySettings(department_id=None)
        db.add(row)
        db.flush()
    return row


def resolve_alert_recipient(db: Session, employee: User) -> Optional[User]:
    """Who gets told when this employee goes idle or drops offline.

    Direct team leader first; then the department's default leader; finally any
    active admin, so an alert is never silently dropped.
    """
    if employee.team_leader_id and employee.team_leader and employee.team_leader.is_active:
        return employee.team_leader

    if employee.department_id:
        dept = db.get(Department, employee.department_id)
        if dept and dept.default_team_leader and dept.default_team_leader.is_active:
            return dept.default_team_leader

    from app.models.enums import Role

    return db.scalar(
        select(User)
        .where(User.role == Role.ADMIN, User.is_active.is_(True))
        .order_by(User.id)
        .limit(1)
    )
