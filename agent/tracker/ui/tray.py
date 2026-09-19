"""System tray icon.

Uses pystray when available. If it is not, the agent still runs and tracks
normally — it simply keeps the window visible instead, so a missing optional
dependency can never silently stop attendance recording.
"""
from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Optional

from tracker.config import APP_NAME
from tracker.engine import AgentState, TrackerEngine

logger = logging.getLogger(__name__)

try:
    import pystray
    from PIL import Image, ImageDraw

    TRAY_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    TRAY_AVAILABLE = False
    logger.info("pystray/Pillow not installed — running without a tray icon")

STATE_COLOURS = {
    "active": (22, 163, 74),
    "idle": (234, 88, 12),
    "on_break": (217, 119, 6),
    "offline": (220, 38, 38),
    "clocked_out": (100, 116, 139),
}
DISCONNECTED = (148, 163, 184)


def _icon_image(colour: tuple[int, int, int]):
    """A filled circle in the state colour, readable at 16px."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse([4, 4, size - 4, size - 4], fill=colour + (255,))
    draw.ellipse([4, 4, size - 4, size - 4], outline=(255, 255, 255, 190), width=3)
    return image


class TrayIcon:
    def __init__(
        self,
        engine: TrackerEngine,
        on_open: Callable[[], None],
        on_quit: Callable[[], None],
    ):
        self.engine = engine
        self.on_open = on_open
        self.on_quit = on_quit
        self.icon: Optional["pystray.Icon"] = None

        if TRAY_AVAILABLE:
            self.icon = pystray.Icon(
                APP_NAME,
                _icon_image(DISCONNECTED),
                APP_NAME,
                menu=self._menu(),
            )
            engine.subscribe(self._on_state)

    def _menu(self):
        def clocked_in() -> bool:
            return self.engine.state.clocked_in

        def on_break() -> bool:
            return self.engine.state.on_break

        return pystray.Menu(
            pystray.MenuItem(
                lambda _: f"{self.engine.state.status_emoji}  {self.engine.state.status_label}",
                self._open,
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Clock In", lambda: self._act(self.engine.clock_in),
                visible=lambda _: not clocked_in(),
            ),
            pystray.MenuItem(
                "Clock Out", lambda: self._act(self.engine.clock_out),
                visible=lambda _: clocked_in(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start Lunch Break",
                lambda: self._act(lambda: self.engine.start_break("lunch")),
                visible=lambda _: clocked_in() and not on_break(),
            ),
            pystray.MenuItem(
                "Start 10-Minute Break",
                lambda: self._act(lambda: self.engine.start_break("short")),
                visible=lambda _: clocked_in() and not on_break(),
            ),
            pystray.MenuItem(
                "End Break", lambda: self._act(self.engine.end_break),
                visible=lambda _: on_break(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open tracker", self._open),
            pystray.MenuItem("Quit", self._quit),
        )

    def _act(self, call: Callable[[], tuple[bool, str]]) -> None:
        ok, message = call()
        self.notify(message if ok else f"Not allowed: {message}")

    def _open(self, *_args) -> None:
        self.on_open()

    def _quit(self, *_args) -> None:
        self.on_quit()

    def _on_state(self, state: AgentState) -> None:
        if self.icon is None:
            return
        colour = (
            STATE_COLOURS.get(state.state, DISCONNECTED)
            if state.connected
            else DISCONNECTED
        )
        try:
            self.icon.icon = _icon_image(colour)
            title = f"{APP_NAME} — {state.status_label}"
            if state.on_break and state.break_remaining_seconds is not None:
                minutes = max(0, state.break_remaining_seconds // 60)
                title += f" ({minutes}m left)"
            self.icon.title = title
            self.icon.update_menu()
        except Exception:  # noqa: BLE001 - a tray failure must not stop tracking
            logger.debug("Could not update the tray icon", exc_info=True)

    def notify(self, message: str) -> None:
        if self.icon is None:
            return
        try:
            self.icon.notify(message, APP_NAME)
        except Exception:  # noqa: BLE001 - not every desktop supports notifications
            logger.debug("Tray notification unavailable", exc_info=True)

    def run_detached(self) -> None:
        """Run the tray loop on its own thread so Tk keeps the main thread."""
        if self.icon is None:
            return
        threading.Thread(
            target=self.icon.run, name="presence-tray", daemon=True
        ).start()

    def stop(self) -> None:
        if self.icon is not None:
            with contextlib.suppress(Exception):
                self.icon.stop()
