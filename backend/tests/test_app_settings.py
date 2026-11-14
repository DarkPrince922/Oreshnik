"""«Настройки»: operating parameters changed in the panel instead of .env — and the limit of scans that run at once."""
import json

import pytest

from app.core import runtime_settings as rs
from app.core.config import settings
from app.core.db import SessionLocal
from app.models import ScanStatus
from app.models.app_setting import AppSetting
from app.worker import tasks
from tests.conftest import load_scan, make_runner, make_scan, make_user


def put(client, headers, values):
    return client.put("/api/settings", json={"values": values}, headers=headers)


def item(body, key):
    return next(i for g in body["groups"] for i in g["items"] if i["key"] == key)


# ── what is editable ────────────────────────────────────────────────────────

def test_every_parameter_exists_on_settings_and_its_baseline_is_valid():
    for spec in rs.SPECS:
        assert hasattr(settings, spec.key), spec.key
        # the default itself must pass the same validation a typed value does, or "сбросить" would produce an invalid state
        assert rs.parse(spec, rs.BASELINE[spec.key]) == rs.BASELINE[spec.key], spec.key


def test_secrets_and_startup_variables_are_not_editable():
    keys = {s.key for s in rs.SPECS}
    for forbidden in ("jwt_secret", "secret_encryption_key", "postgres_password", "postgres_host", "redis_url", "admin_password",
                      "admin_email", "oreshnik_env", "cors_origins", "agent_token", "agent_url", "strix_bin", "strix_runs_dir",
                      "scan_worker_slots"):
        assert forbidden not in keys


def test_text_settings_cannot_carry_shell_metacharacters():
    # strix_version and strix_sandbox_image end up in commands run on SSH hosts
    spec = rs.BY_KEY["strix_version"]
    for bad in ("1.7.0; rm -rf /", "1.7.0 && id", "$(id)", "a b", "", "x" * 40):
        with pytest.raises(ValueError):
            rs.parse(spec, bad)
    assert rs.parse(spec, " 1.8.0 ") == "1.8.0"
    image = rs.BY_KEY["strix_sandbox_image"]
    for bad in ("img; id", "img`id`", "img\nx", "-evil"):
        with pytest.raises(ValueError):
            rs.parse(image, bad)
    assert rs.parse(image, "ghcr.io/usestrix/strix-sandbox:1.3.0")


@pytest.mark.parametrize("key,bad", [
    ("scan_concurrency", 0), ("scan_concurrency", 65), ("scan_concurrency", "5"), ("scan_concurrency", 2.5), ("scan_concurrency", True),
    ("strix_telemetry", "yes"), ("strix_telemetry", 1), ("strix_reasoning_effort", "hihg"), ("strix_api_type", "chat"),
    ("llm_preflight_timeout", 601), ("ssh_sync_interval_seconds", 0.1), ("login_max_failures", -1),
])
def test_invalid_values_are_refused(key, bad):
    with pytest.raises(ValueError):
        rs.parse(rs.BY_KEY[key], bad)


def test_the_live_limit_cannot_exceed_the_worker_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "scan_worker_slots", 4)
    assert rs.parse(rs.BY_KEY["scan_concurrency"], 4) == 4
    with pytest.raises(ValueError, match="SCAN_WORKER_SLOTS"):
        rs.parse(rs.BY_KEY["scan_concurrency"], 5)


# ── the API ──────────────────────────────────────────────────────────────────

def test_only_an_admin_can_read_or_change(client, operator, viewer):
    for headers in (operator, viewer):
        assert client.get("/api/settings", headers=headers).status_code == 403
        assert put(client, headers, {"scan_concurrency": 5}).status_code == 403
    assert client.get("/api/settings").status_code == 401


def test_the_listing_shows_value_default_and_whether_it_was_changed(client, admin):
    body = client.get("/api/settings", headers=admin).json()
    assert [g["name"] for g in body["groups"]] == list(rs.GROUPS)
    scans = item(body, "scan_concurrency")
    assert scans["value"] == scans["default"] == 2 and scans["changed"] is False and scans["env"] == "SCAN_CONCURRENCY"
    assert item(body, "ssh_check_interval_seconds")["restart"] == "beat"


def test_a_change_applies_at_once_and_a_reset_brings_the_default_back(client, admin):
    r = put(client, admin, {"scan_concurrency": 6, "llm_preflight_timeout": 90})
    assert r.status_code == 200
    assert settings.scan_concurrency == 6 and settings.llm_preflight_timeout == 90
    assert item(r.json(), "scan_concurrency")["changed"] is True and item(r.json(), "scan_concurrency")["default"] == 2
    with SessionLocal() as db:
        assert json.loads(db.get(AppSetting, "scan_concurrency").value) == 6
        assert db.get(AppSetting, "scan_concurrency").updated_by is not None

    r = put(client, admin, {"scan_concurrency": None})
    assert settings.scan_concurrency == 2 and settings.llm_preflight_timeout == 90
    assert item(r.json(), "scan_concurrency")["changed"] is False
    with SessionLocal() as db:
        assert db.get(AppSetting, "scan_concurrency") is None


def test_setting_a_value_equal_to_the_default_keeps_no_row(client, admin):
    """So a later edit of .env still takes effect for a parameter nobody really changed."""
    put(client, admin, {"scan_concurrency": 6})
    r = put(client, admin, {"scan_concurrency": 2})
    assert item(r.json(), "scan_concurrency")["changed"] is False
    with SessionLocal() as db:
        assert db.get(AppSetting, "scan_concurrency") is None


def test_one_invalid_value_changes_nothing(client, admin):
    r = put(client, admin, {"scan_concurrency": 6, "llm_preflight_timeout": 9999, "nope": 1})
    assert r.status_code == 422
    assert "Ожидание пробного запроса" in r.json()["detail"] and "nope" in r.json()["detail"]
    assert settings.scan_concurrency == 2
    with SessionLocal() as db:
        assert db.query(AppSetting).count() == 0


def test_a_text_and_a_choice_and_a_boolean_round_trip(client, admin):
    r = put(client, admin, {"strix_reasoning_effort": " LOW ", "strix_telemetry": True, "strix_version": "1.8.0"})
    assert r.status_code == 200
    assert settings.strix_reasoning_effort == "low" and settings.strix_telemetry is True and settings.strix_version == "1.8.0"


def test_a_parameter_in_the_listing_is_what_the_panel_really_uses(client, admin):
    from app.engine.base import strix_privacy_env, strix_reasoning_env

    put(client, admin, {"strix_reasoning_effort": "minimal", "strix_telemetry": True})
    assert strix_reasoning_env() == {"STRIX_REASONING_EFFORT": "minimal"}
    assert strix_privacy_env()["STRIX_TELEMETRY"] == "1"


# ── reaching the other processes ───────────────────────────────────────────────

def test_another_process_picks_a_change_up_by_refreshing(client, admin):
    """The worker is a different process: it knows nothing of the request, only of the table."""
    with SessionLocal() as db:
        db.add(AppSetting(key="scan_concurrency", value="7"))
        db.add(AppSetting(key="max_schedules_per_user", value="3"))
        db.commit()
    assert settings.scan_concurrency == 2
    assert rs.refresh() is True
    assert settings.scan_concurrency == 7 and settings.max_schedules_per_user == 3
    with SessionLocal() as db:
        db.query(AppSetting).delete()
        db.commit()
    rs.refresh()
    assert settings.scan_concurrency == 2  # a deleted row is a reset, seen by every process


def test_a_stored_value_that_is_not_valid_any_more_or_unknown_is_ignored(client):
    with SessionLocal() as db:
        db.add_all([AppSetting(key="scan_concurrency", value="9999"), AppSetting(key="gone_parameter", value="1"), AppSetting(key="strix_telemetry", value="\"x\"")])
        db.commit()
    rs.refresh()
    assert settings.scan_concurrency == 2 and settings.strix_telemetry is False


def test_a_broken_database_leaves_the_current_values_alone(monkeypatch):
    settings.scan_concurrency = 5

    class Boom:
        def __enter__(self):
            raise RuntimeError("db is away")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("app.core.db.SessionLocal", lambda: Boom())
    assert rs.refresh() is False and settings.scan_concurrency == 5


# ── the limit of scans that run at once ──────────────────────────────────────────

def queued_and_running(monkeypatch, running: int, limit: int):
    monkeypatch.setattr(settings, "scan_concurrency", limit)
    user = make_user("scan-owner@example.com")
    runner = make_runner()
    for _ in range(running):
        make_scan(user.id, runner.id, status=ScanStatus.running)
    return make_scan(user.id, runner.id, status=ScanStatus.queued)


def test_a_scan_waits_while_the_limit_is_reached(monkeypatch):
    scan = queued_and_running(monkeypatch, running=2, limit=2)
    assert tasks._claim(scan.id) == "busy"
    assert load_scan(scan.id).status == ScanStatus.queued  # untouched: it keeps its place in the queue
    assert tasks.scan_run(scan.id) == "waiting"


def test_a_scan_starts_when_there_is_a_free_place(monkeypatch):
    scan = queued_and_running(monkeypatch, running=1, limit=2)
    snap = tasks._claim(scan.id)
    assert snap not in (None, "busy") and load_scan(scan.id).status == ScanStatus.running


def test_raising_the_limit_lets_the_waiting_scan_start_without_a_restart(client, admin, monkeypatch):
    scan = queued_and_running(monkeypatch, running=2, limit=2)
    assert tasks._claim(scan.id) == "busy"
    put(client, admin, {"scan_concurrency": 3})
    assert tasks._claim(scan.id) not in (None, "busy")


def test_lowering_the_limit_does_not_kill_what_runs(client, admin, monkeypatch):
    scan = queued_and_running(monkeypatch, running=3, limit=5)
    put(client, admin, {"scan_concurrency": 1})
    assert tasks._claim(scan.id) == "busy"
    with SessionLocal() as db:
        assert db.query(tasks.Scan).filter(tasks.Scan.status == ScanStatus.running).count() == 3


def test_a_cancelling_scan_still_holds_its_place(monkeypatch):
    user = make_user("o@example.com")
    runner = make_runner()
    monkeypatch.setattr(settings, "scan_concurrency", 1)
    make_scan(user.id, runner.id, status=ScanStatus.cancelling)
    waiting = make_scan(user.id, runner.id, status=ScanStatus.queued)
    assert tasks._claim(waiting.id) == "busy"


def test_finished_and_queued_scans_do_not_hold_places(monkeypatch):
    user = make_user("o@example.com")
    runner = make_runner()
    monkeypatch.setattr(settings, "scan_concurrency", 1)
    for status in (ScanStatus.succeeded, ScanStatus.failed, ScanStatus.cancelled, ScanStatus.queued):
        make_scan(user.id, runner.id, status=status)
    scan = make_scan(user.id, runner.id, status=ScanStatus.queued)
    assert tasks._claim(scan.id) not in (None, "busy")


def test_a_cancelled_scan_is_skipped_not_retried(monkeypatch):
    scan = queued_and_running(monkeypatch, running=2, limit=2)
    with SessionLocal() as db:
        db.get(tasks.Scan, scan.id).status = ScanStatus.cancelled
        db.commit()
    assert tasks.scan_run(scan.id) == "skipped"


def test_a_waiting_scan_is_scheduled_again_in_a_real_worker(monkeypatch):
    from app.worker.celery_app import celery

    scan = queued_and_running(monkeypatch, running=2, limit=2)
    monkeypatch.setattr(celery.conf, "task_always_eager", False)
    calls = []
    monkeypatch.setattr(tasks.scan_run, "apply_async", lambda args, countdown: calls.append((args, countdown)))
    assert tasks.scan_run(scan.id) == "waiting"
    assert calls == [((scan.id,), tasks._WAIT_SECONDS)]
