"""Failed-login throttling backed by Redis.

Fails open: if Redis is unreachable, logins are not blocked (availability of the
panel wins over throttling; the bcrypt cost still limits guessing speed).
"""

import logging

import redis

from app.core.config import settings

log = logging.getLogger("oreshnik.throttle")

_client: "redis.Redis | None" = None


def _redis() -> "redis.Redis":
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=1, socket_timeout=1
        )
    return _client


def _key(email: str, ip: str) -> str:
    return f"oreshnik:login_fail:{email}:{ip}"


def is_blocked(email: str, ip: str) -> bool:
    try:
        count = _redis().get(_key(email, ip))
        return count is not None and int(count) >= settings.login_max_failures
    except Exception as exc:
        log.warning("login throttle unavailable: %s", exc)
        return False


def record_failure(email: str, ip: str) -> None:
    try:
        key = _key(email, ip)
        pipe = _redis().pipeline()
        pipe.incr(key)
        pipe.expire(key, settings.login_window_seconds)
        pipe.execute()
    except Exception as exc:
        log.warning("login throttle unavailable: %s", exc)


def reset(email: str, ip: str) -> None:
    try:
        _redis().delete(_key(email, ip))
    except Exception as exc:
        log.warning("login throttle unavailable: %s", exc)
