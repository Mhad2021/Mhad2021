"""Windows idle detection via GetLastInputInfo.

GetLastInputInfo returns the tick count of the last input event, system-wide.
It carries no information about which key or button was pressed — Windows does
not expose that through this API, which is precisely why it is the right one
for attendance tracking.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from tracker.idle.base import IdleDetector

logger = logging.getLogger(__name__)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class WindowsIdleDetector(IdleDetector):
    name = "windows"

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._info = LASTINPUTINFO()
        self._info.cbSize = ctypes.sizeof(LASTINPUTINFO)

    def is_supported(self) -> bool:
        try:
            self.idle_seconds()
            return True
        except Exception:  # noqa: BLE001
            return False

    def idle_seconds(self) -> int:
        if not self._user32.GetLastInputInfo(ctypes.byref(self._info)):
            raise OSError("GetLastInputInfo failed")

        # GetTickCount wraps every ~49.7 days; masking to 32 bits keeps the
        # subtraction correct across the wrap instead of returning a huge value.
        now_ticks = self._kernel32.GetTickCount() & 0xFFFFFFFF
        last_ticks = self._info.dwTime & 0xFFFFFFFF
        elapsed_ms = (now_ticks - last_ticks) & 0xFFFFFFFF
        return int(elapsed_ms / 1000)

    def is_locked(self) -> bool:
        """A locked workstation has no accessible desktop to open.

        OpenInputDesktop fails with ERROR_ACCESS_DENIED while the secure
        desktop (lock screen) is showing.
        """
        try:
            desktop = self._user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
            if desktop == 0:
                return True
            self._user32.CloseDesktop(desktop)
            return False
        except Exception:  # noqa: BLE001
            logger.debug("Lock-state check failed", exc_info=True)
            return False

    def describe(self) -> str:
        return "Windows GetLastInputInfo"
