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
from app.models import Role, User  # noqa: E402


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
def _clean_state(monkeypatch):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users, llm_profiles, runners RESTART IDENTITY CASCADE"))

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
