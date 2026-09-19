"""Start the tracker when the employee signs in to Windows.

Two mechanisms, used together:

  * HKCU Run — per-user, survives a reinstall, needs no admin rights.
  * A Scheduled Task installed by the MSI — runs at logon *and* restarts the
    agent if it exits, which is what stops someone simply ending the task.

The Run key alone is easy to remove, so it is treated as convenience rather
than enforcement. Enforcement comes from the server: a laptop that stops
sending heartbeats is reported regardless of why it stopped.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from tracker.config import APP_NAME

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def executable_command() -> str:
    """The command that relaunches this agent, frozen or from source."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = Path(__file__).resolve().parents[2] / "run_agent.py"
    return f'"{sys.executable}" "{script}"'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except (ImportError, FileNotFoundError, OSError):
        return False


def enable() -> bool:
    """Register the agent to start at logon. Returns True on success."""
    if sys.platform != "win32":
        logger.debug("Autostart registration is Windows-only")
        return False
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(
                key, APP_NAME, 0, winreg.REG_SZ, executable_command()
            )
        logger.info("Registered %s to start at logon", APP_NAME)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Could not register autostart", exc_info=True)
        return False


def disable() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
        return True
    except (ImportError, FileNotFoundError, OSError):
        return False


def ensure_enabled() -> None:
    """Re-register on every start, so a cleared key repairs itself."""
    if sys.platform == "win32" and not is_enabled():
        enable()
