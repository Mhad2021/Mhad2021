"""Authentication payloads."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    must_change_password: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=256)


class CurrentUser(ORMModel):
    id: int
    username: str
    email: EmailStr
    full_name: str
    employee_code: str
    role: str
    timezone: str
    department_id: Optional[int] = None
    team_leader_id: Optional[int] = None
    must_change_password: bool
    monitoring_notice_accepted_at: Optional[datetime] = None
