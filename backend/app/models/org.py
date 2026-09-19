"""Organisational structure: departments, work schedules, policy settings."""
from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class Department(Base, TimestampMixin):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    default_schedule_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("work_schedules.id", ondelete="SET NULL")
    )
    # Fallback recipient when an employee has no team leader assigned.
    # users.department_id points back here, so this side of the cycle is
    # created with ALTER TABLE after both tables exist.
    default_team_leader_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL", use_alter=True,
                   name="fk_departments_default_team_leader_id_users")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    members: Mapped[list["User"]] = relationship(
        back_populates="department", foreign_keys="User.department_id"
    )
    default_schedule: Mapped[Optional["WorkSchedule"]] = relationship(
        foreign_keys=[default_schedule_id]
    )
    default_team_leader: Mapped[Optional["User"]] = relationship(
        foreign_keys=[default_team_leader_id]
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Department {self.name}>"


class WorkSchedule(Base, TimestampMixin):
    """A normal working pattern, e.g. Mon-Fri 09:00-18:00 Europe/London.

    Late arrival and early departure are derived by comparing the clock-in and
    clock-out times against this schedule in the schedule's own timezone.
    """

    __tablename__ = "work_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(9, 0))
    end_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(18, 0))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)

    # Mon=0 .. Sun=6, stored as a 7-char string of "1"/"0" for portability.
    workdays: Mapped[str] = mapped_column(String(7), default="1111100", nullable=False)

    grace_late_minutes: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    grace_early_minutes: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    expected_hours_per_day: Mapped[int] = mapped_column(
        Integer, default=8, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def is_workday(self, weekday: int) -> bool:
        """weekday: Monday=0 ... Sunday=6 (matches datetime.weekday())."""
        if not 0 <= weekday <= 6:
            return False
        return self.workdays[weekday] == "1"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<WorkSchedule {self.name} {self.start_time}-{self.end_time}>"


class PolicySettings(Base, TimestampMixin):
    """Monitoring policy. One global row (department_id NULL) plus optional
    per-department overrides. Admin-editable from the dashboard."""

    __tablename__ = "policy_settings"
    __table_args__ = (UniqueConstraint("department_id", name="uq_policy_department"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True
    )

    # --- Inactivity ---------------------------------------------------------
    idle_threshold_seconds: Mapped[int] = mapped_column(
        Integer, default=600, nullable=False  # 10 minutes
    )
    # Wait this long after an idle alert before alerting again for the same run.
    idle_realert_seconds: Mapped[int] = mapped_column(
        Integer, default=1800, nullable=False
    )
    treat_lock_as_idle: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    # --- Breaks -------------------------------------------------------------
    short_break_max_seconds: Mapped[int] = mapped_column(
        Integer, default=600, nullable=False  # 10 minutes
    )
    lunch_break_max_seconds: Mapped[int] = mapped_column(
        Integer, default=3600, nullable=False  # 60 minutes
    )
    break_overrun_grace_seconds: Mapped[int] = mapped_column(
        Integer, default=120, nullable=False
    )
    short_breaks_per_day: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    lunch_breaks_per_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    auto_end_expired_breaks: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    # --- Heartbeat / offline ------------------------------------------------
    heartbeat_interval_seconds: Mapped[int] = mapped_column(
        Integer, default=45, nullable=False
    )
    # Heartbeat considered missing after this long (≈3 missed beats).
    heartbeat_grace_seconds: Mapped[int] = mapped_column(
        Integer, default=150, nullable=False
    )
    # Alert the team leader once offline this long.
    offline_alert_after_seconds: Mapped[int] = mapped_column(
        Integer, default=600, nullable=False
    )

    # --- Session hygiene ----------------------------------------------------
    auto_clock_out_after_hours: Mapped[int] = mapped_column(
        Integer, default=14, nullable=False
    )
    alert_on_late_arrival: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    alert_on_early_departure: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # Off by default: teams with flexible hours would find it noisy, and an
    # alert nobody wants is an alert everybody learns to ignore.
    alert_on_no_show: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    no_show_after_minutes: Mapped[int] = mapped_column(
        Integer, default=60, nullable=False
    )

    department: Mapped[Optional["Department"]] = relationship()

    def __repr__(self) -> str:  # pragma: no cover
        scope = f"dept={self.department_id}" if self.department_id else "global"
        return f"<PolicySettings {scope}>"
