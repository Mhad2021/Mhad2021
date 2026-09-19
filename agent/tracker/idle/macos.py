"""macOS idle detection via IOKit HIDIdleTime.

Included so the agent's architecture is genuinely cross-platform rather than
Windows-only with a promise. The server side is already platform-agnostic.
"""
from __future__ import annotations

import logging
import subprocess

from tracker.idle.base import IdleDetector

logger = logging.getLogger(__name__)


class MacIdleDetector(IdleDetector):
    name = "macos"

    def is_supported(self) -> bool:
        try:
            self.idle_seconds()
            return True
        except Exception:  # noqa: BLE001
            return False

    def idle_seconds(self) -> int:
        """Read HIDIdleTime (nanoseconds) from the IOKit registry."""
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4", "-r"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        for line in result.stdout.splitlines():
            if "HIDIdleTime" in line:
                nanoseconds = int(line.split("=")[-1].strip())
                return int(nanoseconds / 1_000_000_000)
        raise OSError("HIDIdleTime not present in the IOKit registry")

    def is_locked(self) -> bool:
        """Screen-lock state from the session dictionary."""
        try:
            result = subprocess.run(
                ["ioreg", "-n", "Root", "-d", "1", "-a"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            return "CGSSessionScreenIsLocked" in result.stdout
        except Exception:  # noqa: BLE001
            return False

    def describe(self) -> str:
        return "macOS IOKit HIDIdleTime"
