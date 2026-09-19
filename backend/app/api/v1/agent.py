"""Desktop agent API.

Everything here is called by the tray application on the employee's laptop.
The agent authenticates with a device token and reports only:
  - seconds since the last keyboard/mouse input (a number, never content)
  - whether the workstation is locked
  - clock-in / break actions the employee pressed
  - lifecycle events (started, stopping, sleeping, shutting down)
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import Agent, DbSession, client_ip
from app.core.security import (
    generate_token,
    hash_token,
    verify_token_hash,
)
from app.core.timeutil import ensure_aware, utcnow
from app.models import Device, EnrollmentCode, User
from app.models.enums import BreakType, ClockOutReason, EventSource, EventType
from app.schemas.agent import (
    AcceptNoticeRequest,
    AgentCredentials,
    AgentEventBatch,
    AgentEventRequest,
    AgentLoginRequest,
    BreakRequest,
    DeviceInfo,
    EnrollRequest,
    HeartbeatRequest,
)
from app.schemas.common import ActionResult
from app.services import activity, agent_session, attendance, audit
from app.services.attendance import AttendanceError
from app.services.policy import get_policy

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])


def _register_device(db, user: User, info: DeviceInfo, ip: str) -> tuple[Device, str]:
    """Create or re-key a device record, returning it with a fresh raw token."""
    raw_token = generate_token()
    device = db.scalar(select(Device).where(Device.device_uid == info.device_uid))

    if device is None:
        device = Device(user_id=user.id, device_uid=info.device_uid)
        db.add(device)
    elif device.user_id != user.id:
        # A shared laptop reassigned to a different employee.
        logger.info(
            "Device %s reassigned from user %s to %s",
            info.device_uid,
            device.user_id,
            user.id,
        )
        device.user_id = user.id

    device.token_hash = hash_token(raw_token)
    device.hostname = info.hostname
    device.platform = info.platform
    device.os_version = info.os_version
    device.agent_version = info.agent_version
    device.last_seen_at = utcnow()
    device.last_ip = ip[:64]
    device.is_active = True
    device.revoked_at = None
    db.flush()
    return device, raw_token


def _credentials(db, user: User, raw_token: str) -> AgentCredentials:
    return AgentCredentials(
        device_token=raw_token,
        user_id=user.id,
        full_name=user.full_name,
        employee_code=user.employee_code,
        timezone=user.timezone,
        team_leader=user.team_leader.full_name if user.team_leader else None,
        monitoring_notice_accepted=user.monitoring_notice_accepted_at is not None,
        config=get_policy(db, user).as_agent_config(),
    )


@router.post("/enroll", response_model=AgentCredentials)
def enroll(payload: EnrollRequest, request: Request, db: DbSession) -> AgentCredentials:
    """Bind this laptop to an employee using a one-time code issued by an admin."""
    code = payload.enrollment_code.strip().upper()
    record = db.scalar(
        select(EnrollmentCode).where(EnrollmentCode.code_hash == hash_token(code))
    )

    now = utcnow()
    if (
        record is None
        or record.used_at is not None
        or ensure_aware(record.expires_at) <= now
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That enrollment code is invalid, already used, or expired",
        )

    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=403, detail="Account is inactive")

    record.used_at = now
    device, raw_token = _register_device(db, user, payload.device, client_ip(request))

    audit.record(
        db,
        actor=None,
        action="device_enrolled",
        entity_type="device",
        entity_id=device.id,
        summary=f"{payload.device.hostname or 'A laptop'} enrolled for {user.full_name}",
        after={"device_uid": device.device_uid, "user_id": user.id},
        ip_address=client_ip(request),
    )
    activity.log_event(
        db,
        user_id=user.id,
        device_id=device.id,
        event_type=EventType.AGENT_STARTED,
        source=EventSource.AGENT,
        message="Device enrolled",
    )
    return _credentials(db, user, raw_token)


@router.post("/login", response_model=AgentCredentials)
def agent_login(
    payload: AgentLoginRequest, request: Request, db: DbSession
) -> AgentCredentials:
    """Sign in from the tray app on an already-approved laptop."""
    from app.api.v1.auth import authenticate

    user = authenticate(db, payload.username, payload.password, request)
    device, raw_token = _register_device(db, user, payload.device, client_ip(request))

    audit.record(
        db,
        actor=user,
        action="agent_login",
        entity_type="device",
        entity_id=device.id,
        summary=f"{user.username} signed in on {payload.device.hostname or 'a laptop'}",
        ip_address=client_ip(request),
    )
    return _credentials(db, user, raw_token)


@router.post("/accept-notice", response_model=ActionResult)
def accept_notice(
    payload: AcceptNoticeRequest, agent: Agent, db: DbSession
) -> ActionResult:
    """Record that the employee has read the monitoring disclosure."""
    if payload.accepted and agent.user.monitoring_notice_accepted_at is None:
        agent.user.monitoring_notice_accepted_at = utcnow()
        audit.record(
            db,
            actor=agent.user,
            action="accept_monitoring_notice",
            entity_type="user",
            entity_id=agent.user.id,
            summary=f"{agent.user.full_name} acknowledged the monitoring notice",
        )
    return ActionResult(detail="Acknowledged")


@router.get("/config")
def get_config(agent: Agent, db: DbSession) -> dict:
    policy = get_policy(db, agent.user)
    return {
        "config": policy.as_agent_config(),
        "user": {
            "full_name": agent.user.full_name,
            "employee_code": agent.user.employee_code,
            "timezone": agent.user.timezone,
            "team_leader": (
                agent.user.team_leader.full_name if agent.user.team_leader else None
            ),
        },
        "server_time": utcnow().isoformat(),
    }


@router.post("/heartbeat")
def heartbeat(payload: HeartbeatRequest, agent: Agent, request: Request, db: DbSession) -> dict:
    """The 30-60s pulse. Missing beats are what reveal a closed or killed app."""
    return agent_session.process_heartbeat(
        db,
        agent.user,
        agent.device,
        idle_seconds=payload.idle_seconds,
        is_locked=payload.is_locked,
        reported_at=payload.reported_at,
        agent_version=payload.agent_version,
        hostname=payload.hostname,
        platform=payload.platform,
        os_version=payload.os_version,
        ip_address=client_ip(request),
    )


@router.post("/clock-in")
def clock_in(agent: Agent, db: DbSession) -> dict:
    try:
        session = attendance.clock_in(db, agent.user, device=agent.device)
    except AttendanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "ok": True,
        "session_id": session.id,
        "clock_in_at": ensure_aware(session.clock_in_at).isoformat(),
        "is_late": session.is_late_arrival,
        "late_by_seconds": session.late_by_seconds,
        "detail": "Clocked in",
    }


@router.post("/clock-out")
def clock_out(agent: Agent, db: DbSession) -> dict:
    session = attendance.get_open_session(db, agent.user.id)
    if session is None:
        raise HTTPException(status_code=409, detail="You are not currently clocked in")

    attendance.clock_out(db, session, reason=ClockOutReason.MANUAL)
    return {
        "ok": True,
        "session_id": session.id,
        "clock_out_at": ensure_aware(session.clock_out_at).isoformat(),
        "worked_seconds": session.worked_seconds,
        "active_seconds": session.active_seconds,
        "idle_seconds": session.idle_seconds,
        "break_seconds": session.break_seconds,
        "is_early_departure": session.is_early_departure,
        "detail": "Clocked out",
    }


@router.post("/break/start")
def start_break(payload: BreakRequest, agent: Agent, db: DbSession) -> dict:
    session = attendance.get_open_session(db, agent.user.id)
    if session is None:
        raise HTTPException(
            status_code=409, detail="Clock in before starting a break"
        )

    try:
        period = attendance.start_break(db, session, payload.break_type)
    except AttendanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "ok": True,
        "break_id": period.id,
        "break_type": period.break_type.value,
        "started_at": ensure_aware(period.started_at).isoformat(),
        "allowed_seconds": period.allowed_seconds,
        "detail": (
            "Lunch break started"
            if period.break_type is BreakType.LUNCH
            else "Break started"
        ),
    }


@router.post("/break/end")
def end_break(agent: Agent, db: DbSession) -> dict:
    session = attendance.get_open_session(db, agent.user.id)
    if session is None:
        raise HTTPException(status_code=409, detail="You are not currently clocked in")

    period = attendance.get_open_break(db, session.id)
    if period is None:
        raise HTTPException(status_code=409, detail="No break is currently running")

    attendance.end_break(db, session, period)
    return {
        "ok": True,
        "break_id": period.id,
        "duration_seconds": period.duration_seconds,
        "overrun_seconds": period.overrun_seconds,
        "detail": "Break ended",
    }


@router.post("/events", response_model=ActionResult)
def report_event(payload: AgentEventRequest, agent: Agent, db: DbSession) -> ActionResult:
    agent_session.record_agent_event(
        db,
        agent.user,
        agent.device,
        event_type=payload.event_type,
        occurred_at=payload.occurred_at,
        payload=payload.payload,
    )
    return ActionResult(detail="Recorded")


@router.post("/events/batch", response_model=ActionResult)
def report_events(payload: AgentEventBatch, agent: Agent, db: DbSession) -> ActionResult:
    """Flush events the agent buffered locally while the network was down."""
    for event in payload.events:
        try:
            agent_session.record_agent_event(
                db,
                agent.user,
                agent.device,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                payload=event.payload,
            )
        except Exception:  # noqa: BLE001 - one bad event must not lose the batch
            logger.exception("Failed to record buffered event %s", event.event_type)
            continue
    return ActionResult(detail=f"Recorded {len(payload.events)} buffered events")


@router.get("/status")
def status_snapshot(agent: Agent, db: DbSession) -> dict:
    """What the tray app renders on startup, before the first heartbeat."""
    from app.services import presence

    row = presence.build_row(db, agent.user)
    policy = get_policy(db, agent.user)
    session = attendance.get_open_session(db, agent.user.id)

    breaks_used = {"lunch": 0, "short": 0}
    if session is not None:
        breaks_used = {
            "lunch": attendance.breaks_taken_today(
                db, agent.user.id, session.work_date, BreakType.LUNCH
            ),
            "short": attendance.breaks_taken_today(
                db, agent.user.id, session.work_date, BreakType.SHORT
            ),
        }

    return {
        **row.to_dict(agent.user.timezone),
        "breaks_used": breaks_used,
        "config": policy.as_agent_config(),
        "monitoring_notice_accepted": agent.user.monitoring_notice_accepted_at is not None,
        "server_time": utcnow().isoformat(),
    }
