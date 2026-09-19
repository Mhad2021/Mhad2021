"""Server-rendered dashboards.

Three role-scoped views share one template set: Admin, Team Leader and
Employee. The live board refreshes over HTMX so there is no separate SPA
build step to deploy.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.api.deps import (
    CurrentUser,
    DbSession,
    can_view_employee,
    client_ip,
    get_current_user,
    require_admin,
    require_manager,
)
from app.config import settings
from app.core.timeutil import (
    humanize_duration,
    local_date,
    month_bounds,
    to_local,
    utcnow,
    week_bounds,
)
from app.models import Alert, Department, NotificationChannel, User, WorkSchedule
from app.models.enums import PresenceState, Role
from app.services import alerts as alert_service, audit, directory, presence, reporting
from app.services.policy import get_policy_for_department

logger = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.filters["duration"] = humanize_duration
templates.env.filters["hours"] = lambda s: f"{max(0, s or 0) / 3600:.2f}"
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["state_meta"] = presence.STATE_META
templates.env.globals["PresenceState"] = PresenceState


def _ctx(request: Request, user: Optional[User], **extra) -> dict:
    return {"request": request, "user": user, "now": utcnow(), **extra}


def _redirect_home(user: User) -> RedirectResponse:
    target = "/team/live" if user.is_manager else "/me"
    return RedirectResponse(url=target, status_code=303)


# --------------------------------------------------------------------------- #
# Auth pages
# --------------------------------------------------------------------------- #
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> Response:
    return templates.TemplateResponse(
        "login.html", _ctx(request, None, next=next, error=None)
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    db: DbSession,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
) -> Response:
    from app.api.v1.auth import authenticate, _issue_tokens

    try:
        user = authenticate(db, username, password, request)
    except HTTPException as exc:
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, None, next=next, error=exc.detail),
            status_code=exc.status_code if exc.status_code != 423 else 200,
        )

    destination = next if next and next.startswith("/") else "/"
    if user.must_change_password:
        destination = "/change-password"
    elif destination == "/":
        destination = "/team/live" if user.is_manager else "/me"

    response = RedirectResponse(url=destination, status_code=303)
    _issue_tokens(db, user, request, response)
    audit.record(
        db,
        actor=user,
        action="login",
        entity_type="user",
        entity_id=user.id,
        summary=f"{user.username} signed in",
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return response


@router.get("/logout")
def logout_page(request: Request) -> Response:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return response


@router.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request, user: CurrentUser) -> Response:
    return templates.TemplateResponse(
        "change_password.html", _ctx(request, user, error=None, success=False)
    )


@router.post("/change-password", response_class=HTMLResponse)
def change_password_submit(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> Response:
    from app.api.v1.auth import ChangePasswordRequest, change_password

    if new_password != confirm_password:
        return templates.TemplateResponse(
            "change_password.html",
            _ctx(request, user, error="The two passwords do not match", success=False),
        )

    try:
        change_password(
            ChangePasswordRequest(
                current_password=current_password, new_password=new_password
            ),
            request,
            db,
            user,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "change_password.html",
            _ctx(request, user, error=exc.detail, success=False),
        )

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return response


@router.get("/", response_class=HTMLResponse)
def home(request: Request, user: CurrentUser) -> Response:
    if user.must_change_password:
        return RedirectResponse(url="/change-password", status_code=303)
    return _redirect_home(user)


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request, db: DbSession, user: CurrentUser) -> Response:
    policy = get_policy_for_department(db, user.department_id)
    return templates.TemplateResponse("privacy.html", _ctx(request, user, policy=policy))


# --------------------------------------------------------------------------- #
# Employee dashboard
# --------------------------------------------------------------------------- #
@router.get("/me", response_class=HTMLResponse)
def my_dashboard(request: Request, db: DbSession, user: CurrentUser) -> Response:
    from app.api.v1.live import my_status

    today = local_date(utcnow(), user.timezone)
    week_start, week_end = week_bounds(today)

    return templates.TemplateResponse(
        "employee/dashboard.html",
        _ctx(
            request,
            user,
            status=my_status(user, db),
            today=reporting.daily_row(db, user, today),
            week=reporting.period_summary(db, user, week_start, week_end),
            policy=get_policy_for_department(db, user.department_id),
            week_start=week_start,
            week_end=week_end,
        ),
    )


@router.get("/partials/my-status", response_class=HTMLResponse)
def my_status_partial(request: Request, db: DbSession, user: CurrentUser) -> Response:
    from app.api.v1.live import my_status

    return templates.TemplateResponse(
        "partials/my_status.html", _ctx(request, user, status=my_status(user, db))
    )


# --------------------------------------------------------------------------- #
# Manager live board
# --------------------------------------------------------------------------- #
@router.get("/team/live", response_class=HTMLResponse)
def live_board_page(
    request: Request,
    db: DbSession,
    user: User = Depends(require_manager),
    department_id: Optional[int] = None,
    state: Optional[str] = None,
) -> Response:
    rows = presence.live_board(db, user, department_id=department_id, state_filter=state)
    return templates.TemplateResponse(
        "team/live.html",
        _ctx(
            request,
            user,
            rows=rows,
            summary=presence.board_summary(rows),
            departments=list(db.scalars(select(Department).order_by(Department.name))),
            selected_department=department_id,
            selected_state=state,
            open_alert_count=len(alert_service.open_alerts_for(db, user)),
        ),
    )


@router.get("/partials/board", response_class=HTMLResponse)
def board_partial(
    request: Request,
    db: DbSession,
    user: User = Depends(require_manager),
    department_id: Optional[int] = None,
    state: Optional[str] = None,
) -> Response:
    rows = presence.live_board(db, user, department_id=department_id, state_filter=state)
    return templates.TemplateResponse(
        "partials/board.html",
        _ctx(request, user, rows=rows, summary=presence.board_summary(rows)),
    )


@router.get("/team/employee/{user_id}", response_class=HTMLResponse)
def employee_detail_page(
    user_id: int, request: Request, db: DbSession, user: CurrentUser
) -> Response:
    from app.api.v1.live import employee_detail

    employee = db.get(User, user_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not can_view_employee(user, employee):
        raise HTTPException(status_code=403, detail="No access to this employee")

    today = local_date(utcnow(), user.timezone)
    week_start, week_end = week_bounds(today)

    return templates.TemplateResponse(
        "team/employee.html",
        _ctx(
            request,
            user,
            employee=employee,
            detail=employee_detail(user_id, user, db),
            today=reporting.daily_row(db, employee, today),
            week=reporting.period_summary(db, employee, week_start, week_end),
        ),
    )


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(
    request: Request,
    db: DbSession,
    user: User = Depends(require_manager),
    show_all: bool = False,
) -> Response:
    from app.api.v1.alerts import _serialise

    total, rows = alert_service.query(db, user, only_open=not show_all, limit=200)
    return templates.TemplateResponse(
        "team/alerts.html",
        _ctx(
            request,
            user,
            alerts=[_serialise(a, user.timezone, db) for a in rows],
            total=total,
            show_all=show_all,
        ),
    )


@router.get("/alerts/{alert_id}", response_class=HTMLResponse)
def alert_detail_page(
    alert_id: int, request: Request, db: DbSession, user: CurrentUser
) -> Response:
    from app.api.v1.alerts import get_alert

    return templates.TemplateResponse(
        "team/alert_detail.html",
        _ctx(request, user, alert=get_alert(alert_id, user, db)),
    )


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    db: DbSession,
    user: User = Depends(require_manager),
    period: str = Query(default="daily", pattern="^(daily|weekly|monthly)$"),
    day: Optional[str] = Query(default=None, alias="date"),
    department_id: Optional[int] = None,
) -> Response:
    try:
        anchor = date.fromisoformat(day) if day else local_date(utcnow(), user.timezone)
    except ValueError:
        anchor = local_date(utcnow(), user.timezone)

    context = {
        "period": period,
        "anchor": anchor,
        "departments": list(db.scalars(select(Department).order_by(Department.name))),
        "selected_department": department_id,
    }

    if period == "daily":
        rows = reporting.daily_report(db, user, anchor, department_id)
        context["rows"] = rows
        context["totals"] = {
            "present": sum(1 for r in rows if not r.is_absent),
            "absent": sum(1 for r in rows if r.is_absent),
            "late": sum(1 for r in rows if r.is_late),
            "early": sum(1 for r in rows if r.is_early_departure),
            "worked_seconds": sum(r.worked_seconds for r in rows),
            "active_seconds": sum(r.active_seconds for r in rows),
            "idle_seconds": sum(r.idle_seconds for r in rows),
        }
    else:
        builder = reporting.weekly_report if period == "weekly" else reporting.monthly_report
        start, end, summaries = builder(db, user, anchor, department_id)
        context.update(
            {
                "start": start,
                "end": end,
                "summaries": summaries,
                "department_rollup": reporting.department_rollup(summaries),
            }
        )

    return templates.TemplateResponse("team/reports.html", _ctx(request, user, **context))


@router.get("/reports/employee/{user_id}", response_class=HTMLResponse)
def employee_report_page(
    user_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Response:
    employee = db.get(User, user_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not can_view_employee(user, employee):
        raise HTTPException(status_code=403, detail="No access to this employee")

    today = local_date(utcnow(), user.timezone)
    try:
        start_date = date.fromisoformat(start) if start else month_bounds(today)[0]
        end_date = date.fromisoformat(end) if end else today
    except ValueError:
        start_date, end_date = month_bounds(today)[0], today

    return templates.TemplateResponse(
        "team/employee_report.html",
        _ctx(
            request,
            user,
            employee=employee,
            summary=reporting.period_summary(db, employee, start_date, end_date),
            start=start_date,
            end=end_date,
        ),
    )


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
@router.get("/admin/employees", response_class=HTMLResponse)
def admin_employees(
    request: Request,
    db: DbSession,
    user: User = Depends(require_admin),
    search: Optional[str] = None,
    include_inactive: bool = False,
) -> Response:
    from app.api.v1.employees import _to_employee_out

    employees = directory.list_employees(
        db, user, include_inactive=include_inactive, search=search
    )
    return templates.TemplateResponse(
        "admin/employees.html",
        _ctx(
            request,
            user,
            employees=[_to_employee_out(db, e) for e in employees],
            departments=list(db.scalars(select(Department).order_by(Department.name))),
            schedules=list(db.scalars(select(WorkSchedule).order_by(WorkSchedule.name))),
            leaders=directory.managers(db),
            search=search,
            include_inactive=include_inactive,
        ),
    )


@router.get("/admin/organisation", response_class=HTMLResponse)
def admin_organisation(
    request: Request, db: DbSession, user: User = Depends(require_admin)
) -> Response:
    departments = list(db.scalars(select(Department).order_by(Department.name)))
    counts = {
        row[0]: row[1]
        for row in db.execute(
            select(User.department_id, func.count(User.id))
            .where(User.is_active.is_(True))
            .group_by(User.department_id)
        ).all()
    }
    return templates.TemplateResponse(
        "admin/organisation.html",
        _ctx(
            request,
            user,
            departments=departments,
            member_counts=counts,
            schedules=list(db.scalars(select(WorkSchedule).order_by(WorkSchedule.name))),
            leaders=directory.managers(db),
        ),
    )


@router.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(
    request: Request,
    db: DbSession,
    user: User = Depends(require_admin),
    department_id: Optional[int] = None,
) -> Response:
    return templates.TemplateResponse(
        "admin/settings.html",
        _ctx(
            request,
            user,
            policy=get_policy_for_department(db, department_id),
            departments=list(db.scalars(select(Department).order_by(Department.name))),
            selected_department=department_id,
        ),
    )


@router.get("/admin/integrations", response_class=HTMLResponse)
def admin_integrations(
    request: Request, db: DbSession, user: User = Depends(require_admin)
) -> Response:
    from app.api.v1.settings_api import channel_types, list_channels

    return templates.TemplateResponse(
        "admin/integrations.html",
        _ctx(
            request,
            user,
            channels=list_channels(user, db),
            catalog=channel_types(user),
            departments=list(db.scalars(select(Department).order_by(Department.name))),
        ),
    )


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(
    request: Request,
    db: DbSession,
    user: User = Depends(require_admin),
    entity_type: Optional[str] = None,
) -> Response:
    entries = audit.query(db, entity_type=entity_type, limit=300)
    return templates.TemplateResponse(
        "admin/audit.html",
        _ctx(
            request,
            user,
            entries=[
                {
                    "at": to_local(e.occurred_at, user.timezone).strftime(
                        "%d %b %Y, %H:%M:%S"
                    ),
                    "actor": e.actor_label,
                    "action": e.action,
                    "entity_type": e.entity_type,
                    "entity_id": e.entity_id,
                    "summary": e.summary,
                    "ip_address": e.ip_address,
                }
                for e in entries
            ],
            entity_type=entity_type,
        ),
    )
