"""Admin / management payloads."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import AlertSeverity, ChannelType, Role
from app.schemas.common import ORMModel


# --------------------------------------------------------------------------- #
# Departments
# --------------------------------------------------------------------------- #
class DepartmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = None
    default_schedule_id: Optional[int] = None
    default_team_leader_id: Optional[int] = None


class DepartmentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = None
    default_schedule_id: Optional[int] = None
    default_team_leader_id: Optional[int] = None
    is_active: Optional[bool] = None


class DepartmentOut(ORMModel):
    id: int
    name: str
    description: Optional[str] = None
    default_schedule_id: Optional[int] = None
    default_team_leader_id: Optional[int] = None
    is_active: bool
    member_count: int = 0


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #
class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    start_time: time = time(9, 0)
    end_time: time = time(18, 0)
    timezone: str = "UTC"
    workdays: str = Field(default="1111100", pattern=r"^[01]{7}$")
    grace_late_minutes: int = Field(default=10, ge=0, le=240)
    grace_early_minutes: int = Field(default=10, ge=0, le=240)
    expected_hours_per_day: int = Field(default=8, ge=1, le=24)

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone: {v}") from exc
        return v


class ScheduleUpdate(BaseModel):
    name: Optional[str] = None
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    timezone: Optional[str] = None
    workdays: Optional[str] = Field(default=None, pattern=r"^[01]{7}$")
    grace_late_minutes: Optional[int] = Field(default=None, ge=0, le=240)
    grace_early_minutes: Optional[int] = Field(default=None, ge=0, le=240)
    expected_hours_per_day: Optional[int] = Field(default=None, ge=1, le=24)
    is_active: Optional[bool] = None


class ScheduleOut(ORMModel):
    id: int
    name: str
    start_time: time
    end_time: time
    timezone: str
    workdays: str
    grace_late_minutes: int
    grace_early_minutes: int
    expected_hours_per_day: int
    is_active: bool


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    employee_code: str = Field(min_length=1, max_length=32)
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=160)
    password: Optional[str] = Field(default=None, min_length=10, max_length=256)
    role: Role = Role.EMPLOYEE
    department_id: Optional[int] = None
    team_leader_id: Optional[int] = None
    schedule_id: Optional[int] = None
    timezone: str = "UTC"
    phone: Optional[str] = Field(default=None, max_length=32)
    slack_user_id: Optional[str] = Field(default=None, max_length=64)


class EmployeeUpdate(BaseModel):
    employee_code: Optional[str] = Field(default=None, max_length=32)
    email: Optional[EmailStr] = None
    full_name: Optional[str] = Field(default=None, max_length=160)
    role: Optional[Role] = None
    department_id: Optional[int] = None
    team_leader_id: Optional[int] = None
    schedule_id: Optional[int] = None
    timezone: Optional[str] = None
    phone: Optional[str] = Field(default=None, max_length=32)
    slack_user_id: Optional[str] = Field(default=None, max_length=64)
    is_active: Optional[bool] = None


class EmployeeOut(ORMModel):
    id: int
    employee_code: str
    username: str
    email: EmailStr
    full_name: str
    role: str
    department_id: Optional[int] = None
    department_name: Optional[str] = None
    team_leader_id: Optional[int] = None
    team_leader_name: Optional[str] = None
    schedule_id: Optional[int] = None
    schedule_name: Optional[str] = None
    timezone: str
    phone: Optional[str] = None
    is_active: bool
    last_login_at: Optional[datetime] = None
    device_count: int = 0


class ResetPasswordRequest(BaseModel):
    new_password: Optional[str] = Field(default=None, min_length=10, max_length=256)
    require_change: bool = True


class AssignLeaderRequest(BaseModel):
    team_leader_id: Optional[int] = None


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
class PolicyUpdate(BaseModel):
    idle_threshold_seconds: Optional[int] = Field(default=None, ge=60, le=7200)
    idle_realert_seconds: Optional[int] = Field(default=None, ge=60, le=86400)
    treat_lock_as_idle: Optional[bool] = None
    short_break_max_seconds: Optional[int] = Field(default=None, ge=60, le=7200)
    lunch_break_max_seconds: Optional[int] = Field(default=None, ge=300, le=14400)
    break_overrun_grace_seconds: Optional[int] = Field(default=None, ge=0, le=3600)
    short_breaks_per_day: Optional[int] = Field(default=None, ge=0, le=20)
    lunch_breaks_per_day: Optional[int] = Field(default=None, ge=0, le=5)
    auto_end_expired_breaks: Optional[bool] = None
    heartbeat_interval_seconds: Optional[int] = Field(default=None, ge=30, le=60)
    heartbeat_grace_seconds: Optional[int] = Field(default=None, ge=60, le=1800)
    offline_alert_after_seconds: Optional[int] = Field(default=None, ge=60, le=14400)
    auto_clock_out_after_hours: Optional[int] = Field(default=None, ge=1, le=48)
    alert_on_late_arrival: Optional[bool] = None
    alert_on_early_departure: Optional[bool] = None
    alert_on_no_show: Optional[bool] = None
    no_show_after_minutes: Optional[int] = Field(default=None, ge=5, le=720)


class PolicyOut(BaseModel):
    department_id: Optional[int] = None
    idle_threshold_seconds: int
    idle_realert_seconds: int
    treat_lock_as_idle: bool
    short_break_max_seconds: int
    lunch_break_max_seconds: int
    break_overrun_grace_seconds: int
    short_breaks_per_day: int
    lunch_breaks_per_day: int
    auto_end_expired_breaks: bool
    heartbeat_interval_seconds: int
    heartbeat_grace_seconds: int
    offline_alert_after_seconds: int
    auto_clock_out_after_hours: int
    alert_on_late_arrival: bool
    alert_on_early_departure: bool
    alert_on_no_show: bool
    no_show_after_minutes: int


# --------------------------------------------------------------------------- #
# Notification channels
# --------------------------------------------------------------------------- #
class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    channel_type: ChannelType
    config: dict[str, Any] = Field(default_factory=dict)
    min_severity: AlertSeverity = AlertSeverity.WARNING
    alert_types: Optional[list[str]] = None
    department_id: Optional[int] = None
    is_enabled: bool = True


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[dict[str, Any]] = None
    min_severity: Optional[AlertSeverity] = None
    alert_types: Optional[list[str]] = None
    department_id: Optional[int] = None
    is_enabled: Optional[bool] = None


class ChannelOut(ORMModel):
    id: int
    name: str
    channel_type: str
    min_severity: str
    alert_types: Optional[list[str]] = None
    department_id: Optional[int] = None
    is_enabled: bool
    config_preview: dict[str, Any] = Field(default_factory=dict)
    last_success_at: Optional[datetime] = None
    last_error_at: Optional[datetime] = None
    last_error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Session corrections
# --------------------------------------------------------------------------- #
class SessionAdjust(BaseModel):
    clock_in_at: Optional[datetime] = None
    clock_out_at: Optional[datetime] = None
    note: str = Field(min_length=3, max_length=1000)


class ManualSessionCreate(BaseModel):
    user_id: int
    clock_in_at: datetime
    clock_out_at: datetime
    work_date: Optional[date] = None
    note: str = Field(min_length=3, max_length=1000)


class EnrollmentCodeOut(BaseModel):
    code: str
    user_id: int
    username: str
    full_name: str
    expires_at: datetime
