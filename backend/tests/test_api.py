"""API contract, authentication and role-based permissions."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.core.security import generate_token, hash_token
from app.main import app


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def admin(db):
    from app.core.security import hash_password
    from app.models import User
    from app.models.enums import Role

    user = User(
        employee_code="ADM-1", username="admin", email="admin@example.com",
        full_name="Alex Morgan", password_hash=hash_password("TestPass12345"),
        role=Role.ADMIN, must_change_password=False,
    )
    db.add(user)
    db.commit()
    return user


def auth_headers(client, username: str, password: str = "TestPass12345") -> dict:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def agent_headers(db, device):
    raw = generate_token()
    device.token_hash = hash_token(raw)
    db.commit()
    return {"X-Device-Token": raw}


class TestAuthentication:
    def test_valid_credentials_return_a_token_pair(self, client, employee):
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "john.doe", "password": "TestPass12345"},
        )
        assert response.status_code == 200
        assert "access_token" in response.json()
        assert "refresh_token" in response.json()

    def test_wrong_password_is_rejected(self, client, employee):
        response = client.post(
            "/api/v1/auth/login", json={"username": "john.doe", "password": "nope"}
        )
        assert response.status_code == 401

    def test_unknown_user_gives_the_same_error(self, client, employee):
        response = client.post(
            "/api/v1/auth/login", json={"username": "ghost", "password": "nope"}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Incorrect username or password"

    def test_repeated_failures_lock_the_account(self, client, employee, db):
        from app.config import settings

        for _ in range(settings.max_failed_logins):
            client.post(
                "/api/v1/auth/login", json={"username": "john.doe", "password": "bad"}
            )
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "john.doe", "password": "TestPass12345"},
        )
        assert response.status_code == 423

    def test_endpoints_reject_an_absent_token(self, client):
        assert client.get("/api/v1/auth/me").status_code == 401

    def test_endpoints_reject_a_forged_token(self, client):
        response = client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-jwt"}
        )
        assert response.status_code == 401

    def test_deactivated_accounts_cannot_sign_in(self, client, employee, db):
        employee.is_active = False
        db.commit()
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "john.doe", "password": "TestPass12345"},
        )
        assert response.status_code == 403


class TestPermissions:
    def test_employees_cannot_see_the_live_board(self, client, employee, policy):
        headers = auth_headers(client, "john.doe")
        assert client.get("/api/v1/live/board", headers=headers).status_code == 403

    def test_employees_cannot_change_policy(self, client, employee, policy):
        headers = auth_headers(client, "john.doe")
        response = client.patch(
            "/api/v1/settings/policy",
            headers=headers,
            json={"idle_threshold_seconds": 60},
        )
        assert response.status_code == 403

    def test_team_leaders_cannot_change_policy(self, client, leader, policy):
        headers = auth_headers(client, "leader")
        response = client.patch(
            "/api/v1/settings/policy",
            headers=headers,
            json={"idle_threshold_seconds": 60},
        )
        assert response.status_code == 403

    def test_team_leaders_can_see_their_own_team(self, client, leader, employee, policy):
        headers = auth_headers(client, "leader")
        response = client.get("/api/v1/live/board", headers=headers)
        assert response.status_code == 200
        assert "John Doe" in [e["full_name"] for e in response.json()["employees"]]

    def test_team_leaders_cannot_see_another_leaders_team(
        self, client, db, leader, employee, policy
    ):
        from app.core.security import hash_password
        from app.models import User
        from app.models.enums import Role

        other = User(
            employee_code="TL-2", username="other.leader",
            email="other@example.com", full_name="Sam Patel",
            password_hash=hash_password("TestPass12345"),
            role=Role.TEAM_LEADER, must_change_password=False,
        )
        db.add(other)
        db.commit()

        headers = auth_headers(client, "other.leader")
        response = client.get("/api/v1/live/board", headers=headers)
        assert "John Doe" not in [e["full_name"] for e in response.json()["employees"]]

    def test_admins_can_change_policy(self, client, admin, policy):
        headers = auth_headers(client, "admin")
        response = client.patch(
            "/api/v1/settings/policy",
            headers=headers,
            json={"idle_threshold_seconds": 900},
        )
        assert response.status_code == 200
        assert response.json()["idle_threshold_seconds"] == 900

    def test_employees_can_always_see_their_own_status(self, client, employee, policy):
        headers = auth_headers(client, "john.doe")
        assert client.get("/api/v1/live/me", headers=headers).status_code == 200


class TestAgentApi:
    def test_heartbeat_requires_a_device_token(self, client, policy):
        response = client.post("/api/v1/agent/heartbeat", json={"idle_seconds": 5})
        assert response.status_code == 401

    def test_a_revoked_device_is_refused(self, client, db, device, agent_headers, policy):
        device.is_active = False
        db.commit()
        response = client.post(
            "/api/v1/agent/heartbeat", headers=agent_headers, json={"idle_seconds": 5}
        )
        assert response.status_code == 401

    def test_clock_in_then_heartbeat_reports_active(
        self, client, agent_headers, policy, employee
    ):
        assert client.post("/api/v1/agent/clock-in", headers=agent_headers).status_code == 200
        response = client.post(
            "/api/v1/agent/heartbeat", headers=agent_headers, json={"idle_seconds": 5}
        )
        assert response.json()["state"] == "active"

    def test_breaks_cannot_be_started_before_clocking_in(
        self, client, agent_headers, policy, employee
    ):
        response = client.post(
            "/api/v1/agent/break/start",
            headers=agent_headers,
            json={"break_type": "short"},
        )
        assert response.status_code == 409

    def test_heartbeat_rejects_a_negative_idle_time(
        self, client, agent_headers, policy, employee
    ):
        response = client.post(
            "/api/v1/agent/heartbeat", headers=agent_headers, json={"idle_seconds": -5}
        )
        assert response.status_code == 422

    def test_the_agent_receives_the_current_policy(
        self, client, agent_headers, policy, employee
    ):
        response = client.get("/api/v1/agent/config", headers=agent_headers)
        assert response.status_code == 200
        assert response.json()["config"]["idle_threshold_seconds"] == 600

    def test_enrollment_codes_are_single_use(self, client, db, admin, employee, policy):
        headers = auth_headers(client, "admin")
        issued = client.post(
            f"/api/v1/employees/{employee.id}/enrollment-code", headers=headers
        )
        code = issued.json()["code"]

        first = client.post(
            "/api/v1/agent/enroll",
            json={"enrollment_code": code, "device": {"device_uid": "LAPTOP-A-0001"}},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/v1/agent/enroll",
            json={"enrollment_code": code, "device": {"device_uid": "LAPTOP-B-0002"}},
        )
        assert second.status_code == 400


class TestPolicyValidation:
    def test_heartbeat_grace_must_exceed_twice_the_interval(self, client, admin, policy):
        headers = auth_headers(client, "admin")
        response = client.patch(
            "/api/v1/settings/policy",
            headers=headers,
            json={"heartbeat_interval_seconds": 60, "heartbeat_grace_seconds": 90},
        )
        assert response.status_code == 422
        assert "twice" in response.json()["detail"]

    def test_heartbeat_interval_is_capped_to_the_30_60_second_band(
        self, client, admin, policy
    ):
        headers = auth_headers(client, "admin")
        assert client.patch(
            "/api/v1/settings/policy", headers=headers,
            json={"heartbeat_interval_seconds": 5},
        ).status_code == 422
        assert client.patch(
            "/api/v1/settings/policy", headers=headers,
            json={"heartbeat_interval_seconds": 600},
        ).status_code == 422


class TestHealth:
    def test_health_endpoint_reports_the_database(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
