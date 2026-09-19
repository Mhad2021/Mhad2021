"""Enumerations shared across models, schemas and services."""
from __future__ import annotations

import enum


class Role(str, enum.Enum):
    ADMIN = "admin"
    TEAM_LEADER = "team_leader"
    EMPLOYEE = "employee"

    @property
    def rank(self) -> int:
        return {"admin": 3, "team_leader": 2, "employee": 1}[self.value]


class ClientKind(str, enum.Enum):
    """Which client opened a work session.

    This is not cosmetic. A browser tab can only observe input inside itself,
    so it cannot report system-wide idle time the way the desktop agent can.
    Alerts that depend on that signal are suppressed for web sessions rather
    than being raised on evidence the client cannot actually provide.
    """

    AGENT = "agent"   # desktop tracker: real OS-level idle detection
    WEB = "web"       # browser tab: attendance and breaks only

    @property
    def can_detect_idle(self) -> bool:
        return self is ClientKind.AGENT


class PresenceState(str, enum.Enum):
    """The live state shown on the manager board."""

    ACTIVE = "active"          # 🟢 keyboard/mouse activity within threshold
    ON_BREAK = "on_break"      # 🟡 approved lunch or short break running
    IDLE = "idle"              # 🟠 no input for >= idle threshold, no break
    OFFLINE = "offline"        # 🔴 heartbeat missing / laptop off / app killed
    CLOCKED_OUT = "clocked_out"  # ⚫ no open work session


class ActivityState(str, enum.Enum):
    """Mutually exclusive states recorded as contiguous intervals."""

    ACTIVE = "active"
    IDLE = "idle"
    LOCKED = "locked"
    BREAK_LUNCH = "break_lunch"
    BREAK_SHORT = "break_short"
    OFFLINE = "offline"

    @property
    def is_break(self) -> bool:
        return self in (ActivityState.BREAK_LUNCH, ActivityState.BREAK_SHORT)

    @property
    def counts_as_worked(self) -> bool:
        """Active time is productive time. Idle/locked count toward the session
        but not toward active time; breaks and offline do not count at all."""
        return self is ActivityState.ACTIVE


class BreakType(str, enum.Enum):
    LUNCH = "lunch"
    SHORT = "short"


class BreakEndReason(str, enum.Enum):
    MANUAL = "manual"              # employee pressed End Break
    AUTO_EXPIRED = "auto_expired"  # allowance ran out, server closed it
    SESSION_END = "session_end"    # clock-out closed it
    ADMIN = "admin"                # manager corrected it


class ClockOutReason(str, enum.Enum):
    MANUAL = "manual"
    AUTO_SCHEDULE = "auto_schedule"  # server closed a forgotten session
    ADMIN = "admin"
    OFFLINE_TIMEOUT = "offline_timeout"


class EventType(str, enum.Enum):
    """Everything written to the immutable activity timeline."""

    CLOCK_IN = "clock_in"
    CLOCK_OUT = "clock_out"
    BREAK_START = "break_start"
    BREAK_END = "break_end"
    BREAK_OVERRUN = "break_overrun"
    IDLE_START = "idle_start"
    IDLE_END = "idle_end"
    SCREEN_LOCKED = "screen_locked"
    SCREEN_UNLOCKED = "screen_unlocked"
    AGENT_STARTED = "agent_started"
    AGENT_STOPPED = "agent_stopped"
    SYSTEM_SUSPEND = "system_suspend"
    SYSTEM_RESUME = "system_resume"
    SYSTEM_SHUTDOWN = "system_shutdown"
    HEARTBEAT_LOST = "heartbeat_lost"
    RECONNECTED = "reconnected"
    LATE_ARRIVAL = "late_arrival"
    EARLY_DEPARTURE = "early_departure"


class EventSource(str, enum.Enum):
    AGENT = "agent"      # reported by the desktop app
    SERVER = "server"    # inferred by the monitor (e.g. missing heartbeat)
    ADMIN = "admin"      # manual correction through the dashboard


class AlertType(str, enum.Enum):
    IDLE_NO_BREAK = "idle_no_break"
    BREAK_OVERRUN = "break_overrun"
    AGENT_OFFLINE = "agent_offline"
    AGENT_TERMINATED = "agent_terminated"
    LATE_ARRIVAL = "late_arrival"
    EARLY_DEPARTURE = "early_departure"
    MISSING_CLOCK_OUT = "missing_clock_out"
    NO_SHOW = "no_show"


class AlertSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ChannelType(str, enum.Enum):
    EMAIL = "email"
    SLACK = "slack"
    TEAMS = "teams"
    WHATSAPP = "whatsapp"
    WEBHOOK = "webhook"
    DASHBOARD = "dashboard"


class DeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"
