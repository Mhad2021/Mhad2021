"""Desktop agent API payloads."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.models.enums import BreakType, EventType


class DeviceInfo(BaseModel):
    device_uid: str = Field(min_length=8, max_length=128)
    hostname: Optional[str] = Field(default=None, max_length=160)
    platform: Optional[str] = Field(default=None, max_length=64)
    os_version: Optional[str] = Field(default=None, max_length=120)
    agent_version: Optional[str] = Field(default=None, max_length=32)


class EnrollRequest(BaseModel):
    """Bind a laptop to an employee using a one-time code from an admin."""

    enrollment_code: str = Field(min_length=6, max_length=64)
    device: DeviceInfo


class AgentLoginRequest(BaseModel):
    """Day-to-day sign-in from the tray app."""

    username: str
    password: str
    device: DeviceInfo


class AgentCredentials(BaseModel):
    device_token: str
    user_id: int
    full_name: str
    employee_code: str
    timezone: str
    team_leader: Optional[str] = None
    monitoring_notice_accepted: bool = False
    config: dict[str, Any]


class HeartbeatRequest(BaseModel):
    idle_seconds: int = Field(ge=0, le=86400)
    is_locked: bool = False
    reported_at: Optional[datetime] = None
    agent_version: Optional[str] = Field(default=None, max_length=32)
    hostname: Optional[str] = Field(default=None, max_length=160)
    platform: Optional[str] = Field(default=None, max_length=64)
    os_version: Optional[str] = Field(default=None, max_length=120)


class HeartbeatResponse(BaseModel):
    state: str
    detail: Optional[str] = None
    clocked_in: bool
    session_id: Optional[int] = None
    clock_in_at: Optional[str] = None
    state_since: Optional[str] = None
    break_: Optional[dict[str, Any]] = Field(default=None, alias="break")
    totals: Optional[dict[str, int]] = None
    breaks_used: Optional[dict[str, int]] = None
    config: dict[str, Any]
    server_time: str

    model_config = {"populate_by_name": True}


class BreakRequest(BaseModel):
    break_type: BreakType


class AgentEventRequest(BaseModel):
    event_type: EventType
    occurred_at: Optional[datetime] = None
    payload: Optional[dict[str, Any]] = None


class AgentEventBatch(BaseModel):
    """Events buffered locally while the laptop was offline."""

    events: list[AgentEventRequest] = Field(max_length=200)


class AcceptNoticeRequest(BaseModel):
    accepted: bool = True
