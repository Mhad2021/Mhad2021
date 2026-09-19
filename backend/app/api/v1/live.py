"""Live status board and event stream."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, DbSession, ManagerUser, get_viewable_employee
from app.config import settings
from app.core.timeutil import ensure_aware, humanize_duration, to_local, utcnow
from app.database import SessionLocal
from app.models import ActivityEvent
from app.services import alerts, attendance, presence

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/live", tags=["live"])


@router.get("/board")
def live_board(
    viewer: ManagerUser,
    db: DbSession,
    department_id: Optional[int] = None,
    state: Optional[str] = Query(default=None),
) -> dict:
    """Every visible employee's current status and how long they've held it."""
    rows = presence.live_board(db, viewer, department_id=department_id, state_filter=state)
    return {
        "generated_at": utcnow().isoformat(),
        "summary": presence.board_summary(rows),
        "employees": [r.to_dict(viewer.timezone) for r in rows],
    }


@router.get("/me")
def my_status(viewer: CurrentUser, db: DbSession) -> dict:
    """What the employee dashboard and the tray app both render."""
    row = presence.build_row(db, viewer)
    session = attendance.get_open_session(db, viewer.id)

    recent_events = []
    if session is not None:
        events = db.query(ActivityEvent).filter(
            ActivityEvent.session_id == session.id
        ).order_by(ActivityEvent.occurred_at.desc()).limit(15).all()
        recent_events = [
            {
                "type": e.event_type.value,
                "at": to_local(e.occurred_at, viewer.timezone).strftime("%H:%M"),
                "message": e.message or e.event_type.value.replace("_", " ").title(),
            }
            for e in events
        ]

    return {
        **row.to_dict(viewer.timezone),
        "recent_events": recent_events,
        "server_time": utcnow().isoformat(),
    }


@router.get("/employee/{user_id}")
def employee_detail(user_id: int, viewer: CurrentUser, db: DbSession) -> dict:
    """Drill-down: current status plus today's timeline."""
    employee = get_viewable_employee(user_id, viewer, db)
    row = presence.build_row(db, employee)
    session = attendance.get_open_session(db, employee.id)

    timeline = []
    if session is not None:
        for interval in sorted(
            session.intervals, key=lambda i: ensure_aware(i.started_at)
        ):
            timeline.append(
                {
                    "state": interval.state.value,
                    "from": to_local(interval.started_at, viewer.timezone).strftime("%H:%M"),
                    "to": (
                        to_local(interval.ended_at, viewer.timezone).strftime("%H:%M")
                        if interval.ended_at
                        else "now"
                    ),
                    "duration": humanize_duration(
                        interval.duration_seconds
                        if interval.ended_at
                        else int((utcnow() - ensure_aware(interval.started_at)).total_seconds())
                    ),
                }
            )

    events = db.query(ActivityEvent).filter(
        ActivityEvent.user_id == employee.id
    ).order_by(ActivityEvent.occurred_at.desc()).limit(40).all()

    return {
        "employee": row.to_dict(viewer.timezone),
        "timeline": timeline,
        "events": [
            {
                "type": e.event_type.value,
                "source": e.source.value,
                "at": to_local(e.occurred_at, viewer.timezone).strftime("%d %b %H:%M"),
                "message": e.message or e.event_type.value.replace("_", " ").title(),
                "payload": e.payload,
            }
            for e in events
        ],
        "open_alerts": [
            {
                "id": a.id,
                "type": a.alert_type.value,
                "title": a.title,
                "message": a.message,
                "severity": a.severity.value,
                "at": to_local(a.triggered_at, viewer.timezone).strftime("%d %b %H:%M"),
            }
            for a in alerts.open_alerts_for(db, viewer)
            if a.subject_id == employee.id
        ],
    }


@router.get("/stream")
async def stream(request: Request, viewer: ManagerUser) -> StreamingResponse:
    """Server-sent events pushing a fresh board snapshot on an interval.

    A short poll inside the server is deliberate: it keeps one query path for
    the board, and at 200 employees the snapshot is a few hundred rows.
    """
    viewer_id = viewer.id
    viewer_tz = viewer.timezone

    async def generator():
        while True:
            if await request.is_disconnected():
                break

            def snapshot() -> str:
                db = SessionLocal()
                try:
                    from app.models import User

                    user = db.get(User, viewer_id)
                    if user is None:
                        return json.dumps({"error": "session ended"})
                    rows = presence.live_board(db, user)
                    return json.dumps(
                        {
                            "generated_at": utcnow().isoformat(),
                            "summary": presence.board_summary(rows),
                            "employees": [r.to_dict(viewer_tz) for r in rows],
                        }
                    )
                finally:
                    db.close()

            try:
                payload = await asyncio.to_thread(snapshot)
                yield f"event: board\ndata: {payload}\n\n"
            except Exception:  # noqa: BLE001 - never kill the stream on one error
                logger.exception("Live stream snapshot failed")
                yield 'event: error\ndata: {"error":"snapshot failed"}\n\n'

            await asyncio.sleep(settings.sse_poll_seconds)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
