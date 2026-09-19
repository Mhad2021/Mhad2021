"""Employee self-service API for the browser tracking page.

These mirror the agent endpoints but authenticate with the employee's own
dashboard session rather than a device token, so no install is needed.

What is deliberately absent: an idle-time field. A browser tab can only observe
input inside itself, so it cannot distinguish "away from the desk" from
"working in another application". Accepting an idle figure from it would mean
alerting a team leader that someone is inactive while they are working, so the
signal is not collected at all rather than collected and disbelieved.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, client_ip
from app.core.timeutil import ensure_aware, utcnow
from app.models import Device, User
from app.models.enums import BreakType, ClientKind, ClockOutReason, EventSource
from app.services import attendance, presence
from app.services.attendance import AttendanceError
from app.services.policy import get_policy

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/me", tags=["employee"])

WEB_DEVICE_PREFIX = "web:"


class BreakRequest(BaseModel):
    break_type: BreakType


class WebHeartbeat(BaseModel):
    """Sent every 30-60s while the tracking page is open.

    ``page_visible`` is the only activity signal a browser can honestly give,
    and it is recorded for context rather than used to raise alerts.
    """

    page_visible: bool = True
    user_agent: Optional[str] = Field(default=None, max_length=255)


def _web_device(db: DbSession, user: User, request: Request) -> Device:
    """A stand-in device record so web sessions appear in the device list.

    One per employee rather than one per browser: the point is to show a
    manager that this person is tracking from the web, not to fingerprint
    which machine they used.
    """
    uid = f"{WEB_DEVICE_PREFIX}{user.id}"
    # Query rather than scanning user.devices: the relationship may not be
    # loaded, or may be stale from an earlier request, and inserting a second
    # row for the same uid violates the unique constraint.
    device = db.scalar(select(Device).where(Device.device_uid == uid))
    if device is None:
        device = Device(
            user_id=user.id,
            device_uid=uid,
            token_hash=f"web-session-no-token-{user.id}",
            hostname="Browser",
            platform="web",
            agent_version="web",
        )
        db.add(device)
        db.flush()

    device.last_seen_at = utcnow()
    device.last_ip = client_ip(request)[:64]
    ua = request.headers.get("user-agent")
    if ua:
        device.os_version = ua[:120]
    return device


def _snapshot(db: DbSession, user: User) -> dict[str, Any]:
    """Everything the page renders, in one response."""
    row = presence.build_row(db, user)
    policy = get_policy(db, user)
    session = attendance.get_open_session(db, user.id)

    breaks_used = {"lunch": 0, "short": 0}
    open_break = None
    if session is not None:
        breaks_used = {
            "lunch": attendance.breaks_taken_today(
                db, user.id, session.work_date, BreakType.LUNCH
            ),
            "short": attendance.breaks_taken_today(
                db, user.id, session.work_date, BreakType.SHORT
            ),
        }
        period = attendance.get_open_break(db, session.id)
        if period is not None:
            open_break = {
                "type": period.break_type.value,
                "started_at": ensure_aware(period.started_at).isoformat(),
                "allowed_seconds": period.allowed_seconds,
                "remaining_seconds": attendance.break_remaining_seconds(period),
            }

    return {
        **row.to_dict(user.timezone),
        "employee": {
            "full_name": user.full_name,
            "employee_code": user.employee_code,
            "team_leader": user.team_leader.full_name if user.team_leader else None,
        },
        "break": open_break,
        "breaks_used": breaks_used,
        "allowances": {
            "lunch_seconds": policy.lunch_break_max_seconds,
            "short_seconds": policy.short_break_max_seconds,
            "lunch_per_day": policy.lunch_breaks_per_day,
            "short_per_day": policy.short_breaks_per_day,
        },
        "heartbeat_interval_seconds": policy.heartbeat_interval_seconds,
        "server_time": utcnow().isoformat(),
    }


@router.get("/status")
def status(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    return _snapshot(db, user)


@router.post("/heartbeat")
def heartbeat(
    payload: WebHeartbeat, user: CurrentUser, db: DbSession, request: Request
) -> dict[str, Any]:
    """Record that the tracking page is still open.

    This proves the page is loaded; it does not prove the employee is working,
    and the board is worded accordingly.
    """
    device = _web_device(db, user, request)
    session = attendance.get_open_session(db, user.id)

    if session is not None:
        session.last_heartbeat_at = utcnow()
        if session.device_id is None:
            session.device_id = device.id
        if session.is_offline:
            # The tab was closed and is open again.
            from app.services.agent_session import handle_reconnect

            handle_reconnect(db, session, utcnow(), get_policy(db, user))
        db.flush()

    return _snapshot(db, user)


@router.post("/clock-in")
def clock_in(user: CurrentUser, db: DbSession, request: Request) -> dict[str, Any]:
    device = _web_device(db, user, request)
    try:
        attendance.clock_in(
            db,
            user,
            device=device,
            source=EventSource.AGENT,
            client_kind=ClientKind.WEB,
        )
    except AttendanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return _snapshot(db, user)


@router.post("/clock-out")
def clock_out(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    session = attendance.get_open_session(db, user.id)
    if session is None:
        raise HTTPException(status_code=409, detail="You are not currently clocked in")

    attendance.clock_out(db, session, reason=ClockOutReason.MANUAL)
    return _snapshot(db, user)


@router.post("/break/start")
def start_break(
    payload: BreakRequest, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    session = attendance.get_open_session(db, user.id)
    if session is None:
        raise HTTPException(status_code=409, detail="Clock in before starting a break")

    try:
        attendance.start_break(db, session, payload.break_type)
    except AttendanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return _snapshot(db, user)


@router.post("/break/end")
def end_break(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    session = attendance.get_open_session(db, user.id)
    if session is None:
        raise HTTPException(status_code=409, detail="You are not currently clocked in")

    period = attendance.get_open_break(db, session.id)
    if period is None:
        raise HTTPException(status_code=409, detail="No break is currently running")

    attendance.end_break(db, session, period)
    return _snapshot(db, user)
