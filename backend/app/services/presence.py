"""Live presence: what each employee's status is right now, and for how long."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.timeutil import (
    ensure_aware,
    humanize_duration,
    seconds_between,
    to_local,
    utcnow,
)
from app.models import BreakPeriod, Department, User, WorkSession
from app.models.enums import (
    ActivityState,
    BreakType,
    ClientKind,
    PresenceState,
    Role,
)
from app.services import activity, attendance
from app.services.policy import EffectivePolicy, get_policy_for_department

STATE_META: dict[PresenceState, dict[str, str]] = {
    PresenceState.ACTIVE: {"emoji": "🟢", "label": "Active", "colour": "green"},
    PresenceState.ON_BREAK: {"emoji": "🟡", "label": "On Break", "colour": "amber"},
    PresenceState.IDLE: {"emoji": "🟠", "label": "Idle", "colour": "orange"},
    PresenceState.OFFLINE: {"emoji": "🔴", "label": "Offline", "colour": "red"},
    PresenceState.CLOCKED_OUT: {"emoji": "⚫", "label": "Clocked Out", "colour": "slate"},
}

_SORT_ORDER = {
    PresenceState.OFFLINE: 0,
    PresenceState.IDLE: 1,
    PresenceState.ON_BREAK: 2,
    PresenceState.ACTIVE: 3,
    PresenceState.CLOCKED_OUT: 4,
}


@dataclass
class PresenceRow:
    user_id: int
    full_name: str
    employee_code: str
    department: Optional[str]
    team_leader: Optional[str]
    state: PresenceState
    since: Optional[datetime]
    duration_seconds: int
    detail: str
    session_id: Optional[int] = None
    clock_in_at: Optional[datetime] = None
    active_seconds: int = 0
    idle_seconds: int = 0
    break_seconds: int = 0
    worked_seconds: int = 0
    is_late: bool = False
    break_type: Optional[str] = None
    break_remaining_seconds: Optional[int] = None
    last_heartbeat_at: Optional[datetime] = None
    client_kind: str = "agent"
    warnings: list[str] = field(default_factory=list)

    @property
    def emoji(self) -> str:
        return STATE_META[self.state]["emoji"]

    @property
    def label(self) -> str:
        return STATE_META[self.state]["label"]

    @property
    def colour(self) -> str:
        return STATE_META[self.state]["colour"]

    @property
    def duration_display(self) -> str:
        return humanize_duration(self.duration_seconds)

    def to_dict(self, tz: str = "UTC") -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "full_name": self.full_name,
            "employee_code": self.employee_code,
            "department": self.department,
            "team_leader": self.team_leader,
            "state": self.state.value,
            "emoji": self.emoji,
            "label": self.label,
            "colour": self.colour,
            "since": self.since.isoformat() if self.since else None,
            "since_ts": self.since.timestamp() if self.since else None,
            "duration_seconds": self.duration_seconds,
            "duration_display": self.duration_display,
            "detail": self.detail,
            "session_id": self.session_id,
            "clock_in_display": (
                to_local(self.clock_in_at, tz).strftime("%H:%M")
                if self.clock_in_at
                else None
            ),
            "active_seconds": self.active_seconds,
            "idle_seconds": self.idle_seconds,
            "break_seconds": self.break_seconds,
            "worked_seconds": self.worked_seconds,
            "worked_display": humanize_duration(self.worked_seconds),
            "active_display": humanize_duration(self.active_seconds),
            "idle_display": humanize_duration(self.idle_seconds),
            "is_late": self.is_late,
            "client_kind": self.client_kind,
            "tracks_idle": self.client_kind == "agent",
            "break_type": self.break_type,
            "break_remaining_seconds": self.break_remaining_seconds,
            "warnings": self.warnings,
        }


def compute_state(
    db: Session,
    user: User,
    session: Optional[WorkSession],
    policy: EffectivePolicy,
    now: Optional[datetime] = None,
) -> tuple[PresenceState, Optional[datetime], str, Optional[BreakPeriod]]:
    """Derive the board state from the session's recorded state plus heartbeat age.

    The heartbeat check comes first: an agent that was killed leaves the session
    sitting in ACTIVE, and only the missing heartbeat reveals it.
    """
    now = now or utcnow()

    if session is None or session.clock_out_at is not None:
        return PresenceState.CLOCKED_OUT, None, "Not clocked in", None

    heartbeat_age = (
        seconds_between(session.last_heartbeat_at, now)
        if session.last_heartbeat_at
        else seconds_between(session.clock_in_at, now)
    )
    if heartbeat_age > policy.heartbeat_grace_seconds:
        since = ensure_aware(
            session.offline_since or session.last_heartbeat_at or session.clock_in_at
        )
        # Say what is actually known. For the desktop tracker a silent laptop
        # means the machine is off or unreachable; for a browser tab it means
        # the page was closed, which says nothing about whether they are working.
        detail = (
            f"Tracking page closed {humanize_duration(heartbeat_age)} ago — "
            "still clocked in"
            if session.client_kind is ClientKind.WEB
            else f"No heartbeat for {humanize_duration(heartbeat_age)}"
        )
        return PresenceState.OFFLINE, since, detail, None

    open_break = attendance.get_open_break(db, session.id)
    if open_break is not None:
        label = "Lunch" if open_break.break_type is BreakType.LUNCH else "10-min break"
        remaining = attendance.break_remaining_seconds(open_break, now)
        detail = (
            f"{label} — {humanize_duration(remaining)} left"
            if remaining >= 0
            else f"{label} — {humanize_duration(abs(remaining))} over"
        )
        return PresenceState.ON_BREAK, ensure_aware(open_break.started_at), detail, open_break

    state_since = ensure_aware(session.state_since or session.clock_in_at)

    if session.current_state is ActivityState.LOCKED:
        return (
            PresenceState.IDLE,
            state_since,
            "Screen locked",
            None,
        )
    if session.current_state is ActivityState.IDLE:
        return (
            PresenceState.IDLE,
            state_since,
            "No keyboard or mouse activity",
            None,
        )

    # The web client reports that the tab is open, nothing more. Saying
    # "Working" would overstate what it can see.
    detail = (
        "Clocked in — tracking page open"
        if session.client_kind is ClientKind.WEB
        else "Working"
    )
    return PresenceState.ACTIVE, state_since, detail, None


def build_row(
    db: Session,
    user: User,
    now: Optional[datetime] = None,
    policy: Optional[EffectivePolicy] = None,
) -> PresenceRow:
    now = now or utcnow()
    policy = policy or get_policy_for_department(db, user.department_id)
    session = attendance.get_open_session(db, user.id)
    state, since, detail, open_break = compute_state(db, user, session, policy, now)

    row = PresenceRow(
        user_id=user.id,
        full_name=user.full_name,
        employee_code=user.employee_code,
        department=user.department.name if user.department else None,
        team_leader=user.team_leader.full_name if user.team_leader else None,
        state=state,
        since=since,
        duration_seconds=seconds_between(since, now) if since else 0,
        detail=detail,
    )

    if session is not None:
        totals = activity.live_totals(db, session)
        row.session_id = session.id
        row.clock_in_at = ensure_aware(session.clock_in_at)
        row.active_seconds = totals["active_seconds"]
        row.idle_seconds = totals["idle_seconds"] + totals["locked_seconds"]
        row.break_seconds = totals["lunch_seconds"] + totals["short_break_seconds"]
        row.worked_seconds = max(
            0,
            seconds_between(session.clock_in_at, now)
            - row.break_seconds
            - totals["offline_seconds"],
        )
        row.is_late = session.is_late_arrival
        row.last_heartbeat_at = session.last_heartbeat_at
        row.client_kind = session.client_kind.value

        if session.is_late_arrival:
            row.warnings.append(
                f"Late by {humanize_duration(session.late_by_seconds)}"
            )

    if open_break is not None:
        row.break_type = open_break.break_type.value
        row.break_remaining_seconds = attendance.break_remaining_seconds(open_break, now)
        if row.break_remaining_seconds < 0:
            row.warnings.append("Break overrun")

    if (
        state is PresenceState.IDLE
        and row.duration_seconds >= policy.idle_threshold_seconds
    ):
        row.warnings.append("Idle past threshold, no break active")

    return row


def visible_employees(db: Session, viewer: User) -> list[User]:
    """Scope the board by role: admins see everyone, leaders see their team."""
    stmt = (
        select(User)
        .options(
            selectinload(User.department),
            selectinload(User.team_leader),
            selectinload(User.schedule),
        )
        .where(User.is_active.is_(True))
    )

    if viewer.role is Role.TEAM_LEADER:
        stmt = stmt.where(
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
    elif viewer.role is Role.EMPLOYEE:
        stmt = stmt.where(User.id == viewer.id)

    return list(db.scalars(stmt.order_by(User.full_name)))


def live_board(
    db: Session,
    viewer: User,
    *,
    department_id: Optional[int] = None,
    state_filter: Optional[str] = None,
) -> list[PresenceRow]:
    now = utcnow()
    rows: list[PresenceRow] = []
    policy_cache: dict[Optional[int], EffectivePolicy] = {}

    for user in visible_employees(db, viewer):
        if department_id is not None and user.department_id != department_id:
            continue
        if user.department_id not in policy_cache:
            policy_cache[user.department_id] = get_policy_for_department(
                db, user.department_id
            )
        rows.append(build_row(db, user, now, policy_cache[user.department_id]))

    if state_filter:
        rows = [r for r in rows if r.state.value == state_filter]

    rows.sort(key=lambda r: (_SORT_ORDER[r.state], r.full_name))
    return rows


def board_summary(rows: list[PresenceRow]) -> dict[str, int]:
    summary = {state.value: 0 for state in PresenceState}
    for row in rows:
        summary[row.state.value] += 1
    summary["total"] = len(rows)
    summary["needs_attention"] = sum(
        1
        for r in rows
        if r.state in (PresenceState.IDLE, PresenceState.OFFLINE) or r.warnings
    )
    return summary
