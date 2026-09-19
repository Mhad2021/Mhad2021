"""FastAPI dependencies: authentication, authorisation and request context."""
from __future__ import annotations

import logging
from typing import Annotated, Optional

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.core.security import decode_access_token, hash_token
from app.core.timeutil import utcnow
from app.database import get_db
from app.models import Device, User
from app.models.enums import Role

logger = logging.getLogger(__name__)

DbSession = Annotated[Session, Depends(get_db)]

CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def client_ip(request: Request) -> str:
    """Client address, honouring one layer of reverse proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _extract_bearer(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # Server-rendered dashboard pages carry the token in a cookie instead.
    return request.cookies.get(settings.session_cookie_name)


def _load_user(db: Session, user_id: int) -> Optional[User]:
    return db.scalar(
        select(User)
        .options(
            selectinload(User.department),
            selectinload(User.team_leader),
            selectinload(User.schedule),
        )
        .where(User.id == user_id)
    )


def get_current_user(request: Request, db: DbSession) -> User:
    token = _extract_bearer(request)
    if not token:
        raise CREDENTIALS_EXC

    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired, please sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.PyJWTError as exc:
        raise CREDENTIALS_EXC from exc

    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError) as exc:
        raise CREDENTIALS_EXC from exc

    user = _load_user(db, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is inactive"
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_optional_user(request: Request, db: DbSession) -> Optional[User]:
    """For pages that render differently when signed in, without forcing login."""
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


def require_password_current(user: CurrentUser) -> User:
    """Block the rest of the app until a forced password change is done."""
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must change your password before continuing",
        )
    return user


def require_role(*allowed: Role):
    def _checker(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to do that",
            )
        return user

    return _checker


require_admin = require_role(Role.ADMIN)
require_manager = require_role(Role.ADMIN, Role.TEAM_LEADER)

AdminUser = Annotated[User, Depends(require_admin)]
ManagerUser = Annotated[User, Depends(require_manager)]


def can_view_employee(viewer: User, employee: User) -> bool:
    """Admins see everyone; leaders see their team plus their department's
    default-assigned members; employees see only themselves."""
    if viewer.role is Role.ADMIN:
        return True
    if viewer.id == employee.id:
        return True
    if viewer.role is Role.TEAM_LEADER:
        if employee.team_leader_id == viewer.id:
            return True
        dept = employee.department
        if dept is not None and dept.default_team_leader_id == viewer.id:
            return True
    return False


def get_viewable_employee(
    user_id: int, viewer: CurrentUser, db: DbSession
) -> User:
    employee = _load_user(db, user_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not can_view_employee(viewer, employee):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this employee",
        )
    return employee


# --------------------------------------------------------------------------- #
# Agent authentication
# --------------------------------------------------------------------------- #
class AgentContext:
    """The authenticated laptop plus the employee it is bound to."""

    def __init__(self, user: User, device: Device):
        self.user = user
        self.device = device


def get_agent_context(
    db: DbSession,
    request: Request,
    x_device_token: Annotated[Optional[str], Header()] = None,
) -> AgentContext:
    if not x_device_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing device token",
            headers={"WWW-Authenticate": "DeviceToken"},
        )

    device = db.scalar(
        select(Device)
        .options(selectinload(Device.user))
        .where(
            Device.token_hash == hash_token(x_device_token),
            Device.is_active.is_(True),
        )
    )
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This device is not enrolled, or its access was revoked",
        )

    user = device.user
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is inactive"
        )

    device.last_seen_at = utcnow()
    device.last_ip = client_ip(request)[:64]
    return AgentContext(user=user, device=device)


Agent = Annotated[AgentContext, Depends(get_agent_context)]
