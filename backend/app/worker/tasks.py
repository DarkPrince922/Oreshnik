from datetime import datetime, timezone

from app.core.db import SessionLocal
from app.models.runner import Runner, RunnerStatus
from app.services.runner_health import check_runner
from app.worker.celery_app import celery


@celery.task(name="oreshnik.runner_check")
def runner_check(runner_id: int) -> dict:
    """Health-check a runner from the worker's point of view and persist the result."""
    with SessionLocal() as db:
        runner = db.get(Runner, runner_id)
        if runner is None:
            return {"status": "error", "detail": "runner not found"}
        status, detail = check_runner(runner)
        runner.status = status
        runner.status_detail = detail
        if status == RunnerStatus.online:
            runner.last_seen_at = datetime.now(timezone.utc)
        db.commit()
        return {"status": status.value, "detail": detail}
