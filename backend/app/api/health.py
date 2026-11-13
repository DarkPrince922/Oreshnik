from fastapi import APIRouter
from sqlalchemy import text

from app import __version__
from app.core.config import settings
from app.core.db import engine

router = APIRouter(prefix="/api", tags=["health"])


def _check_db() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _check_redis() -> bool:
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
        return bool(client.ping())
    except Exception:
        return False


@router.get("/health")
def health() -> dict:
    db_ok = _check_db()
    redis_ok = _check_redis()
    return {
        "status": "ok" if (db_ok and redis_ok) else "degraded",
        "version": __version__,
        "env": settings.oreshnik_env,
        "services": {"database": db_ok, "redis": redis_ok},
    }
