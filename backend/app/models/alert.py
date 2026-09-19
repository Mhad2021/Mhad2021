"""Alerts, notification channels, delivery attempts and the admin audit log."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    Boolean,
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
from app.models.enums import AlertSeverity, AlertType, ChannelType, DeliveryStatus

if TYPE_CHECKING:
    from app.models.user import User


class Alert(Base, TimestampMixin):
    """A condition worth a team leader's attention."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_recipient_ack", "recipient_id", "acknowledged_at"),
        Index("ix_alerts_subject_time", "subject_id", "triggered_at"),
        Index("ix_alerts_dedup", "dedup_key", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # The employee the alert is about.
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # The team leader / manager it goes to.
    recipient_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("work_sessions.id", ondelete="CASCADE")
    )

    alert_type: Mapped[AlertType] = mapped_column(
        SAEnum(AlertType, native_enum=False, length=32), nullable=False
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        SAEnum(AlertSeverity, native_enum=False, length=16),
        default=AlertSeverity.WARNING,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    triggered_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    acknowledged_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    acknowledgement_note: Mapped[Optional[str]] = mapped_column(Text)

    # Stops the monitor re-raising the same alert every 30s tick.
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)

    subject: Mapped["User"] = relationship(foreign_keys=[subject_id])
    recipient: Mapped[Optional["User"]] = relationship(foreign_keys=[recipient_id])
    deliveries: Mapped[list["NotificationDelivery"]] = relationship(
        back_populates="alert", cascade="all, delete-orphan"
    )

    @property
    def is_open(self) -> bool:
        return self.acknowledged_at is None and self.resolved_at is None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Alert {self.alert_type.value} subject={self.subject_id}>"


class NotificationChannel(Base, TimestampMixin):
    """A configured outbound integration. Credentials are encrypted at rest."""

    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    channel_type: Mapped[ChannelType] = mapped_column(
        SAEnum(ChannelType, native_enum=False, length=20), nullable=False
    )
    # Fernet-encrypted JSON blob (webhook URL, SMTP override, API token…).
    encrypted_config: Mapped[Optional[str]] = mapped_column(Text)
    # Non-secret display config, safe to show in the dashboard.
    public_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    min_severity: Mapped[AlertSeverity] = mapped_column(
        SAEnum(AlertSeverity, native_enum=False, length=16),
        default=AlertSeverity.WARNING,
        nullable=False,
    )
    # Empty list = all alert types.
    alert_types: Mapped[Optional[list[str]]] = mapped_column(JSON)
    department_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE")
    )

    last_success_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    last_error_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    def handles(self, alert_type: AlertType, severity: AlertSeverity) -> bool:
        severity_rank = {"info": 1, "warning": 2, "critical": 3}
        if severity_rank[severity.value] < severity_rank[self.min_severity.value]:
            return False
        if self.alert_types:
            return alert_type.value in self.alert_types
        return True

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NotificationChannel {self.name} ({self.channel_type.value})>"


class NotificationDelivery(Base, TimestampMixin):
    """One attempt to push an alert down one channel. Retried with backoff."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        Index("ix_deliveries_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="SET NULL")
    )
    channel_type: Mapped[ChannelType] = mapped_column(
        SAEnum(ChannelType, native_enum=False, length=20), nullable=False
    )
    target: Mapped[Optional[str]] = mapped_column(String(255))

    status: Mapped[DeliveryStatus] = mapped_column(
        SAEnum(DeliveryStatus, native_enum=False, length=16),
        default=DeliveryStatus.PENDING,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    sent_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    alert: Mapped["Alert"] = relationship(back_populates="deliveries")
    channel: Mapped[Optional["NotificationChannel"]] = relationship()


class AuditLog(Base):
    """Who changed what, from where. Written for every mutating admin action."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_actor_time", "actor_id", "occurred_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_label: Mapped[str] = mapped_column(String(160), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[Optional[str]] = mapped_column(String(60))
    summary: Mapped[Optional[str]] = mapped_column(String(500))
    before: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    after: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False
    )

    actor: Mapped[Optional["User"]] = relationship()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditLog {self.action} {self.entity_type}:{self.entity_id}>"
