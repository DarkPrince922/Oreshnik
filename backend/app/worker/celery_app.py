from celery import Celery

from app.core.config import settings

celery = Celery(
    "oreshnik",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.worker.tasks"],
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Agent turns last minutes and have their own worker (docker-compose: agent-worker), so they cannot starve scans.
    task_routes={"oreshnik.agent_turn": {"queue": "agent"}},
    result_expires=3600,
    # Scans run for minutes to hours: a worker process must take one job at a time instead of
    # hoarding queued scans it can't start yet.
    worker_prefetch_multiplier=1,
    beat_schedule={
        "reap-stale-scans": {"task": "oreshnik.reap_stale_scans", "schedule": 60.0},
        "reap-stale-agent-turns": {"task": "oreshnik.reap_stale_agent_turns", "schedule": 60.0},
        "run-due-schedules": {"task": "oreshnik.run_due_schedules", "schedule": 30.0},
        **(
            {"check-ssh-runners": {"task": "oreshnik.check_ssh_runners", "schedule": float(settings.ssh_check_interval_seconds)}}
            if settings.ssh_check_interval_seconds > 0
            else {}
        ),
    },
)


@celery.task(name="oreshnik.ping")
def ping() -> str:
    """Smoke-test task — confirms the worker is wired up."""
    return "pong"
