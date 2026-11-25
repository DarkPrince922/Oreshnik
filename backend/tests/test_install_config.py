"""What a fresh server tripped over: CORS_ORIGINS mangled by Docker Compose, a placeholder encryption key, a runner check that
answered 500 when the worker was not running."""
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.api import runners as runners_api
from app.core.config import Settings
from app.main import check_config
from app.models import RunnerKind
from tests.conftest import make_runner

WANT = ["http://203.0.113.5:5173", "http://localhost:5173"]


@pytest.mark.parametrize("raw", [
    '["http://203.0.113.5:5173","http://localhost:5173"]',     # JSON as written
    "[http://203.0.113.5:5173,http://localhost:5173]",         # the same after Docker Compose stripped the quotes
    "http://203.0.113.5:5173,http://localhost:5173",           # a plain list
    " http://203.0.113.5:5173 , http://localhost:5173 ",
    '\'["http://203.0.113.5:5173","http://localhost:5173"]\'',  # quotes around the whole value
])
def test_cors_origins_is_read_however_it_reaches_us(raw, monkeypatch):
    assert Settings(_env_file=None, cors_origins=raw).cors_origins == WANT
    monkeypatch.setenv("CORS_ORIGINS", raw)
    assert Settings(_env_file=None).cors_origins == WANT


def test_cors_origins_default_and_a_clear_error(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert Settings(_env_file=None).cors_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]
    with pytest.raises(ValidationError, match="CORS_ORIGINS=http://203.0.113.5:5173"):  # the message carries an example
        Settings(_env_file=None, cors_origins="203.0.113.5")


def test_a_broken_encryption_key_stops_the_start_with_the_fix_in_the_message(monkeypatch):
    from app import main

    for bad in ("CHANGE_ME_generate_a_fernet_key", "too-short", ""):
        monkeypatch.setattr(main.settings, "secret_encryption_key", bad)
        with pytest.raises(RuntimeError, match="install.sh"):
            check_config()
    monkeypatch.setattr(main.settings, "secret_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(main.settings, "jwt_secret", "x" * 40)
    check_config()  # a real key: starts


def test_a_runner_check_with_no_worker_says_so_instead_of_500(client, admin, monkeypatch):
    runner = make_runner(RunnerKind.local, "local")

    class Never:
        def get(self, timeout=None):
            raise TimeoutError("The operation timed out.")

    monkeypatch.setattr(runners_api.runner_check, "delay", lambda *_a, **_k: Never())
    r = client.post(f"/api/runners/{runner.id}/check", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "offline" and "docker compose ps worker" in r.json()["status_detail"]
