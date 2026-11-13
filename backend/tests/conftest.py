"""Test harness: a real Postgres database, created through the real Alembic migrations.

Connection settings come from the usual POSTGRES_* env vars (POSTGRES_HOST/PORT/USER/
PASSWORD); the database used is always `oreshnik_test`, created on demand.
"""

import os
from pathlib import Path

import psycopg
import pytest
from cryptography.fernet import Fernet

# Must be set before anything from `app` is imported (settings are read once).
os.environ["POSTGRES_DB"] = "oreshnik_test"
os.environ["SECRET_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["JWT_SECRET"] = "t" * 48
os.environ["ADMIN_EMAIL"] = "root@example.com"
os.environ["ADMIN_PASSWORD"] = "bootstrap-pass"

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal, engine  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models import LLMProfile, Provider, Role, Runner, RunnerKind, RunnerStatus, Scan, ScanMode, ScanStatus, Schedule, User  # noqa: E402
from app.core.crypto import encrypt  # noqa: E402


def _ensure_database() -> None:
    conninfo = (
        f"host={settings.postgres_host} port={settings.postgres_port} "
        f"user={settings.postgres_user} password={settings.postgres_password} dbname=postgres"
    )
    with psycopg.connect(conninfo, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (settings.postgres_db,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{settings.postgres_db}"')


@pytest.fixture(scope="session", autouse=True)
def _database():
    _ensure_database()
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users, llm_profiles, runners, scans, findings, schedules RESTART IDENTITY CASCADE"))

    # Scan artifacts go to a per-test directory; the simulated runner runs at full speed;
    # a stuck fake Strix is escalated quickly.
    monkeypatch.setattr(settings, "strix_runs_dir", str(tmp_path / "runs"))
    monkeypatch.setattr(settings, "mock_delay_seconds", 0.0)
    monkeypatch.setattr(settings, "strix_cancel_grace_seconds", 1)

    # In-memory stand-in for the Redis-backed login throttle.
    from app.services import throttle

    counts: dict[str, int] = {}
    monkeypatch.setattr(throttle, "is_blocked", lambda e, ip: counts.get(f"{e}|{ip}", 0) >= settings.login_max_failures)
    monkeypatch.setattr(throttle, "record_failure", lambda e, ip: counts.__setitem__(f"{e}|{ip}", counts.get(f"{e}|{ip}", 0) + 1))
    monkeypatch.setattr(throttle, "reset", lambda e, ip: counts.pop(f"{e}|{ip}", None))

    # Run Celery tasks inline instead of needing a broker/worker.
    from app.worker.celery_app import celery

    monkeypatch.setattr(celery.conf, "task_always_eager", True)
    monkeypatch.setattr(celery.conf, "task_eager_propagates", True)


@pytest.fixture
def client():
    # No `with`: skip the lifespan so bootstrap doesn't run implicitly.
    return TestClient_()


def TestClient_():
    from app.main import app

    return TestClient(app)


def make_user(email: str, password: str = "password123", role: Role = Role.operator, active: bool = True) -> User:
    with SessionLocal() as db:
        user = User(email=email, password_hash=hash_password(password), role=role, is_active=active)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


def auth_headers(client, email: str, password: str = "password123") -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def admin(client):
    make_user("admin@example.com", role=Role.admin)
    return auth_headers(client, "admin@example.com")


@pytest.fixture
def operator(client):
    make_user("op@example.com", role=Role.operator)
    return auth_headers(client, "op@example.com")


@pytest.fixture
def viewer(client):
    make_user("view@example.com", role=Role.viewer)
    return auth_headers(client, "view@example.com")


def make_runner(kind: RunnerKind = RunnerKind.mock, name: str | None = None) -> Runner:
    with SessionLocal() as db:
        runner = Runner(name=name or f"{kind.value}-runner", kind=kind, status=RunnerStatus.online)
        db.add(runner)
        db.commit()
        db.refresh(runner)
        return runner


def make_profile(user_id: int, name: str = "p", key: str = "sk-test-key-1234567890") -> LLMProfile:
    with SessionLocal() as db:
        profile = LLMProfile(
            user_id=user_id, name=name, provider=Provider.claude, model="claude-sonnet-4-6",
            api_key_enc=encrypt(key), api_key_hint="…7890",
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        return profile


def make_scan(user_id: int, runner_id: int | None, *, targets=None, instruction=None, profile_id=None,
              status: ScanStatus = ScanStatus.queued, mode: ScanMode = ScanMode.quick, **extra) -> Scan:
    with SessionLocal() as db:
        scan = Scan(user_id=user_id, runner_id=runner_id, llm_profile_id=profile_id,
                    targets=targets or ["https://example.com"], scan_mode=mode, instruction=instruction,
                    status=status, **extra)
        db.add(scan)
        db.commit()
        db.refresh(scan)
        return scan


def load_scan(scan_id: int) -> Scan:
    with SessionLocal() as db:
        return db.get(Scan, scan_id)


def make_schedule(user_id: int, runner_id: int | None, *, name: str = "nightly", cron: str = "0 3 * * *", tz: str = "UTC",
                  enabled: bool = True, next_run_at=None, profile_id=None, targets=None, **extra) -> Schedule:
    from datetime import datetime, timezone
    with SessionLocal() as db:
        schedule = Schedule(
            user_id=user_id, name=name, cron=cron, timezone=tz, enabled=enabled, targets=targets or ["https://example.com"],
            scan_mode=ScanMode.quick, runner_id=runner_id, llm_profile_id=profile_id,
            next_run_at=next_run_at if next_run_at is not None else (datetime.now(timezone.utc) if enabled else None), **extra,
        )
        db.add(schedule)
        db.commit()
        db.refresh(schedule)
        return schedule


def load_schedule(schedule_id: int) -> Schedule:
    with SessionLocal() as db:
        return db.get(Schedule, schedule_id)
