"""Agent configuration and local state.

Machine-wide settings (the server URL) come from the installer and live in
ProgramData so an employee cannot point the agent at a different server.
Per-user state (the device token) lives in the user's own profile.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import socket
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

APP_NAME = "Presence"
AGENT_VERSION = "1.0.0"


def machine_config_dir() -> Path:
    """Installer-managed settings, readable by all users, writable by admins."""
    if sys.platform == "win32":
        base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    elif sys.platform == "darwin":
        base = Path("/Library/Application Support")
    else:
        base = Path("/etc")
    return base / APP_NAME


def user_config_dir() -> Path:
    """Per-user state: device token, cached status, buffered events."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


def log_dir() -> Path:
    path = user_config_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def device_uid() -> str:
    """A stable identifier for this laptop.

    Derived from the machine's hostname and MAC address so it survives
    reinstalls, with a persisted fallback if either changes.
    """
    cache = user_config_dir() / "device.id"
    if cache.exists():
        try:
            stored = cache.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        except OSError:
            logger.warning("Could not read the cached device id", exc_info=True)

    raw = f"{socket.gethostname()}-{uuid.getnode():x}"
    generated = str(uuid.uuid5(uuid.NAMESPACE_DNS, raw))

    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(generated, encoding="utf-8")
    except OSError:
        logger.warning("Could not persist the device id", exc_info=True)

    return generated


def device_info() -> dict[str, str]:
    """Hardware and OS facts sent with enrollment and heartbeats."""
    return {
        "device_uid": device_uid(),
        "hostname": socket.gethostname()[:160],
        "platform": platform.system()[:64],
        "os_version": f"{platform.release()} ({platform.version()})"[:120],
        "agent_version": AGENT_VERSION,
    }


@dataclass
class AgentConfig:
    """Everything the agent needs to reach the server and behave correctly."""

    server_url: str = "http://localhost:8000"
    device_token: Optional[str] = None
    verify_tls: bool = True
    # Server-supplied policy, refreshed on every heartbeat.
    idle_threshold_seconds: int = 600
    heartbeat_interval_seconds: int = 45
    short_break_max_seconds: int = 600
    lunch_break_max_seconds: int = 3600
    short_breaks_per_day: int = 2
    lunch_breaks_per_day: int = 1
    treat_lock_as_idle: bool = True
    # Cached identity, so the window can render before the first response.
    full_name: str = ""
    employee_code: str = ""
    team_leader: str = ""
    monitoring_notice_accepted: bool = False

    _machine_keys = ("server_url", "verify_tls")

    @property
    def api_base(self) -> str:
        return self.server_url.rstrip("/") + "/api/v1"

    # -- Loading ------------------------------------------------------------
    @classmethod
    def load(cls) -> "AgentConfig":
        config = cls()

        machine_file = machine_config_dir() / "config.json"
        if machine_file.exists():
            try:
                data = json.loads(machine_file.read_text(encoding="utf-8"))
                for key in cls._machine_keys:
                    if key in data:
                        setattr(config, key, data[key])
            except (OSError, json.JSONDecodeError):
                logger.error("Could not read %s", machine_file, exc_info=True)

        # The environment wins, which makes local development straightforward.
        if env_url := os.environ.get("PRESENCE_SERVER_URL"):
            config.server_url = env_url

        user_file = user_config_dir() / "state.json"
        if user_file.exists():
            try:
                data = json.loads(user_file.read_text(encoding="utf-8"))
                for key, value in data.items():
                    if hasattr(config, key) and not key.startswith("_"):
                        setattr(config, key, value)
            except (OSError, json.JSONDecodeError):
                logger.error("Could not read %s", user_file, exc_info=True)

        return config

    def save(self) -> None:
        """Persist per-user state only; machine settings are never rewritten."""
        path = user_config_dir() / "state.json"
        payload = {
            key: getattr(self, key)
            for key in (
                "device_token", "idle_threshold_seconds", "heartbeat_interval_seconds",
                "short_break_max_seconds", "lunch_break_max_seconds",
                "short_breaks_per_day", "lunch_breaks_per_day", "treat_lock_as_idle",
                "full_name", "employee_code", "team_leader",
                "monitoring_notice_accepted",
            )
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            if sys.platform != "win32":
                path.chmod(0o600)  # the device token is a credential
        except OSError:
            logger.error("Could not save agent state to %s", path, exc_info=True)

    def apply_server_config(self, data: dict[str, Any]) -> None:
        """Adopt the policy the server sent with the last heartbeat."""
        for key, value in (data or {}).items():
            if hasattr(self, key) and not key.startswith("_"):
                setattr(self, key, value)

    def clear_credentials(self) -> None:
        self.device_token = None
        self.save()
