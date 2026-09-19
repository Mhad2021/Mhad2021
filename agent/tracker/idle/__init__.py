"""Pick the right idle detector for this machine."""
from __future__ import annotations

import logging
import sys

from tracker.idle.base import IdleDetector, NullIdleDetector

logger = logging.getLogger(__name__)


def build_detector() -> IdleDetector:
    """Return the best available detector, never raising."""
    try:
        if sys.platform == "win32":
            from tracker.idle.windows import WindowsIdleDetector

            detector = WindowsIdleDetector()
        elif sys.platform == "darwin":
            from tracker.idle.macos import MacIdleDetector

            detector = MacIdleDetector()
        elif sys.platform.startswith("linux"):
            from tracker.idle.linux import LinuxIdleDetector

            detector = LinuxIdleDetector()
        else:
            logger.warning("No idle detector for platform %s", sys.platform)
            return NullIdleDetector()

        if detector.is_supported():
            logger.info("Idle detection: %s", detector.describe())
            return detector

        logger.warning("%s reported unsupported; idle time will not be measured",
                       detector.describe())
    except Exception:  # noqa: BLE001 - never let detection stop the agent
        logger.exception("Failed to initialise idle detection")

    return NullIdleDetector()


__all__ = ["IdleDetector", "NullIdleDetector", "build_detector"]
