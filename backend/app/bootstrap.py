"""First-run initialisation: an admin account and a mock runner.

Runs on API startup and is idempotent — it only acts on empty tables.
"""

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.models.runner import Runner, RunnerKind, RunnerStatus
from app.models.user import Role, User

log = logging.getLogger("oreshnik.bootstrap")


def ensure_admin(db: Session) -> None:
    if db.scalar(select(User.id).limit(1)) is not None:
        return
    password = settings.admin_password
    generated = not password
    if generated:
        password = secrets.token_urlsafe(12)
    db.add(
        User(
            email=settings.admin_email.strip().lower(),
            password_hash=hash_password(password),
            role=Role.admin,
        )
    )
    db.commit()
    if generated:
        # Shown once: the password is not stored anywhere else.
        log.warning(
            "Created initial admin %s with generated password: %s  (change it after login)",
            settings.admin_email,
            password,
        )
    else:
        log.warning("Created initial admin %s from ADMIN_PASSWORD", settings.admin_email)


def ensure_mock_runner(db: Session) -> None:
    if db.scalar(select(Runner.id).limit(1)) is not None:
        return
    db.add(
        Runner(
            name="mock",
            kind=RunnerKind.mock,
            status=RunnerStatus.online,
            status_detail="Симуляция запуска Strix — токены не тратятся",
        )
    )
    db.commit()
    log.info("Created default mock runner")


def run_bootstrap(db: Session) -> None:
    ensure_admin(db)
    ensure_mock_runner(db)
