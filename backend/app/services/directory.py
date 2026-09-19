"""Employee directory queries, scoped by the viewer's role.

Shared by the REST API and the server-rendered pages so both apply exactly the
same visibility rules.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Department, User
from app.models.enums import Role


def scope_to_viewer(stmt, viewer: User):
    """Admins see everyone; team leaders see their team and the departments they
    cover; employees see only themselves."""
    if viewer.role is Role.ADMIN:
        return stmt
    if viewer.role is Role.TEAM_LEADER:
        return stmt.where(
            (User.team_leader_id == viewer.id)
            | (User.id == viewer.id)
            | (
                User.department_id.in_(
                    select(Department.id).where(
                        Department.default_team_leader_id == viewer.id
                    )
                )
            )
        )
    return stmt.where(User.id == viewer.id)


def list_employees(
    db: Session,
    viewer: User,
    *,
    department_id: Optional[int] = None,
    include_inactive: bool = False,
    search: Optional[str] = None,
) -> list[User]:
    stmt = scope_to_viewer(
        select(User).options(
            selectinload(User.department),
            selectinload(User.team_leader),
            selectinload(User.schedule),
        ),
        viewer,
    )

    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))
    if department_id is not None:
        stmt = stmt.where(User.department_id == department_id)
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            User.full_name.ilike(pattern)
            | User.username.ilike(pattern)
            | User.employee_code.ilike(pattern)
            | User.email.ilike(pattern)
        )

    return list(db.scalars(stmt.order_by(User.full_name)))


def managers(db: Session) -> list[User]:
    """Everyone who can be assigned as a team leader."""
    return list(
        db.scalars(
            select(User)
            .where(
                User.role.in_([Role.ADMIN, Role.TEAM_LEADER]),
                User.is_active.is_(True),
            )
            .order_by(User.full_name)
        )
    )
