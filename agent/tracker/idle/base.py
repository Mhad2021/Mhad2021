"""Idle detection interface.

Every backend answers one question: how many seconds since the last keyboard or
mouse input. It never reads *what* was typed or clicked — the operating system
exposes only a timestamp, and that is all this layer requests.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class IdleDetector(ABC):
    """Platform-specific source of input-idle time and lock state."""

    name = "base"

    @abstractmethod
    def idle_seconds(self) -> int:
        """Seconds since the last keyboard or mouse input."""

    def is_locked(self) -> bool:
        """True when the workstation is locked. Defaults to unknown = False."""
        return False

    def is_supported(self) -> bool:
        """Whether this detector can run on the current machine."""
        return True

    def describe(self) -> str:
        return self.name


class NullIdleDetector(IdleDetector):
    """Fallback when no platform backend is available.

    Always reports zero idle time so an unsupported machine never produces
    false inactivity alerts. The server still sees the heartbeat, so the
    laptop is correctly shown as online.
    """

    name = "unsupported"

    def idle_seconds(self) -> int:
        return 0

    def describe(self) -> str:
        return "unsupported platform (idle time not measured)"
