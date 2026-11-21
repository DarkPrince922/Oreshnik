"""Password hashing and JWT helpers. Auth routes land in Phase 1."""

import base64
import hashlib
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings


def _prehash(password: str) -> bytes:
    # bcrypt rejects inputs > 72 bytes; a SHA-256 pre-hash keeps any length safe.
    return base64.b64encode(hashlib.sha256(password.encode()).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), hashed.encode())
    except ValueError:
        return False


def create_access_token(subject: str, extra: dict | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def create_listener_token(session_id: int, session_key: str, user_id: int, ttl_seconds: int) -> str:
    """A short-lived token the agent presents to open ITS OWN session's listener (typ 'lstn'). Scoped to one session and user,
    so even when self-listeners are enabled a session can act only for itself. TTL ties it to the turn that issued it."""
    now = datetime.now(timezone.utc)
    payload = {
        "typ": "lstn", "sid": session_id, "skey": session_key, "uid": user_id,
        "iat": now, "exp": now + timedelta(seconds=max(60, ttl_seconds)),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_listener_token(token: str) -> dict:
    data = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if data.get("typ") != "lstn":
        raise jwt.InvalidTokenError("not a listener token")
    return data
