"""The agent's background engine.

Runs one loop: read the idle counter, send a heartbeat, publish the server's
answer to the UI. Deliberately thin — it reports raw signals and renders what
the server decides, so the two can never disagree about an employee's state.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from tracker.api_client import ApiClient, ApiError
from tracker.config import AgentConfig
from tracker.idle import IdleDetector, build_detector
from tracker.storage import EventBuffer

logger = logging.getLogger(__name__)

# Heartbeats that fail this many times in a row mean the network is down, not
# a blip — the UI says so rather than showing a stale "Active".
OFFLINE_AFTER_FAILURES = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AgentState:
    """What the tray and window render. Owned by the engine, read by the UI."""

    connected: bool = False
    clocked_in: bool = False
    state: str = "clocked_out"
    detail: str = "Not clocked in"
    session_id: Optional[int] = None
    clock_in_at: Optional[str] = None
    break_type: Optional[str] = None
    break_remaining_seconds: Optional[int] = None
    active_seconds: int = 0
    idle_seconds: int = 0
    break_seconds: int = 0
    worked_seconds: int = 0
    breaks_used: dict[str, int] = field(default_factory=lambda: {"lunch": 0, "short": 0})
    last_error: Optional[str] = None
    last_heartbeat_at: Optional[datetime] = None
    local_idle_seconds: int = 0
    is_locked: bool = False

    @property
    def on_break(self) -> bool:
        return self.break_type is not None

    @property
    def status_label(self) -> str:
        if not self.connected:
            return "Not connected"
        return {
            "active": "Active",
            "idle": "Idle",
            "on_break": "On break",
            "offline": "Offline",
            "clocked_out": "Clocked out",
        }.get(self.state, self.state.replace("_", " ").title())

    @property
    def status_emoji(self) -> str:
        if not self.connected:
            return "⚪"
        return {
            "active": "🟢",
            "idle": "🟠",
            "on_break": "🟡",
            "offline": "🔴",
            "clocked_out": "⚫",
        }.get(self.state, "⚪")


class TrackerEngine:
    """Owns the heartbeat thread and the authoritative local view of state."""

    def __init__(self, config: AgentConfig, client: Optional[ApiClient] = None):
        self.config = config
        self.client = client or ApiClient(config)
        self.detector: IdleDetector = build_detector()
        self.buffer = EventBuffer()
        self.state = AgentState()

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._listeners: list[Callable[[AgentState], None]] = []
        self._consecutive_failures = 0
        self._was_locked = False

    # -- Listener plumbing --------------------------------------------------
    def subscribe(self, callback: Callable[[AgentState], None]) -> None:
        self._listeners.append(callback)

    def _publish(self) -> None:
        for callback in list(self._listeners):
            try:
                callback(self.state)
            except Exception:  # noqa: BLE001 - a broken view must not stop tracking
                logger.exception("A state listener raised")

    # -- Lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="presence-heartbeat", daemon=True
        )
        self._thread.start()
        self.report_event("agent_started")
        logger.info("Engine started (idle detection: %s)", self.detector.describe())

    def stop(self, reason: str = "application closed") -> None:
        """Tell the server we are going away, then stop the loop.

        Best effort only. If the process is killed outright this never runs —
        which is exactly the case the server's missing-heartbeat detection
        exists to catch.
        """
        logger.info("Engine stopping: %s", reason)
        self._stop.set()
        self._wake.set()

        try:
            self.client.report_event("agent_stopped", _now_iso(), {"reason": reason})
        except ApiError as exc:
            logger.warning("Could not report shutdown: %s", exc.message)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def nudge(self) -> None:
        """Send a heartbeat now instead of waiting for the next interval."""
        self._wake.set()

    # -- The loop -----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - the loop must outlive any single error
                logger.exception("Heartbeat tick failed")

            interval = max(30, min(60, self.config.heartbeat_interval_seconds))
            self._wake.wait(timeout=interval)
            self._wake.clear()

    def _tick(self) -> None:
        idle_seconds, is_locked = self._read_signals()

        with self._lock:
            self.state.local_idle_seconds = idle_seconds
            self.state.is_locked = is_locked

        self._report_lock_transition(is_locked)

        try:
            response = self.client.heartbeat(idle_seconds, is_locked)
        except ApiError as exc:
            self._handle_failure(exc)
            self._publish()
            return

        self._consecutive_failures = 0
        self._apply_response(response)
        self._flush_buffer()
        self._publish()

    def _read_signals(self) -> tuple[int, bool]:
        try:
            idle_seconds = max(0, int(self.detector.idle_seconds()))
        except Exception:  # noqa: BLE001
            logger.exception("Idle detection failed; reporting zero")
            idle_seconds = 0

        try:
            is_locked = bool(self.detector.is_locked())
        except Exception:  # noqa: BLE001
            is_locked = False

        return idle_seconds, is_locked

    def _report_lock_transition(self, is_locked: bool) -> None:
        if is_locked == self._was_locked:
            return
        self._was_locked = is_locked
        self.report_event("screen_locked" if is_locked else "screen_unlocked")

    def _apply_response(self, response: dict[str, Any]) -> None:
        totals = response.get("totals") or {}
        break_info = response.get("break") or {}

        # Policy changes made by an admin take effect on the next heartbeat.
        if config := response.get("config"):
            self.config.apply_server_config(config)

        with self._lock:
            self.state.connected = True
            self.state.last_error = None
            self.state.last_heartbeat_at = datetime.now(timezone.utc)
            self.state.state = response.get("state", "clocked_out")
            self.state.detail = response.get("detail") or ""
            self.state.clocked_in = bool(response.get("clocked_in"))
            self.state.session_id = response.get("session_id")
            self.state.clock_in_at = response.get("clock_in_at")
            self.state.break_type = break_info.get("type")
            self.state.break_remaining_seconds = break_info.get("remaining_seconds")
            self.state.active_seconds = totals.get("active_seconds", 0)
            self.state.idle_seconds = totals.get("idle_seconds", 0)
            self.state.break_seconds = totals.get("break_seconds", 0)
            self.state.worked_seconds = totals.get("worked_seconds", 0)
            if response.get("breaks_used"):
                self.state.breaks_used = response["breaks_used"]

    def _handle_failure(self, exc: ApiError) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "Heartbeat failed (%s consecutive): %s",
            self._consecutive_failures,
            exc.message,
        )
        with self._lock:
            self.state.last_error = exc.message
            if exc.is_auth:
                # Enrolment was revoked; the UI must send the user back to sign-in.
                self.state.connected = False
                self.state.detail = "This laptop needs to be enrolled again"
            elif self._consecutive_failures >= OFFLINE_AFTER_FAILURES:
                self.state.connected = False
                self.state.detail = "Cannot reach the server"

    def _flush_buffer(self) -> None:
        events = self.buffer.take_all()
        if not events:
            return
        try:
            self.client.report_events(events)
            logger.info("Flushed %s buffered events", len(events))
        except ApiError as exc:
            logger.warning("Could not flush buffered events: %s", exc.message)
            self.buffer.restore(events)

    # -- Employee actions ---------------------------------------------------
    def clock_in(self) -> tuple[bool, str]:
        return self._action(self.client.clock_in, "Clocked in")

    def clock_out(self) -> tuple[bool, str]:
        return self._action(self.client.clock_out, "Clocked out")

    def start_break(self, break_type: str) -> tuple[bool, str]:
        return self._action(
            lambda: self.client.start_break(break_type),
            "Lunch break started" if break_type == "lunch" else "Break started",
        )

    def end_break(self) -> tuple[bool, str]:
        return self._action(self.client.end_break, "Break ended")

    def _action(self, call: Callable[[], dict], success_message: str) -> tuple[bool, str]:
        """Run an action, then immediately re-sync so the UI cannot lag."""
        try:
            result = call()
        except ApiError as exc:
            logger.info("Action refused: %s", exc.message)
            return False, exc.message

        self.nudge()
        return True, result.get("detail") or success_message

    def report_event(
        self, event_type: str, payload: Optional[dict[str, Any]] = None
    ) -> None:
        """Send a lifecycle event, buffering it locally if the server is down."""
        record = {
            "event_type": event_type,
            "occurred_at": _now_iso(),
            "payload": payload,
        }
        try:
            self.client.report_event(event_type, record["occurred_at"], payload)
        except ApiError as exc:
            if exc.is_auth:
                logger.warning("Not reporting %s: %s", event_type, exc.message)
                return
            logger.info("Buffering %s until the server is reachable", event_type)
            self.buffer.add(record)

    def refresh_now(self) -> None:
        """Pull a full status snapshot, used when a window opens."""
        try:
            response = self.client.status()
        except ApiError as exc:
            self._handle_failure(exc)
            self._publish()
            return

        with self._lock:
            self.state.connected = True
            self.state.last_error = None
            self.state.state = response.get("state", "clocked_out")
            self.state.detail = response.get("detail") or ""
            self.state.clocked_in = response.get("state") != "clocked_out"
            self.state.session_id = response.get("session_id")
            self.state.active_seconds = response.get("active_seconds", 0)
            self.state.idle_seconds = response.get("idle_seconds", 0)
            self.state.break_seconds = response.get("break_seconds", 0)
            self.state.worked_seconds = response.get("worked_seconds", 0)
            self.state.break_type = response.get("break_type")
            self.state.break_remaining_seconds = response.get("break_remaining_seconds")
            if response.get("breaks_used"):
                self.state.breaks_used = response["breaks_used"]
        self._publish()
