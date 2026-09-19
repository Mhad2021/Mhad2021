"""Local buffer for events recorded while the server was unreachable.

Lock and shutdown events still matter once the laptop comes back, so they are
queued to disk rather than dropped. The heartbeat itself is never buffered —
a heartbeat that arrives late would misrepresent when the laptop was online,
and the server's missing-heartbeat detection is the authoritative signal.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from tracker.config import user_config_dir

logger = logging.getLogger(__name__)

MAX_BUFFERED_EVENTS = 500


class EventBuffer:
    """A small, append-only JSON queue guarded by a lock."""

    def __init__(self, path: Path | None = None):
        self.path = path or (user_config_dir() / "pending_events.json")
        self._lock = threading.Lock()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            logger.warning("Buffered events unreadable; starting a fresh queue")
            return []

    def _write(self, events: list[dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file then replace, so a crash mid-write cannot
            # leave a truncated queue behind.
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(events), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            logger.error("Could not persist buffered events", exc_info=True)

    def add(self, event: dict[str, Any]) -> None:
        with self._lock:
            events = self._read()
            events.append(event)
            if len(events) > MAX_BUFFERED_EVENTS:
                # Keep the newest; the server's own detection covers the gap.
                dropped = len(events) - MAX_BUFFERED_EVENTS
                events = events[-MAX_BUFFERED_EVENTS:]
                logger.warning("Event buffer full, dropped %s oldest events", dropped)
            self._write(events)

    def take_all(self) -> list[dict[str, Any]]:
        """Return everything queued and clear the buffer."""
        with self._lock:
            events = self._read()
            if events:
                self._write([])
            return events

    def restore(self, events: list[dict[str, Any]]) -> None:
        """Put events back after a failed flush."""
        if not events:
            return
        with self._lock:
            existing = self._read()
            self._write((events + existing)[-MAX_BUFFERED_EVENTS:])

    def count(self) -> int:
        with self._lock:
            return len(self._read())
