"""Agent-side unit tests: config, buffering and the engine's state handling."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def workdir(monkeypatch):
    path = Path(tempfile.mkdtemp(prefix="presence-test-"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(path))
    monkeypatch.setenv("APPDATA", str(path))
    return path


class TestConfig:
    def test_defaults_are_sane(self, workdir):
        from tracker.config import AgentConfig

        config = AgentConfig()
        assert config.idle_threshold_seconds == 600
        assert 30 <= config.heartbeat_interval_seconds <= 60
        assert config.short_break_max_seconds == 600

    def test_api_base_is_built_from_the_server_url(self, workdir):
        from tracker.config import AgentConfig

        config = AgentConfig(server_url="https://presence.example.com/")
        assert config.api_base == "https://presence.example.com/api/v1"

    def test_state_round_trips_through_disk(self, workdir):
        from tracker.config import AgentConfig

        config = AgentConfig()
        config.device_token = "secret-token"
        config.full_name = "John Doe"
        config.save()

        reloaded = AgentConfig.load()
        assert reloaded.device_token == "secret-token"
        assert reloaded.full_name == "John Doe"

    def test_the_environment_overrides_the_server_url(self, workdir, monkeypatch):
        from tracker.config import AgentConfig

        monkeypatch.setenv("PRESENCE_SERVER_URL", "http://localhost:9999")
        assert AgentConfig.load().server_url == "http://localhost:9999"

    def test_server_policy_is_adopted(self, workdir):
        from tracker.config import AgentConfig

        config = AgentConfig()
        config.apply_server_config(
            {"idle_threshold_seconds": 300, "unknown_key": "ignored"}
        )
        assert config.idle_threshold_seconds == 300
        assert not hasattr(config, "unknown_key")

    def test_the_device_id_is_stable(self, workdir):
        from tracker.config import device_uid

        assert device_uid() == device_uid()


class TestEventBuffer:
    def test_events_survive_a_restart(self, workdir):
        from tracker.storage import EventBuffer

        path = workdir / "buffer.json"
        EventBuffer(path).add({"event_type": "screen_locked"})

        assert EventBuffer(path).count() == 1

    def test_taking_events_clears_the_buffer(self, workdir):
        from tracker.storage import EventBuffer

        buffer = EventBuffer(workdir / "buffer.json")
        buffer.add({"event_type": "a"})
        buffer.add({"event_type": "b"})

        assert len(buffer.take_all()) == 2
        assert buffer.count() == 0

    def test_a_failed_flush_puts_the_events_back(self, workdir):
        from tracker.storage import EventBuffer

        buffer = EventBuffer(workdir / "buffer.json")
        buffer.add({"event_type": "a"})
        taken = buffer.take_all()
        buffer.restore(taken)

        assert buffer.count() == 1

    def test_the_buffer_is_bounded(self, workdir):
        from tracker.storage import MAX_BUFFERED_EVENTS, EventBuffer

        buffer = EventBuffer(workdir / "buffer.json")
        for index in range(MAX_BUFFERED_EVENTS + 50):
            buffer.add({"event_type": "noise", "n": index})

        assert buffer.count() == MAX_BUFFERED_EVENTS

    def test_a_corrupt_buffer_does_not_crash_the_agent(self, workdir):
        from tracker.storage import EventBuffer

        path = workdir / "buffer.json"
        path.write_text("{ not valid json")

        buffer = EventBuffer(path)
        assert buffer.count() == 0
        buffer.add({"event_type": "a"})
        assert buffer.count() == 1


class TestIdleDetection:
    def test_a_detector_is_always_returned(self, workdir):
        from tracker.idle import build_detector

        detector = build_detector()
        assert isinstance(detector.idle_seconds(), int)
        assert detector.idle_seconds() >= 0

    def test_the_fallback_never_reports_false_inactivity(self):
        from tracker.idle.base import NullIdleDetector

        # Zero means "no evidence of inactivity", so an unsupported platform
        # never generates a spurious alert about an employee.
        assert NullIdleDetector().idle_seconds() == 0
        assert NullIdleDetector().is_locked() is False


class TestEngine:
    def _engine(self, workdir):
        from tracker.config import AgentConfig
        from tracker.engine import TrackerEngine

        config = AgentConfig(device_token="test-token")
        engine = TrackerEngine(config, client=MagicMock())
        return engine

    def test_a_heartbeat_response_updates_the_published_state(self, workdir):
        engine = self._engine(workdir)
        engine.client.heartbeat.return_value = {
            "state": "idle",
            "detail": "No keyboard or mouse activity",
            "clocked_in": True,
            "session_id": 7,
            "totals": {"active_seconds": 3600, "idle_seconds": 700,
                       "break_seconds": 0, "worked_seconds": 4300},
            "config": {"idle_threshold_seconds": 900},
        }

        engine._tick()

        assert engine.state.state == "idle"
        assert engine.state.session_id == 7
        assert engine.state.active_seconds == 3600
        assert engine.state.connected is True
        # Policy pushed by the server is adopted immediately.
        assert engine.config.idle_threshold_seconds == 900

    def test_repeated_failures_mark_the_agent_disconnected(self, workdir):
        from tracker.api_client import ApiError

        engine = self._engine(workdir)
        engine.client.heartbeat.side_effect = ApiError("Cannot reach the server")

        engine._tick()
        assert engine.state.connected is False or engine.state.last_error
        engine._tick()
        assert engine.state.connected is False
        assert "reach" in engine.state.detail.lower()

    def test_a_rejected_token_asks_the_employee_to_re_enroll(self, workdir):
        from tracker.api_client import ApiError

        engine = self._engine(workdir)
        engine.client.heartbeat.side_effect = ApiError("revoked", status=401)

        engine._tick()

        assert engine.state.connected is False
        assert "enroll" in engine.state.detail.lower()

    def test_a_refused_action_returns_the_servers_reason(self, workdir):
        from tracker.api_client import ApiError

        engine = self._engine(workdir)
        engine.client.start_break.side_effect = ApiError(
            "You have already used all 2 10-minute break(s) allowed today."
        )

        ok, message = engine.start_break("short")

        assert not ok
        assert "already used all" in message

    def test_lock_transitions_are_reported_once(self, workdir):
        engine = self._engine(workdir)
        engine.client.heartbeat.return_value = {"state": "active", "config": {}}
        engine.detector = MagicMock()
        engine.detector.idle_seconds.return_value = 5
        engine.detector.is_locked.return_value = True

        engine._tick()
        engine._tick()  # still locked — must not re-report

        locks = [
            call for call in engine.client.report_event.call_args_list
            if call[0][0] == "screen_locked"
        ]
        assert len(locks) == 1

    def test_a_broken_idle_detector_does_not_stop_the_heartbeat(self, workdir):
        engine = self._engine(workdir)
        engine.client.heartbeat.return_value = {"state": "active", "config": {}}
        engine.detector = MagicMock()
        engine.detector.idle_seconds.side_effect = OSError("API failed")
        engine.detector.is_locked.return_value = False

        engine._tick()

        # The heartbeat still went out, reporting zero rather than nothing.
        engine.client.heartbeat.assert_called_once()
        assert engine.client.heartbeat.call_args[0][0] == 0

    def test_a_failing_listener_does_not_break_tracking(self, workdir):
        engine = self._engine(workdir)
        engine.client.heartbeat.return_value = {"state": "active", "config": {}}
        engine.subscribe(lambda _state: (_ for _ in ()).throw(RuntimeError("boom")))

        engine._tick()  # must not raise

        assert engine.state.connected is True


class TestApiClient:
    def test_network_errors_are_distinguished_from_auth_errors(self):
        from tracker.api_client import ApiError

        assert ApiError("down").is_network
        assert not ApiError("down").is_auth
        assert ApiError("nope", status=401).is_auth
        assert not ApiError("nope", status=401).is_network

    def test_calling_without_a_token_fails_before_any_request(self):
        from tracker.api_client import ApiClient, ApiError
        from tracker.config import AgentConfig

        client = ApiClient(AgentConfig(device_token=None))
        with pytest.raises(ApiError) as excinfo:
            client.heartbeat(10, False)
        assert excinfo.value.status == 401
