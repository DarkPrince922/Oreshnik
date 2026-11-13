from celery import Celery

from app.core.config import settings

celery = Celery(
    "oreshnik",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
)


@celery.task(name="oreshnik.ping")
def ping() -> str:
    """Smoke-test task — confirms the worker is wired up."""
    return "pong"
