"""Daily, weekly and monthly attendance reporting."""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app.api.deps import (
    AdminUser,
    CurrentUser,
    DbSession,
    ManagerUser,
    client_ip,
    get_viewable_employee,
)
from app.core.timeutil import ensure_aware, local_date, month_bounds, to_local, utcnow, week_bounds
from app.models import ActivityEvent, BreakPeriod, User, WorkSession
from app.models.enums import ClockOutReason, EventSource, EventType
from app.schemas.admin import ManualSessionCreate, SessionAdjust
from app.services import activity, audit, reporting

router = APIRouter(prefix="/reports", tags=["reports"])


def _parse_day(value: Optional[str], viewer: User) -> date:
    if not value:
        return local_date(utcnow(), viewer.timezone)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Date must be in YYYY-MM-DD format"
        ) from exc


@router.get("/daily")
def daily(
    viewer: ManagerUser,
    db: DbSession,
    day: Optional[str] = Query(default=None, alias="date"),
    department_id: Optional[int] = None,
) -> dict:
    target = _parse_day(day, viewer)
    rows = reporting.daily_report(db, viewer, target, department_id)

    present = [r for r in rows if not r.is_absent]
    return {
        "date": target.isoformat(),
        "totals": {
            "employees": len(rows),
            "present": len(present),
            "absent": len(rows) - len(present),
            "late": sum(1 for r in rows if r.is_late),
            "early_departures": sum(1 for r in rows if r.is_early_departure),
            "worked_hours": round(sum(r.worked_seconds for r in rows) / 3600, 2),
            "active_hours": round(sum(r.active_seconds for r in rows) / 3600, 2),
            "idle_hours": round(sum(r.idle_seconds for r in rows) / 3600, 2),
        },
        "rows": [r.to_dict() for r in rows],
    }


@router.get("/daily.csv", response_class=PlainTextResponse)
def daily_csv(
    viewer: ManagerUser,
    db: DbSession,
    day: Optional[str] = Query(default=None, alias="date"),
    department_id: Optional[int] = None,
) -> PlainTextResponse:
    target = _parse_day(day, viewer)
    rows = reporting.daily_report(db, viewer, target, department_id)
    return PlainTextResponse(
        reporting.daily_csv(rows),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="attendance-{target.isoformat()}.csv"'
        },
    )


@router.get("/weekly")
def weekly(
    viewer: ManagerUser,
    db: DbSession,
    day: Optional[str] = Query(default=None, alias="date"),
    department_id: Optional[int] = None,
) -> dict:
    anchor = _parse_day(day, viewer)
    start, end, summaries = reporting.weekly_report(db, viewer, anchor, department_id)
    return {
        "period": "week",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "employees": [s.to_dict() for s in summaries],
        "departments": reporting.department_rollup(summaries),
    }


@router.get("/monthly")
def monthly(
    viewer: ManagerUser,
    db: DbSession,
    day: Optional[str] = Query(default=None, alias="date"),
    department_id: Optional[int] = None,
) -> dict:
    anchor = _parse_day(day, viewer)
    start, end, summaries = reporting.monthly_report(db, viewer, anchor, department_id)
    return {
        "period": "month",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "employees": [s.to_dict() for s in summaries],
        "departments": reporting.department_rollup(summaries),
    }


@router.get("/period.csv", response_class=PlainTextResponse)
def period_csv(
    viewer: ManagerUser,
    db: DbSession,
    period: str = Query(default="week", pattern="^(week|month)$"),
    day: Optional[str] = Query(default=None, alias="date"),
    department_id: Optional[int] = None,
) -> PlainTextResponse:
    anchor = _parse_day(day, viewer)
    if period == "week":
        start, end, summaries = reporting.weekly_report(db, viewer, anchor, department_id)
    else:
        start, end, summaries = reporting.monthly_report(db, viewer, anchor, department_id)

    return PlainTextResponse(
        reporting.period_csv(summaries),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="attendance-{period}-{start.isoformat()}.csv"'
            )
        },
    )


@router.get("/employee/{user_id}")
def employee_report(
    user_id: int,
    viewer: CurrentUser,
    db: DbSession,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> dict:
    employee = get_viewable_employee(user_id, viewer, db)

    today = local_date(utcnow(), viewer.timezone)
    start_date = _parse_day(start, viewer) if start else week_bounds(today)[0]
    end_date = _parse_day(end, viewer) if end else today
    if start_date > end_date:
        raise HTTPException(status_code=422, detail="Start date must be before end date")
    if (end_date - start_date).days > 370:
        raise HTTPException(status_code=422, detail="Range cannot exceed one year")

    summary = reporting.period_summary(db, employee, start_date, end_date)
    return {
        "employee": {
            "id": employee.id,
            "full_name": employee.full_name,
            "employee_code": employee.employee_code,
            "department": employee.department.name if employee.department else None,
            "team_leader": employee.team_leader.full_name if employee.team_leader else None,
            "schedule": employee.schedule.name if employee.schedule else None,
        },
        "summary": summary.to_dict(),
        "days": [d.to_dict() for d in summary.days],
    }


@router.get("/employee/{user_id}/sessions")
def employee_sessions(
    user_id: int,
    viewer: CurrentUser,
    db: DbSession,
    day: Optional[str] = Query(default=None, alias="date"),
) -> list[dict]:
    """Every session on one day, with its breaks — the audit view."""
    employee = get_viewable_employee(user_id, viewer, db)
    target = _parse_day(day, viewer)
    tz = viewer.timezone

    sessions = db.scalars(
        select(WorkSession)
        .where(WorkSession.user_id == employee.id, WorkSession.work_date == target)
        .order_by(WorkSession.clock_in_at)
    )

    out = []
    for session in sessions:
        out.append(
            {
                "id": session.id,
                "clock_in": to_local(session.clock_in_at, tz).strftime("%H:%M:%S"),
                "clock_out": (
                    to_local(session.clock_out_at, tz).strftime("%H:%M:%S")
                    if session.clock_out_at
                    else None
                ),
                "clock_out_reason": (
                    session.clock_out_reason.value if session.clock_out_reason else None
                ),
                "is_open": session.is_open,
                "active_seconds": session.active_seconds,
                "idle_seconds": session.idle_seconds + session.locked_seconds,
                "offline_seconds": session.offline_seconds,
                "worked_seconds": session.worked_seconds,
                "is_late": session.is_late_arrival,
                "is_early_departure": session.is_early_departure,
                "was_edited": session.edited_by_id is not None,
                "note": session.notes,
                "breaks": [
                    {
                        "id": b.id,
                        "type": b.break_type.value,
                        "start": to_local(b.started_at, tz).strftime("%H:%M"),
                        "end": (
                            to_local(b.ended_at, tz).strftime("%H:%M") if b.ended_at else None
                        ),
                        "duration_seconds": b.duration_seconds,
                        "allowed_seconds": b.allowed_seconds,
                        "overrun_seconds": b.overrun_seconds,
                        "end_reason": b.end_reason.value if b.end_reason else None,
                    }
                    for b in session.breaks
                ],
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Manual corrections (admin only, always audited)
# --------------------------------------------------------------------------- #
@router.patch("/sessions/{session_id}")
def adjust_session(
    session_id: int,
    payload: SessionAdjust,
    admin: AdminUser,
    request: Request,
    db: DbSession,
) -> dict:
    """Correct a session's times. The original values stay in the audit log."""
    session = db.get(WorkSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    before = {
        "clock_in_at": ensure_aware(session.clock_in_at).isoformat(),
        "clock_out_at": (
            ensure_aware(session.clock_out_at).isoformat() if session.clock_out_at else None
        ),
    }

    if payload.clock_in_at is not None:
        session.clock_in_at = ensure_aware(payload.clock_in_at)
    if payload.clock_out_at is not None:
        session.clock_out_at = ensure_aware(payload.clock_out_at)
        if session.clock_out_reason is None:
            session.clock_out_reason = ClockOutReason.ADMIN

    if session.clock_out_at and session.clock_out_at <= session.clock_in_at:
        raise HTTPException(
            status_code=422, detail="Clock-out must be after clock-in"
        )

    session.notes = payload.note
    session.edited_by_id = admin.id
    activity.recompute_totals(db, session)
    db.flush()

    activity.log_event(
        db,
        user_id=session.user_id,
        session_id=session.id,
        event_type=EventType.CLOCK_OUT,
        occurred_at=utcnow(),
        source=EventSource.ADMIN,
        payload={"adjusted_by": admin.username, "note": payload.note},
        message=f"Session times corrected by {admin.full_name}",
    )
    audit.record(
        db,
        actor=admin,
        action="adjust_session",
        entity_type="work_session",
        entity_id=session.id,
        summary=f"Corrected session {session.id}: {payload.note}",
        before=before,
        after={
            "clock_in_at": ensure_aware(session.clock_in_at).isoformat(),
            "clock_out_at": (
                ensure_aware(session.clock_out_at).isoformat()
                if session.clock_out_at
                else None
            ),
        },
        ip_address=client_ip(request),
    )
    return {"ok": True, "session_id": session.id, "detail": "Session updated"}


@router.post("/sessions", status_code=201)
def create_manual_session(
    payload: ManualSessionCreate,
    admin: AdminUser,
    request: Request,
    db: DbSession,
) -> dict:
    """Record attendance for someone whose laptop was unavailable."""
    employee = db.get(User, payload.user_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    clock_in = ensure_aware(payload.clock_in_at)
    clock_out = ensure_aware(payload.clock_out_at)
    if clock_out <= clock_in:
        raise HTTPException(status_code=422, detail="Clock-out must be after clock-in")

    from app.services import attendance as attendance_service

    work_date = payload.work_date or local_date(clock_in, employee.timezone)
    window = attendance_service.schedule_window(db, employee, work_date)
    duration = int((clock_out - clock_in).total_seconds())

    session = WorkSession(
        user_id=employee.id,
        work_date=work_date,
        clock_in_at=clock_in,
        clock_out_at=clock_out,
        clock_out_reason=ClockOutReason.ADMIN,
        scheduled_start_at=window.start_at,
        scheduled_end_at=window.end_at,
        active_seconds=duration,
        notes=payload.note,
        edited_by_id=admin.id,
    )
    db.add(session)
    db.flush()

    activity.log_event(
        db,
        user_id=employee.id,
        session_id=session.id,
        event_type=EventType.CLOCK_IN,
        occurred_at=clock_in,
        source=EventSource.ADMIN,
        payload={"manual": True, "note": payload.note},
        message=f"Manual attendance entry by {admin.full_name}",
    )
    audit.record(
        db,
        actor=admin,
        action="create_manual_session",
        entity_type="work_session",
        entity_id=session.id,
        summary=f"Added manual session for {employee.full_name} on {work_date}",
        after={"clock_in": clock_in.isoformat(), "clock_out": clock_out.isoformat()},
        ip_address=client_ip(request),
    )
    return {"ok": True, "session_id": session.id, "detail": "Manual session recorded"}
