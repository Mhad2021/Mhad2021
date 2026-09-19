"""Authentication endpoints for the web dashboards."""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, client_ip
from app.config import settings
from app.core.security import (
    create_access_token,
    generate_token,
    hash_password,
    hash_token,
    password_problems,
    refresh_token_expiry,
    verify_password,
)
from app.core.timeutil import utcnow
from app.models import RefreshToken, User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    TokenPair,
)
from app.schemas.auth import (
    CurrentUser as CurrentUserOut,
)
from app.schemas.common import ActionResult
from app.services import audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Incorrect username or password",
)


def _issue_tokens(
    db, user: User, request: Request, response: Response | None = None
) -> TokenPair:
    access = create_access_token(user.id, user.role.value)
    raw_refresh = generate_token()

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(raw_refresh),
            expires_at=refresh_token_expiry(),
            user_agent=(request.headers.get("user-agent") or "")[:255] or None,
            ip_address=client_ip(request),
        )
    )

    if response is not None:
        response.set_cookie(
            settings.session_cookie_name,
            access,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            max_age=settings.access_token_ttl_minutes * 60,
            path="/",
        )

    return TokenPair(
        access_token=access,
        refresh_token=raw_refresh,
        expires_in=settings.access_token_ttl_minutes * 60,
        must_change_password=user.must_change_password,
    )


def authenticate(db, username: str, password: str, request: Request) -> User:
    """Verify credentials with lockout on repeated failures."""
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        # Spend comparable time on unknown users so the response does not leak
        # whether the username exists.
        verify_password(password, hash_password("dummy-timing-equaliser"))
        raise INVALID_CREDENTIALS

    now = utcnow()
    if user.locked_until and user.locked_until > now:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=(
                "Too many failed sign-in attempts. "
                f"Try again in {settings.lockout_minutes} minutes."
            ),
        )

    if not verify_password(password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= settings.max_failed_logins:
            user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
            logger.warning("Locked account %s after repeated failures", user.username)
        db.flush()
        raise INVALID_CREDENTIALS

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated. Contact your administrator.",
        )

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    db.flush()
    return user


@router.post("/login", response_model=TokenPair)
def login(
    payload: LoginRequest, request: Request, response: Response, db: DbSession
) -> TokenPair:
    user = authenticate(db, payload.username, payload.password, request)
    audit.record(
        db,
        actor=user,
        action="login",
        entity_type="user",
        entity_id=user.id,
        summary=f"{user.username} signed in to the dashboard",
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _issue_tokens(db, user, request, response)


@router.post("/refresh", response_model=TokenPair)
def refresh(
    payload: RefreshRequest, request: Request, response: Response, db: DbSession
) -> TokenPair:
    token_hash = hash_token(payload.refresh_token)
    record = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))

    now = utcnow()
    if record is None or record.revoked_at is not None or record.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired, please sign in again",
        )

    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is inactive")

    # Rotate: the presented token is burned as the replacement is issued.
    record.revoked_at = now
    return _issue_tokens(db, user, request, response)


@router.post("/logout", response_model=ActionResult)
def logout(
    request: Request, response: Response, db: DbSession, user: CurrentUser
) -> ActionResult:
    now = utcnow()
    for token in db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    ):
        token.revoked_at = now

    response.delete_cookie(settings.session_cookie_name, path="/")
    return ActionResult(detail="Signed out")


@router.get("/me", response_model=CurrentUserOut)
def me(user: CurrentUser) -> User:
    return user


@router.post("/change-password", response_model=ActionResult)
def change_password(
    payload: ChangePasswordRequest, request: Request, db: DbSession, user: CurrentUser
) -> ActionResult:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    problems = password_problems(payload.new_password)
    if problems:
        raise HTTPException(
            status_code=422, detail="Password " + ", ".join(problems)
        )
    if payload.new_password == payload.current_password:
        raise HTTPException(
            status_code=400, detail="New password must be different from the current one"
        )

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False

    # Force every other session to re-authenticate with the new password.
    now = utcnow()
    for token in db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    ):
        token.revoked_at = now

    audit.record(
        db,
        actor=user,
        action="change_password",
        entity_type="user",
        entity_id=user.id,
        summary=f"{user.username} changed their password",
        ip_address=client_ip(request),
    )
    return ActionResult(detail="Password updated")
