"""Windows session and power notifications.

Lock, unlock, sleep, resume and shutdown are reported as soon as they happen,
so the server can start an offline run immediately rather than waiting out the
heartbeat grace period. All of these are advisory: the authoritative signal is
still the absence of a heartbeat, which no local event can suppress.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Callable

logger = logging.getLogger(__name__)

WM_WTSSESSION_CHANGE = 0x02B1
WM_POWERBROADCAST = 0x0218
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016

WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
PBT_APMSUSPEND = 0x4
PBT_APMRESUMEAUTOMATIC = 0x12

NOTIFY_FOR_THIS_SESSION = 0


class SessionEventListener:
    """Hidden message-only window that forwards session events to a callback."""

    def __init__(self, on_event: Callable[[str], None]):
        self.on_event = on_event
        self._thread: threading.Thread | None = None
        self._running = False
        self._hwnd = None

    @property
    def is_supported(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import win32con  # noqa: F401
            import win32gui  # noqa: F401
            import win32ts  # noqa: F401

            return True
        except ImportError:
            return False

    def start(self) -> bool:
        if not self.is_supported:
            logger.info(
                "Session-event hooks unavailable (pywin32 not installed). "
                "Lock and shutdown are still inferred from the heartbeat."
            )
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._pump, name="presence-session-events", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._hwnd:
            try:
                import win32con
                import win32gui

                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                pass

    def _pump(self) -> None:  # pragma: no cover - Windows-only message loop
        import win32api
        import win32con
        import win32gui
        import win32ts

        def handler(hwnd, msg, wparam, lparam):
            try:
                if msg == WM_WTSSESSION_CHANGE:
                    if wparam == WTS_SESSION_LOCK:
                        self.on_event("screen_locked")
                    elif wparam == WTS_SESSION_UNLOCK:
                        self.on_event("screen_unlocked")
                elif msg == WM_POWERBROADCAST:
                    if wparam == PBT_APMSUSPEND:
                        self.on_event("system_suspend")
                    elif wparam == PBT_APMRESUMEAUTOMATIC:
                        self.on_event("system_resume")
                elif msg in (WM_QUERYENDSESSION, WM_ENDSESSION):
                    self.on_event("system_shutdown")
            except Exception:  # noqa: BLE001
                logger.exception("Session event handler failed")
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        try:
            window_class = win32gui.WNDCLASS()
            window_class.lpszClassName = "PresenceSessionListener"
            window_class.hInstance = win32api.GetModuleHandle(None)
            window_class.lpfnWndProc = handler
            atom = win32gui.RegisterClass(window_class)

            self._hwnd = win32gui.CreateWindow(
                atom, "Presence", 0, 0, 0, 0, 0, 0, 0, window_class.hInstance, None
            )
            win32ts.WTSRegisterSessionNotification(self._hwnd, NOTIFY_FOR_THIS_SESSION)
            logger.info("Listening for Windows session and power events")

            while self._running:
                win32gui.PumpWaitingMessages()
                import time

                time.sleep(0.4)

            win32ts.WTSUnRegisterSessionNotification(self._hwnd)
        except Exception:  # noqa: BLE001
            logger.exception("Session event listener stopped")
