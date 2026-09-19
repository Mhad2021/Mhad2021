"""Password hashing, token issuance and verification."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
import jwt

from app.config import settings

ALGORITHM = "HS256"
BCRYPT_ROUNDS = 12
# bcrypt silently truncates beyond 72 bytes; reject rather than accept a
# password whose tail is ignored.
MAX_PASSWORD_BYTES = 72
ACCESS_AUDIENCE = "presence:dashboard"
AGENT_AUDIENCE = "presence:agent"


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes long"
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:MAX_PASSWORD_BYTES], hashed.encode())
    except (ValueError, TypeError):
        return False


def password_problems(password: str) -> list[str]:
    """Return a list of human-readable reasons the password is unacceptable."""
    problems: list[str] = []
    if len(password) < settings.password_min_length:
        problems.append(
            f"must be at least {settings.password_min_length} characters long"
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        problems.append(f"must be at most {MAX_PASSWORD_BYTES} bytes long")
    if not any(c.isalpha() for c in password):
        problems.append("must contain at least one letter")
    if not any(c.isdigit() for c in password):
        problems.append("must contain at least one number")
    if password.lower() in {"password123", "presence123", "welcome123"}:
        problems.append("is too common")
    return problems


# --------------------------------------------------------------------------- #
# Opaque tokens (device tokens, refresh tokens, enrollment codes)
# --------------------------------------------------------------------------- #
def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def generate_temp_password(length: int = 14) -> str:
    """A random password guaranteed to satisfy ``password_problems``."""
    import string

    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if not password_problems(candidate):
            return candidate


def generate_enrollment_code() -> str:
    """Short, human-transcribable code: ABCD-1234-EFGH."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1
    groups = [
        "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)
    ]
    return "-".join(groups)


def hash_token(token: str) -> str:
    """Fast one-way hash for high-volume opaque tokens.

    Device tokens are 256 bits of entropy from a CSPRNG, so a slow KDF buys
    nothing here — but the digest is keyed with the app secret so a stolen
    database alone cannot be used to forge lookups.
    """
    return hmac.new(
        settings.secret_key.encode(), token.encode(), hashlib.sha256
    ).hexdigest()


def verify_token_hash(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash)


# --------------------------------------------------------------------------- #
# JWTs
# --------------------------------------------------------------------------- #
def create_access_token(
    subject: int,
    role: str,
    extra: Optional[dict[str, Any]] = None,
    ttl_minutes: Optional[int] = None,
) -> str:
    now = datetime.now(timezone.utc)
    ttl = ttl_minutes or settings.access_token_ttl_minutes
    payload: dict[str, Any] = {
        "sub": str(subject),
        "role": role,
        "aud": ACCESS_AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=ttl),
        "jti": secrets.token_hex(8),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError subclasses on any problem."""
    return jwt.decode(
        token,
        settings.secret_key,
        algorithms=[ALGORITHM],
        audience=ACCESS_AUDIENCE,
    )


def refresh_token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_ttl_days)


def enrollment_code_expiry(hours: int = 72) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)
