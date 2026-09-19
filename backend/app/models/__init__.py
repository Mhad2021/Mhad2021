"""Model package. Importing this registers every table on Base.metadata."""
from app.models.alert import (
    Alert,
    AuditLog,
    NotificationChannel,
    NotificationDelivery,
)
from app.models.enums import (
    ActivityState,
    AlertSeverity,
    AlertType,
    BreakEndReason,
    BreakType,
    ChannelType,
    ClockOutReason,
    DeliveryStatus,
    EventSource,
    EventType,
    PresenceState,
    Role,
)
from app.models.org import Department, PolicySettings, WorkSchedule
from app.models.session import (
    ActivityEvent,
    ActivityInterval,
    BreakPeriod,
    WorkSession,
)
from app.models.user import Device, EnrollmentCode, RefreshToken, User

__all__ = [
    "ActivityEvent",
    "ActivityInterval",
    "ActivityState",
    "Alert",
    "AlertSeverity",
    "AlertType",
    "AuditLog",
    "BreakEndReason",
    "BreakPeriod",
    "BreakType",
    "ChannelType",
    "ClockOutReason",
    "DeliveryStatus",
    "Department",
    "Device",
    "EnrollmentCode",
    "EventSource",
    "EventType",
    "NotificationChannel",
    "NotificationDelivery",
    "PolicySettings",
    "PresenceState",
    "RefreshToken",
    "Role",
    "User",
    "WorkSchedule",
    "WorkSession",
]
