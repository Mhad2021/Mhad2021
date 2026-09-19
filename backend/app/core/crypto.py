"""Symmetric encryption for integration credentials stored in the database."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)


def _fernet() -> Fernet:
    """Build the Fernet key.

    ENCRYPTION_KEY should be a urlsafe-base64 32-byte key (``Fernet.generate_key()``).
    If it is absent we derive one from SECRET_KEY so development works out of the
    box; production startup refuses to run without an explicit key.
    """
    key = settings.encryption_key.strip()
    if key:
        return Fernet(key.encode())
    derived = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_config(data: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_config(blob: str | None) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        return json.loads(_fernet().decrypt(blob.encode()).decode())
    except (InvalidToken, ValueError):
        logger.error("Failed to decrypt channel config — wrong ENCRYPTION_KEY?")
        return {}


def redact(config: dict[str, Any]) -> dict[str, Any]:
    """Mask secret-looking values for display in the dashboard."""
    secret_keys = {"token", "password", "secret", "api_key", "webhook_url", "auth_token"}
    out: dict[str, Any] = {}
    for key, value in config.items():
        if any(s in key.lower() for s in secret_keys) and isinstance(value, str) and value:
            out[key] = f"{value[:6]}…{value[-4:]}" if len(value) > 14 else "••••••••"
        else:
            out[key] = value
    return out
