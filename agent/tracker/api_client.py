"""HTTP client for the Presence server.

Uses the standard library only: the packaged agent should be small and should
not drag a TLS stack of its own onto 200 laptops.
"""
from __future__ import annotations

import json
import logging
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any, Optional

from tracker.config import AgentConfig, device_info

logger = logging.getLogger(__name__)

TIMEOUT = 20


class ApiError(Exception):
    """A request failed. ``status`` is None for network-level failures."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status
        self.message = message

    @property
    def is_network(self) -> bool:
        return self.status is None

    @property
    def is_auth(self) -> bool:
        return self.status in (401, 403)


class ApiClient:
    def __init__(self, config: AgentConfig):
        self.config = config
        self._ssl_context = (
            ssl.create_default_context()
            if config.verify_tls
            else ssl._create_unverified_context()  # noqa: SLF001 - opt-in for self-signed internal CAs
        )

    # -- Plumbing -----------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.config.api_base}{path}"
        body = json.dumps(payload).encode() if payload is not None else None

        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", f"PresenceAgent/{device_info()['agent_version']}")
        if authenticated:
            if not self.config.device_token:
                raise ApiError("This laptop is not enrolled yet", status=401)
            request.add_header("X-Device-Token", self.config.device_token)

        try:
            with urllib.request.urlopen(
                request, timeout=TIMEOUT, context=self._ssl_context
            ) as response:
                raw = response.read().decode("utf-8") or "{}"
                return json.loads(raw)

        except urllib.error.HTTPError as exc:
            detail = self._extract_detail(exc)
            if exc.code >= 500:
                logger.warning("Server error %s on %s: %s", exc.code, path, detail)
            raise ApiError(detail, status=exc.code) from exc

        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            raise ApiError(f"Cannot reach the server: {exc}", status=None) from exc

        except json.JSONDecodeError as exc:
            raise ApiError("The server sent an unreadable response") from exc

    @staticmethod
    def _extract_detail(exc: urllib.error.HTTPError) -> str:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = body.get("detail")
            if isinstance(detail, str):
                return detail
            if isinstance(detail, list) and detail:
                return str(detail[0].get("msg", exc.reason))
        except Exception:  # noqa: BLE001
            pass
        return f"{exc.code} {exc.reason}"

    # -- Enrolment and sign-in ---------------------------------------------
    def enroll(self, enrollment_code: str) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/agent/enroll",
            {"enrollment_code": enrollment_code, "device": device_info()},
            authenticated=False,
        )
        self._store_credentials(result)
        return result

    def login(self, username: str, password: str) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/agent/login",
            {"username": username, "password": password, "device": device_info()},
            authenticated=False,
        )
        self._store_credentials(result)
        return result

    def _store_credentials(self, result: dict[str, Any]) -> None:
        self.config.device_token = result.get("device_token")
        self.config.full_name = result.get("full_name", "")
        self.config.employee_code = result.get("employee_code", "")
        self.config.team_leader = result.get("team_leader") or ""
        self.config.monitoring_notice_accepted = bool(
            result.get("monitoring_notice_accepted")
        )
        self.config.apply_server_config(result.get("config", {}))
        self.config.save()

    # -- Day-to-day ---------------------------------------------------------
    def heartbeat(self, idle_seconds: int, is_locked: bool) -> dict[str, Any]:
        """Send one heartbeat and return the server's directives verbatim.

        Applying the policy that comes back is the engine's job — the client
        only moves bytes.
        """
        info = device_info()
        return self._request(
            "POST",
            "/agent/heartbeat",
            {
                "idle_seconds": idle_seconds,
                "is_locked": is_locked,
                "agent_version": info["agent_version"],
                "hostname": info["hostname"],
                "platform": info["platform"],
                "os_version": info["os_version"],
            },
        )

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/agent/status")

    def clock_in(self) -> dict[str, Any]:
        return self._request("POST", "/agent/clock-in", {})

    def clock_out(self) -> dict[str, Any]:
        return self._request("POST", "/agent/clock-out", {})

    def start_break(self, break_type: str) -> dict[str, Any]:
        return self._request("POST", "/agent/break/start", {"break_type": break_type})

    def end_break(self) -> dict[str, Any]:
        return self._request("POST", "/agent/break/end", {})

    def accept_notice(self) -> dict[str, Any]:
        return self._request("POST", "/agent/accept-notice", {"accepted": True})

    def report_event(
        self,
        event_type: str,
        occurred_at: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/agent/events",
            {
                "event_type": event_type,
                "occurred_at": occurred_at,
                "payload": payload,
            },
        )

    def report_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("POST", "/agent/events/batch", {"events": events})
