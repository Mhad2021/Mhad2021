"""Work sessions, breaks, activity intervals and the event timeline."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    Boolean,
    Date,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, TimestampMixin, UTCDateTime
from app.models.enums import (
    ActivityState,
    BreakEndReason,
    BreakType,
    ClockOutReason,
    EventSource,
    EventType,
)

if TYPE_CHECKING:
    from app.models.user import Device, User


class WorkSession(Base, TimestampMixin):
    """One clock-in → clock-out span. The unit all reporting rolls up from."""

    __tablename__ = "work_sessions"
    __table_args__ = (
        Index("ix_sessions_user_date", "user_id", "work_date"),
        Index("ix_sessions_open", "user_id", "clock_out_at"),
        Index("ix_sessions_heartbeat", "clock_out_at", "last_heartbeat_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL")
    )

    # Local calendar date the session belongs to, used for daily reports.
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    clock_in_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False
    )
    clock_out_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    clock_out_reason: Mapped[Optional[ClockOutReason]] = mapped_column(
        SAEnum(ClockOutReason, native_enum=False, length=24)
    )

    # Rolling totals in seconds, recomputed as intervals close.
    active_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idle_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lunch_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    short_break_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    offline_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Schedule comparison, computed at clock-in / clock-out.
    scheduled_start_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime
    )
    scheduled_end_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    is_late_arrival: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    late_by_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_early_departure: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    early_by_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Live state, refreshed by the heartbeat handler and the monitor job.
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime
    )
    current_state: Mapped[ActivityState] = mapped_column(
        SAEnum(ActivityState, native_enum=False, length=20),
        default=ActivityState.ACTIVE,
        nullable=False,
    )
    state_since: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    is_offline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    offline_since: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)

    notes: Mapped[Optional[str]] = mapped_column(Text)
    edited_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    user: Mapped["User"] = relationship(
        back_populates="work_sessions", foreign_keys=[user_id]
    )
    device: Mapped[Optional["Device"]] = relationship()
    breaks: Mapped[list["BreakPeriod"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="BreakPeriod.started_at"
    )
    intervals: Mapped[list["ActivityInterval"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    @property
    def is_open(self) -> bool:
        return self.clock_out_at is None

    @property
    def break_seconds(self) -> int:
        return self.lunch_seconds + self.short_break_seconds

    @property
    def total_seconds(self) -> int:
        """Wall-clock span of the session, to now while it is still open."""
        end = self.clock_out_at or datetime.now(timezone.utc)
        return max(0, int((end - self.clock_in_at).total_seconds()))

    @property
    def worked_seconds(self) -> int:
        """Paid time: everything except breaks and offline gaps."""
        return max(0, self.total_seconds - self.break_seconds - self.offline_seconds)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<WorkSession u={self.user_id} {self.work_date} open={self.is_open}>"


class BreakPeriod(Base, TimestampMixin):
    """An approved break. While one is open, inactivity alerts are suppressed."""

    __tablename__ = "break_periods"
    __table_args__ = (
        Index("ix_breaks_session_open", "session_id", "ended_at"),
        Index("ix_breaks_user_date", "user_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("work_sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    break_type: Mapped[BreakType] = mapped_column(
        SAEnum(BreakType, native_enum=False, length=16), nullable=False
    )

    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    end_reason: Mapped[Optional[BreakEndReason]] = mapped_column(
        SAEnum(BreakEndReason, native_enum=False, length=24)
    )

    # Allowance snapshotted at start, so later policy changes don't rewrite history.
    allowed_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    overrun_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    overrun_alert_sent: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    session: Mapped["WorkSession"] = relationship(back_populates="breaks")
    user: Mapped["User"] = relationship()

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BreakPeriod {self.break_type.value} u={self.user_id} open={self.is_open}>"


class ActivityInterval(Base):
    """A contiguous run in one state. Closing an interval is how active and idle
    totals are accumulated — far cheaper than storing every heartbeat."""

    __tablename__ = "activity_intervals"
    __table_args__ = (
        Index("ix_intervals_session_start", "session_id", "started_at"),
        Index("ix_intervals_open", "session_id", "ended_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("work_sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[ActivityState] = mapped_column(
        SAEnum(ActivityState, native_enum=False, length=20), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    session: Mapped["WorkSession"] = relationship(back_populates="intervals")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Interval {self.state.value} {self.duration_seconds}s>"


class ActivityEvent(Base):
    """Append-only timeline. Every clock-in, break, idle run, lock, disconnect
    and manual correction lands here; nothing in this table is ever updated."""

    __tablename__ = "activity_events"
    __table_args__ = (
        Index("ix_events_user_time", "user_id", "occurred_at"),
        Index("ix_events_session", "session_id"),
        Index("ix_events_type_time", "event_type", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("work_sessions.id", ondelete="CASCADE")
    )
    device_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL")
    )
    event_type: Mapped[EventType] = mapped_column(
        SAEnum(EventType, native_enum=False, length=32), nullable=False
    )
    source: Mapped[EventSource] = mapped_column(
        SAEnum(EventSource, native_enum=False, length=16),
        default=EventSource.AGENT,
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False
    )
    # Small structured payload: idle seconds, break type, reason. Never content.
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    message: Mapped[Optional[str]] = mapped_column(String(500))

    user: Mapped["User"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Event {self.event_type.value} u={self.user_id} @{self.occurred_at}>"
