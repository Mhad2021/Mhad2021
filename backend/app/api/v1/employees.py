"""Employee, department and schedule management."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.api.deps import AdminUser, CurrentUser, DbSession, ManagerUser, client_ip
from app.core.security import (
    enrollment_code_expiry,
    generate_enrollment_code,
    generate_temp_password,
    hash_password,
    hash_token,
    password_problems,
)
from app.core.timeutil import utcnow
from app.models import Department, Device, EnrollmentCode, User, WorkSchedule
from app.models.enums import Role
from app.schemas.admin import (
    AssignLeaderRequest,
    DepartmentCreate,
    DepartmentOut,
    DepartmentUpdate,
    EmployeeCreate,
    EmployeeOut,
    EmployeeUpdate,
    EnrollmentCodeOut,
    ResetPasswordRequest,
    ScheduleCreate,
    ScheduleOut,
    ScheduleUpdate,
)
from app.schemas.common import ActionResult
from app.services import audit, directory

logger = logging.getLogger(__name__)
router = APIRouter(tags=["management"])

_EMPLOYEE_AUDIT_FIELDS = [
    "employee_code", "username", "email", "full_name", "role",
    "department_id", "team_leader_id", "schedule_id", "timezone", "is_active",
]


def _to_employee_out(db, user: User) -> EmployeeOut:
    device_count = (
        db.scalar(
            select(func.count(Device.id)).where(
                Device.user_id == user.id, Device.is_active.is_(True)
            )
        )
        or 0
    )
    return EmployeeOut(
        id=user.id,
        employee_code=user.employee_code,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        department_id=user.department_id,
        department_name=user.department.name if user.department else None,
        team_leader_id=user.team_leader_id,
        team_leader_name=user.team_leader.full_name if user.team_leader else None,
        schedule_id=user.schedule_id,
        schedule_name=user.schedule.name if user.schedule else None,
        timezone=user.timezone,
        phone=user.phone,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        device_count=device_count,
    )


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@router.get("/employees", response_model=list[EmployeeOut])
def list_employees(
    viewer: ManagerUser,
    db: DbSession,
    department_id: Optional[int] = None,
    include_inactive: bool = False,
    search: Optional[str] = Query(default=None, max_length=120),
) -> list[EmployeeOut]:
    employees = directory.list_employees(
        db,
        viewer,
        department_id=department_id,
        include_inactive=include_inactive,
        search=search,
    )
    return [_to_employee_out(db, u) for u in employees]


@router.post("/employees", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED)
def create_employee(
    payload: EmployeeCreate, admin: AdminUser, request: Request, db: DbSession
) -> EmployeeOut:
    username = payload.username.strip().lower()

    if db.scalar(select(User.id).where(User.username == username)):
        raise HTTPException(status_code=409, detail="That username is already taken")
    if db.scalar(select(User.id).where(User.email == payload.email.lower())):
        raise HTTPException(status_code=409, detail="That email is already registered")
    if db.scalar(select(User.id).where(User.employee_code == payload.employee_code)):
        raise HTTPException(status_code=409, detail="That employee code is already in use")

    # A generated password is handed to the employee out of band; they are
    # forced to change it on first sign-in.
    raw_password = payload.password or generate_temp_password()
    problems = password_problems(raw_password)
    if payload.password and problems:
        raise HTTPException(status_code=422, detail="Password " + ", ".join(problems))

    if payload.team_leader_id:
        leader = db.get(User, payload.team_leader_id)
        if leader is None or leader.role is Role.EMPLOYEE:
            raise HTTPException(
                status_code=400,
                detail="Assigned team leader must be a team leader or admin",
            )

    user = User(
        employee_code=payload.employee_code.strip(),
        username=username,
        email=payload.email.lower(),
        full_name=payload.full_name.strip(),
        password_hash=hash_password(raw_password),
        role=payload.role,
        department_id=payload.department_id,
        team_leader_id=payload.team_leader_id,
        schedule_id=payload.schedule_id,
        timezone=payload.timezone,
        phone=payload.phone,
        slack_user_id=payload.slack_user_id,
        must_change_password=True,
    )
    db.add(user)
    db.flush()

    audit.record(
        db,
        actor=admin,
        action="create_employee",
        entity_type="user",
        entity_id=user.id,
        summary=f"Created {user.full_name} ({user.role.value})",
        after=audit.model_snapshot(user, _EMPLOYEE_AUDIT_FIELDS),
        ip_address=client_ip(request),
    )

    db.refresh(user)
    return _to_employee_out(db, user)


@router.post("/employees/with-password", status_code=status.HTTP_201_CREATED)
def create_employee_with_password(
    payload: EmployeeCreate, admin: AdminUser, request: Request, db: DbSession
) -> dict:
    """Same as create, but returns the initial password once for handover."""
    raw_password = payload.password or generate_temp_password()
    payload = payload.model_copy(update={"password": raw_password})
    employee = create_employee(payload, admin, request, db)
    return {
        "employee": employee.model_dump(),
        "initial_password": raw_password,
        "note": "Share this once. The employee must change it at first sign-in.",
    }


@router.get("/employees/{user_id}", response_model=EmployeeOut)
def get_employee(user_id: int, viewer: ManagerUser, db: DbSession) -> EmployeeOut:
    from app.api.deps import can_view_employee

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not can_view_employee(viewer, user):
        raise HTTPException(status_code=403, detail="No access to this employee")
    return _to_employee_out(db, user)


@router.patch("/employees/{user_id}", response_model=EmployeeOut)
def update_employee(
    user_id: int,
    payload: EmployeeUpdate,
    admin: AdminUser,
    request: Request,
    db: DbSession,
) -> EmployeeOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    before = audit.model_snapshot(user, _EMPLOYEE_AUDIT_FIELDS)
    changes = payload.model_dump(exclude_unset=True)

    if changes.get("team_leader_id") == user.id:
        raise HTTPException(
            status_code=400, detail="An employee cannot be their own team leader"
        )
    if "email" in changes and changes["email"]:
        clash = db.scalar(
            select(User.id).where(
                User.email == changes["email"].lower(), User.id != user.id
            )
        )
        if clash:
            raise HTTPException(status_code=409, detail="That email is already registered")
        changes["email"] = changes["email"].lower()

    if changes.get("is_active") is False and user.role is Role.ADMIN:
        remaining = db.scalar(
            select(func.count(User.id)).where(
                User.role == Role.ADMIN, User.is_active.is_(True), User.id != user.id
            )
        )
        if not remaining:
            raise HTTPException(
                status_code=400, detail="Cannot deactivate the last active admin"
            )

    for field, value in changes.items():
        setattr(user, field, value)
    if changes.get("is_active") is False:
        user.deactivated_at = utcnow()

    db.flush()
    audit.record(
        db,
        actor=admin,
        action="update_employee",
        entity_type="user",
        entity_id=user.id,
        summary=f"Updated {user.full_name}: {', '.join(changes) or 'no changes'}",
        before=before,
        after=audit.model_snapshot(user, _EMPLOYEE_AUDIT_FIELDS),
        ip_address=client_ip(request),
    )
    db.refresh(user)
    return _to_employee_out(db, user)


@router.post("/employees/{user_id}/team-leader", response_model=EmployeeOut)
def assign_team_leader(
    user_id: int,
    payload: AssignLeaderRequest,
    admin: AdminUser,
    request: Request,
    db: DbSession,
) -> EmployeeOut:
    """Route this employee's alerts to a specific team leader."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if payload.team_leader_id is not None:
        if payload.team_leader_id == user.id:
            raise HTTPException(
                status_code=400, detail="An employee cannot be their own team leader"
            )
        leader = db.get(User, payload.team_leader_id)
        if leader is None or not leader.is_manager:
            raise HTTPException(
                status_code=400,
                detail="Alerts can only be routed to a team leader or admin",
            )

    before = {"team_leader_id": user.team_leader_id}
    user.team_leader_id = payload.team_leader_id
    db.flush()

    audit.record(
        db,
        actor=admin,
        action="assign_team_leader",
        entity_type="user",
        entity_id=user.id,
        summary=(
            f"Alerts for {user.full_name} now go to "
            f"{user.team_leader.full_name if user.team_leader else 'department default'}"
        ),
        before=before,
        after={"team_leader_id": user.team_leader_id},
        ip_address=client_ip(request),
    )
    db.refresh(user)
    return _to_employee_out(db, user)


@router.post("/employees/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: ResetPasswordRequest,
    admin: AdminUser,
    request: Request,
    db: DbSession,
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    raw_password = payload.new_password or generate_temp_password()
    problems = password_problems(raw_password)
    if payload.new_password and problems:
        raise HTTPException(status_code=422, detail="Password " + ", ".join(problems))

    user.password_hash = hash_password(raw_password)
    user.must_change_password = payload.require_change
    user.failed_login_count = 0
    user.locked_until = None

    audit.record(
        db,
        actor=admin,
        action="reset_password",
        entity_type="user",
        entity_id=user.id,
        summary=f"Reset password for {user.full_name}",
        ip_address=client_ip(request),
    )
    return {
        "ok": True,
        "temporary_password": raw_password,
        "must_change": payload.require_change,
        "note": "Share this once, over a channel the employee already trusts.",
    }


@router.post("/employees/{user_id}/enrollment-code", response_model=EnrollmentCodeOut)
def issue_enrollment_code(
    user_id: int, admin: AdminUser, request: Request, db: DbSession
) -> EnrollmentCodeOut:
    """Issue a one-time code that binds a laptop to this employee."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    code = generate_enrollment_code()
    expires = enrollment_code_expiry()
    db.add(
        EnrollmentCode(
            user_id=user.id,
            code_hash=hash_token(code),
            expires_at=expires,
            created_by_id=admin.id,
        )
    )
    audit.record(
        db,
        actor=admin,
        action="issue_enrollment_code",
        entity_type="user",
        entity_id=user.id,
        summary=f"Issued a device enrollment code for {user.full_name}",
        ip_address=client_ip(request),
    )
    return EnrollmentCodeOut(
        code=code,
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
        expires_at=expires,
    )


@router.get("/employees/{user_id}/devices")
def list_devices(user_id: int, viewer: ManagerUser, db: DbSession) -> list[dict]:
    from app.api.deps import can_view_employee

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not can_view_employee(viewer, user):
        raise HTTPException(status_code=403, detail="No access to this employee")

    devices = db.scalars(select(Device).where(Device.user_id == user_id))
    return [
        {
            "id": d.id,
            "hostname": d.hostname,
            "platform": d.platform,
            "os_version": d.os_version,
            "agent_version": d.agent_version,
            "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
            "last_ip": d.last_ip,
            "is_active": d.is_active,
        }
        for d in devices
    ]


@router.delete("/devices/{device_id}", response_model=ActionResult)
def revoke_device(
    device_id: int, admin: AdminUser, request: Request, db: DbSession
) -> ActionResult:
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    device.is_active = False
    device.revoked_at = utcnow()
    audit.record(
        db,
        actor=admin,
        action="revoke_device",
        entity_type="device",
        entity_id=device.id,
        summary=f"Revoked device {device.hostname or device.device_uid[:12]}",
        ip_address=client_ip(request),
    )
    return ActionResult(detail="Device access revoked. The agent must re-enroll.")


# --------------------------------------------------------------------------- #
# Departments
# --------------------------------------------------------------------------- #
@router.get("/departments", response_model=list[DepartmentOut])
def list_departments(viewer: CurrentUser, db: DbSession) -> list[DepartmentOut]:
    rows = []
    for dept in db.scalars(select(Department).order_by(Department.name)):
        count = (
            db.scalar(
                select(func.count(User.id)).where(
                    User.department_id == dept.id, User.is_active.is_(True)
                )
            )
            or 0
        )
        rows.append(
            DepartmentOut(
                id=dept.id,
                name=dept.name,
                description=dept.description,
                default_schedule_id=dept.default_schedule_id,
                default_team_leader_id=dept.default_team_leader_id,
                is_active=dept.is_active,
                member_count=count,
            )
        )
    return rows


@router.post("/departments", response_model=DepartmentOut, status_code=201)
def create_department(
    payload: DepartmentCreate, admin: AdminUser, request: Request, db: DbSession
) -> DepartmentOut:
    if db.scalar(select(Department.id).where(Department.name == payload.name)):
        raise HTTPException(status_code=409, detail="A department with that name exists")

    dept = Department(**payload.model_dump())
    db.add(dept)
    db.flush()
    audit.record(
        db,
        actor=admin,
        action="create_department",
        entity_type="department",
        entity_id=dept.id,
        summary=f"Created department {dept.name}",
        ip_address=client_ip(request),
    )
    return DepartmentOut(**payload.model_dump(), id=dept.id, is_active=True, member_count=0)


@router.patch("/departments/{dept_id}", response_model=DepartmentOut)
def update_department(
    dept_id: int,
    payload: DepartmentUpdate,
    admin: AdminUser,
    request: Request,
    db: DbSession,
) -> DepartmentOut:
    dept = db.get(Department, dept_id)
    if dept is None:
        raise HTTPException(status_code=404, detail="Department not found")

    before = audit.model_snapshot(
        dept, ["name", "default_schedule_id", "default_team_leader_id", "is_active"]
    )
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(dept, field, value)
    db.flush()

    audit.record(
        db,
        actor=admin,
        action="update_department",
        entity_type="department",
        entity_id=dept.id,
        summary=f"Updated department {dept.name}",
        before=before,
        after=audit.model_snapshot(
            dept, ["name", "default_schedule_id", "default_team_leader_id", "is_active"]
        ),
        ip_address=client_ip(request),
    )
    count = db.scalar(
        select(func.count(User.id)).where(User.department_id == dept.id)
    ) or 0
    return DepartmentOut(
        id=dept.id,
        name=dept.name,
        description=dept.description,
        default_schedule_id=dept.default_schedule_id,
        default_team_leader_id=dept.default_team_leader_id,
        is_active=dept.is_active,
        member_count=count,
    )


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #
@router.get("/schedules", response_model=list[ScheduleOut])
def list_schedules(viewer: CurrentUser, db: DbSession) -> list[WorkSchedule]:
    return list(db.scalars(select(WorkSchedule).order_by(WorkSchedule.name)))


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
def create_schedule(
    payload: ScheduleCreate, admin: AdminUser, request: Request, db: DbSession
) -> WorkSchedule:
    if db.scalar(select(WorkSchedule.id).where(WorkSchedule.name == payload.name)):
        raise HTTPException(status_code=409, detail="A schedule with that name exists")

    schedule = WorkSchedule(**payload.model_dump())
    db.add(schedule)
    db.flush()
    audit.record(
        db,
        actor=admin,
        action="create_schedule",
        entity_type="schedule",
        entity_id=schedule.id,
        summary=(
            f"Created schedule {schedule.name} "
            f"({schedule.start_time}-{schedule.end_time} {schedule.timezone})"
        ),
        ip_address=client_ip(request),
    )
    return schedule


@router.patch("/schedules/{schedule_id}", response_model=ScheduleOut)
def update_schedule(
    schedule_id: int,
    payload: ScheduleUpdate,
    admin: AdminUser,
    request: Request,
    db: DbSession,
) -> WorkSchedule:
    schedule = db.get(WorkSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")

    fields = ["name", "start_time", "end_time", "timezone", "workdays",
              "grace_late_minutes", "grace_early_minutes", "is_active"]
    before = audit.model_snapshot(schedule, fields)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(schedule, field, value)
    db.flush()

    audit.record(
        db,
        actor=admin,
        action="update_schedule",
        entity_type="schedule",
        entity_id=schedule.id,
        summary=f"Updated schedule {schedule.name}",
        before=before,
        after=audit.model_snapshot(schedule, fields),
        ip_address=client_ip(request),
    )
    return schedule
