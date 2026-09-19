"""Linux idle detection via the X11 screensaver extension.

Present mainly so developers on Linux can run the agent locally; Wayland
sessions do not expose a comparable system-wide idle counter, and the detector
reports unsupported there rather than guessing.
"""
from __future__ import annotations

import ctypes
import logging
import os

from tracker.idle.base import IdleDetector

logger = logging.getLogger(__name__)


class XScreenSaverInfo(ctypes.Structure):
    _fields_ = [
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),
        ("event_mask", ctypes.c_ulong),
    ]


class LinuxIdleDetector(IdleDetector):
    name = "linux"

    def __init__(self) -> None:
        self._available = False
        try:
            self._x11 = ctypes.cdll.LoadLibrary("libX11.so.1")
            self._xss = ctypes.cdll.LoadLibrary("libXss.so.1")
            self._x11.XOpenDisplay.restype = ctypes.c_void_p
            self._xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(XScreenSaverInfo)

            display_name = os.environ.get("DISPLAY", ":0").encode()
            self._display = self._x11.XOpenDisplay(display_name)
            if self._display:
                self._root = self._x11.XDefaultRootWindow(self._display)
                self._info = self._xss.XScreenSaverAllocInfo()
                self._available = True
        except OSError:
            logger.debug("X11 screensaver extension unavailable", exc_info=True)

    def is_supported(self) -> bool:
        return self._available

    def idle_seconds(self) -> int:
        if not self._available:
            raise OSError("X11 screensaver extension unavailable")
        self._xss.XScreenSaverQueryInfo(self._display, self._root, self._info)
        return int(self._info.contents.idle / 1000)

    def describe(self) -> str:
        return "Linux X11 XScreenSaver"
