"""The browser tracking page and its API.

The load-bearing test here is that a web session never raises an inactivity
alert. A browser tab cannot see input outside itself, so an idle signal from it
would mean accusing someone of being away while they were working in another
application — and one false accusation costs the team's trust in the system.
"""
from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.core.timeutil import utcnow
from app.main import app
from app.models import Alert, WorkSession
from app.models.enums import AlertType, ClientKind
from app.services import attendance, monitor


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def signed_in(client, employee, policy):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "john.doe", "password": "TestPass12345"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _alerts_of(db, alert_type):
    return db.query(Alert).filter(Alert.alert_type == alert_type).all()


class TestTrackerApi:
    def test_status_before_clocking_in(self, client, signed_in):
        body = client.get("/api/v1/me/status", headers=signed_in).json()
        assert body["state"] == "clocked_out"
        assert body["employee"]["full_name"] == "John Doe"

    def test_clock_in_and_out(self, client, signed_in, db, employee):
        body = client.post("/api/v1/me/clock-in", headers=signed_in).json()
        assert body["state"] == "active"
        assert body["session_id"] is not None

        session = db.get(WorkSession, body["session_id"])
        assert session.client_kind is ClientKind.WEB

        body = client.post("/api/v1/me/clock-out", headers=signed_in).json()
        assert body["state"] == "clocked_out"

    def test_cannot_clock_in_twice(self, client, signed_in):
        client.post("/api/v1/me/clock-in", headers=signed_in)
        response = client.post("/api/v1/me/clock-in", headers=signed_in)
        assert response.status_code == 409

    def test_breaks_work_from_the_browser(self, client, signed_in):
        client.post("/api/v1/me/clock-in", headers=signed_in)

        body = client.post(
            "/api/v1/me/break/start", headers=signed_in, json={"break_type": "lunch"}
        ).json()
        assert body["state"] == "on_break"
        assert body["break"]["type"] == "lunch"
        assert body["break"]["allowed_seconds"] == 3600

        body = client.post("/api/v1/me/break/end", headers=signed_in).json()
        assert body["state"] == "active"
        assert body["break"] is None

    def test_break_limits_are_enforced(self, client, signed_in, policy):
        client.post("/api/v1/me/clock-in", headers=signed_in)
        for _ in range(policy.short_breaks_per_day):
            client.post(
                "/api/v1/me/break/start", headers=signed_in, json={"break_type": "short"}
            )
            client.post("/api/v1/me/break/end", headers=signed_in)

        response = client.post(
            "/api/v1/me/break/start", headers=signed_in, json={"break_type": "short"}
        )
        assert response.status_code == 409
        assert "already used all" in response.json()["detail"]

    def test_heartbeat_keeps_the_session_current(self, client, signed_in, db):
        body = client.post("/api/v1/me/clock-in", headers=signed_in).json()
        session = db.get(WorkSession, body["session_id"])
        session.last_heartbeat_at = utcnow() - timedelta(minutes=30)
        db.commit()

        client.post("/api/v1/me/heartbeat", headers=signed_in, json={"page_visible": True})
        db.refresh(session)
        assert (utcnow() - session.last_heartbeat_at).total_seconds() < 5

    def test_a_submitted_idle_time_is_ignored(self, client, signed_in, db, employee):
        """The schema has no idle field, so a client cannot inject one."""
        from app.api.v1.me import WebHeartbeat

        assert "idle_seconds" not in WebHeartbeat.model_fields

        client.post("/api/v1/me/clock-in", headers=signed_in)
        response = client.post(
            "/api/v1/me/heartbeat",
            headers=signed_in,
            json={"page_visible": True, "idle_seconds": 99999},
        )
        assert response.status_code == 200

        # The session is still active: the injected value changed nothing.
        session = attendance.get_open_session(db, employee.id)
        assert session.idle_seconds == 0
        assert response.json()["state"] == "active"

    def test_repeated_calls_reuse_one_web_device(self, client, signed_in, db, employee):
        """The device row is created once, not once per request."""
        from app.models import Device

        for _ in range(4):
            client.post("/api/v1/me/heartbeat", headers=signed_in, json={})

        rows = db.query(Device).filter(Device.user_id == employee.id).all()
        web_rows = [d for d in rows if d.device_uid.startswith("web:")]
        assert len(web_rows) == 1

    def test_endpoints_require_sign_in(self, client):
        for path in ("/api/v1/me/status", "/api/v1/me/clock-in"):
            method = client.get if path.endswith("status") else client.post
            assert method(path).status_code == 401


class TestWebSessionsNeverRaiseIdleAlerts:
    """The core guarantee of the browser client."""

    def test_a_silent_web_session_raises_no_inactivity_alert(
        self, client, signed_in, db, employee, policy
    ):
        body = client.post("/api/v1/me/clock-in", headers=signed_in).json()
        session = db.get(WorkSession, body["session_id"])

        # The tab has been shut for an hour — they may well be working.
        session.last_heartbeat_at = utcnow() - timedelta(hours=1)
        db.commit()

        monitor.run_tick(db)
        db.commit()

        assert _alerts_of(db, AlertType.IDLE_NO_BREAK) == []
        assert _alerts_of(db, AlertType.AGENT_OFFLINE) == []

    def test_the_same_silence_from_the_desktop_agent_does_alert(
        self, db, employee, policy, device
    ):
        """The contrast that shows the suppression is deliberate, not broken."""
        session = attendance.clock_in(
            db, employee, device=device, client_kind=ClientKind.AGENT
        )
        db.commit()
        session.last_heartbeat_at = utcnow() - timedelta(hours=1)
        db.commit()

        monitor.run_tick(db)
        db.commit()

        assert len(_alerts_of(db, AlertType.AGENT_OFFLINE)) == 1

    def test_break_overruns_still_alert_for_web_sessions(
        self, client, signed_in, db, employee, policy, rewind
    ):
        """Breaks are explicit button presses, so they are trustworthy."""
        body = client.post("/api/v1/me/clock-in", headers=signed_in).json()
        client.post(
            "/api/v1/me/break/start", headers=signed_in, json={"break_type": "short"}
        )
        session = db.get(WorkSession, body["session_id"])
        rewind(session, timedelta(minutes=15))

        monitor.run_tick(db)
        db.commit()

        assert len(_alerts_of(db, AlertType.BREAK_OVERRUN)) == 1

    def test_late_arrival_still_alerts_for_web_sessions(
        self, client, db, employee, leader, policy, schedule
    ):
        """Clock-in time is trustworthy whatever the client."""
        from datetime import datetime, time

        day = utcnow().date()
        while day.weekday() > 4:
            day -= timedelta(days=1)

        attendance.clock_in(
            db, employee,
            at=datetime.combine(day, time(9, 40), tzinfo=UTC),
            client_kind=ClientKind.WEB,
        )
        db.commit()

        assert len(_alerts_of(db, AlertType.LATE_ARRIVAL)) == 1


class TestBoardWording:
    def test_the_board_says_what_a_web_client_actually_knows(
        self, client, signed_in, db, employee, policy
    ):
        from app.services import presence

        client.post("/api/v1/me/clock-in", headers=signed_in)
        row = presence.build_row(db, employee)

        assert row.client_kind == "web"
        # Not "Working" — the page cannot see whether they are.
        assert "tracking page open" in row.detail.lower()

    def test_a_closed_tab_is_described_as_such(
        self, client, signed_in, db, employee, policy
    ):
        from app.services import presence

        body = client.post("/api/v1/me/clock-in", headers=signed_in).json()
        session = db.get(WorkSession, body["session_id"])
        session.last_heartbeat_at = utcnow() - timedelta(minutes=30)
        db.commit()

        row = presence.build_row(db, employee)
        assert "page closed" in row.detail.lower()
        assert "still clocked in" in row.detail.lower()

    def test_the_board_reports_which_client_is_in_use(
        self, client, signed_in, db, employee, policy
    ):
        from app.services import presence

        client.post("/api/v1/me/clock-in", headers=signed_in)
        payload = presence.build_row(db, employee).to_dict()

        assert payload["client_kind"] == "web"
        assert payload["tracks_idle"] is False
