"""Saved model lists on LLM profiles, and choosing any of them (of any provider) for a scan, a continuation or a schedule."""
import pytest

from app.api import scans as scans_api
from app.core.crypto import encrypt
from app.core.db import SessionLocal
from app.models import LLMProfile, Provider, Role, RunnerKind, ScanStatus
from app.services import llm as llm_service
from app.services.llm import build_strix_env, clean_model_list, profile_models
from app.worker import tasks
from tests.test_continue import _fake_run_dir
from tests.conftest import auth_headers, load_scan, make_profile, make_runner, make_scan, make_schedule, make_user

KEY = "sk-test-key-1234567890"


@pytest.fixture
def queued_only(monkeypatch):
    monkeypatch.setattr(scans_api.scan_run, "delay", lambda *_a, **_k: None)


def profile_body(**over):
    body = {"name": "gw", "provider": "custom", "model": "m-default", "base_url": "http://gw.internal/v1", "api_key": KEY}
    body.update(over)
    return body


def new_profile(client, headers, **over):
    r = client.post("/api/llm-profiles", json=profile_body(**over), headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def models_of(client, headers, pid):
    return next(p for p in client.get("/api/llm-profiles", headers=headers).json() if p["id"] == pid)["models"]


# ── the saved list ────────────────────────────────────────────────────────

def test_a_profile_keeps_the_models_it_was_saved_with_cleaned(client, operator):
    p = new_profile(client, operator, models=[" a ", "b", "a", "", "  ", "c" * 201, "d"])
    assert p["models"] == ["a", "b", "d"]  # trimmed, no blanks, no repeats, no absurd ids
    assert models_of(client, operator, p["id"]) == ["a", "b", "d"]


def test_a_profile_without_models_has_an_empty_list(client, operator):
    assert new_profile(client, operator)["models"] == []


def test_updating_replaces_the_list_only_when_it_is_sent(client, operator):
    pid = new_profile(client, operator, models=["a", "b"])["id"]
    client.patch(f"/api/llm-profiles/{pid}", json={"name": "renamed"}, headers=operator)
    assert models_of(client, operator, pid) == ["a", "b"]
    client.patch(f"/api/llm-profiles/{pid}", json={"models": ["x"]}, headers=operator)
    assert models_of(client, operator, pid) == ["x"]
    client.patch(f"/api/llm-profiles/{pid}", json={"models": []}, headers=operator)
    assert models_of(client, operator, pid) == []


def test_the_default_model_is_always_selectable_first(client, operator):
    with SessionLocal() as db:
        profile = LLMProfile(user_id=1, name="p", provider=Provider.openai, model="gpt-5", models=["gpt-4o", "gpt-5", "o3"],
                             api_key_enc=encrypt(KEY), api_key_hint="…7890")
        assert profile_models(profile) == ["gpt-5", "gpt-4o", "o3"]
        profile.models = []
        assert profile_models(profile) == ["gpt-5"]


def test_clean_model_list_is_bounded():
    assert len(clean_model_list([f"m{i}" for i in range(5000)])) == llm_service.MAX_SAVED_MODELS


# ── «Загрузить модели» keeps what it loaded ───────────────────────────────────────

def fake_fetch(monkeypatch, models=("deepseek-flash", "deepseek-v4-pro")):
    calls = []

    def fetch(provider, base_url, api_key):
        calls.append((provider, base_url, api_key))
        return list(models)

    monkeypatch.setattr("app.api.llm_profiles.fetch_provider_models", fetch)
    return calls


def test_loading_models_for_a_saved_profile_stores_them_at_once(client, operator, monkeypatch):
    calls = fake_fetch(monkeypatch)
    pid = new_profile(client, operator)["id"]
    r = client.post("/api/llm-profiles/models", json={"provider": "custom", "profile_id": pid}, headers=operator)
    assert r.status_code == 200 and r.json() == {"models": ["deepseek-flash", "deepseek-v4-pro"], "saved": True}
    assert calls[0][2] == KEY  # the stored key was used
    assert models_of(client, operator, pid) == ["deepseek-flash", "deepseek-v4-pro"]  # kept without pressing «Сохранить»


def test_models_from_a_different_endpoint_or_key_are_handed_back_but_not_stored(client, operator, monkeypatch):
    fake_fetch(monkeypatch)
    pid = new_profile(client, operator, models=["keep"])["id"]
    other_url = client.post("/api/llm-profiles/models", headers=operator,
                            json={"provider": "custom", "profile_id": pid, "base_url": "http://other.example/v1"})
    typed_key = client.post("/api/llm-profiles/models", headers=operator, json={"provider": "custom", "profile_id": pid, "api_key": "another-key-123456"})
    unsaved = client.post("/api/llm-profiles/models", headers=operator,
                          json={"provider": "custom", "base_url": "http://x/v1", "api_key": "k" * 20})
    assert [r.json()["saved"] for r in (other_url, typed_key, unsaved)] == [False, False, False]
    assert models_of(client, operator, pid) == ["keep"]


def test_refresh_replaces_the_list_with_what_the_provider_serves_now(client, operator, monkeypatch):
    fake_fetch(monkeypatch, models=("new-1", "new-2"))
    pid = new_profile(client, operator, models=["old"])["id"]
    r = client.post(f"/api/llm-profiles/{pid}/refresh-models", headers=operator)
    assert r.status_code == 200 and r.json()["models"] == ["new-1", "new-2"] and KEY not in r.text


def test_refresh_reports_a_provider_failure_and_keeps_the_old_list(client, operator, monkeypatch):
    def boom(*_a):
        raise llm_service.ModelListError("провайдер не отвечает")

    monkeypatch.setattr("app.api.llm_profiles.fetch_provider_models", boom)
    pid = new_profile(client, operator, models=["old"])["id"]
    r = client.post(f"/api/llm-profiles/{pid}/refresh-models", headers=operator)
    assert r.status_code == 502 and "не отвечает" in r.json()["detail"]
    assert models_of(client, operator, pid) == ["old"]


def test_refresh_is_private_to_the_owner(client, operator, admin, monkeypatch):
    fake_fetch(monkeypatch)
    pid = new_profile(client, operator)["id"]
    assert client.post(f"/api/llm-profiles/{pid}/refresh-models", headers=admin).status_code == 404


# ── choosing a model for a scan ──────────────────────────────────────────────

def user_and_profile(client, headers, **over):
    p = new_profile(client, headers, **over)
    return p["id"]


def test_a_scan_runs_with_the_chosen_model_of_the_chosen_profile(client, operator, queued_only):
    make_runner()
    pid = user_and_profile(client, operator, models=["deepseek-flash", "deepseek-v4-pro"])
    r = client.post("/api/scans", headers=operator, json={"targets": ["https://example.com"], "llm_profile_id": pid, "llm_model": "deepseek-v4-pro"})
    assert r.status_code == 201, r.text
    assert r.json()["llm_model"] == "deepseek-v4-pro"
    assert load_scan(r.json()["id"]).llm_model == "deepseek-v4-pro"


def test_without_a_choice_the_profiles_default_model_is_used_and_shown(client, operator, queued_only):
    make_runner()
    pid = user_and_profile(client, operator, models=["other"])
    r = client.post("/api/scans", headers=operator, json={"targets": ["https://example.com"], "llm_profile_id": pid})
    assert r.json()["llm_model"] == "m-default" and load_scan(r.json()["id"]).llm_model is None


def test_the_profiles_default_model_can_be_chosen_even_if_not_in_the_saved_list(client, operator, queued_only):
    make_runner()
    pid = user_and_profile(client, operator, models=["other"])
    r = client.post("/api/scans", headers=operator, json={"targets": ["https://example.com"], "llm_profile_id": pid, "llm_model": "m-default"})
    assert r.status_code == 201


def test_a_model_the_profile_does_not_have_is_refused_with_a_hint(client, operator, queued_only):
    make_runner()
    pid = user_and_profile(client, operator, models=["a"])
    r = client.post("/api/scans", headers=operator, json={"targets": ["https://example.com"], "llm_profile_id": pid, "llm_model": "not-there"})
    assert r.status_code == 422 and "LLM-профили" in r.json()["detail"]


def test_a_model_needs_a_profile(client, operator, queued_only):
    make_runner()
    r = client.post("/api/scans", headers=operator, json={"targets": ["https://example.com"], "llm_model": "a"})
    assert r.status_code == 422 and "вместе с LLM-профилем" in r.json()["detail"]


def test_another_users_profile_cannot_be_used_to_pick_a_model(client, operator, admin, queued_only):
    make_runner()
    pid = user_and_profile(client, admin, models=["a"])
    r = client.post("/api/scans", headers=operator, json={"targets": ["https://example.com"], "llm_profile_id": pid, "llm_model": "a"})
    assert r.status_code == 404


def test_models_of_different_providers_are_chosen_the_same_way(client, operator, queued_only):
    make_runner()
    deep = user_and_profile(client, operator, name="DeepSeek", models=["deepseek-flash"])
    oa = user_and_profile(client, operator, name="OpenAI", provider="openai", model="gpt-5", base_url=None, models=["gpt-4o", "o3"])
    for pid, model in ((deep, "deepseek-flash"), (oa, "o3"), (oa, "gpt-5")):
        r = client.post("/api/scans", headers=operator, json={"targets": ["https://example.com"], "llm_profile_id": pid, "llm_model": model})
        assert r.status_code == 201 and r.json()["llm_model"] == model and r.json()["llm_profile_id"] == pid


# ── what the worker gives Strix ───────────────────────────────────────────────

def test_strix_gets_the_chosen_model_with_the_providers_prefix():
    custom = LLMProfile(user_id=1, name="p", provider=Provider.custom, model="m-default", models=["x/y"], base_url="http://gw/v1", api_key_enc=encrypt(KEY), api_key_hint="…")
    assert build_strix_env(custom)["STRIX_LLM"] == "openai/m-default"
    assert build_strix_env(custom, "x/y")["STRIX_LLM"] == "openai/x/y"
    openai = LLMProfile(user_id=1, name="p", provider=Provider.openai, model="gpt-5", models=["o3"], api_key_enc=encrypt(KEY), api_key_hint="…")
    assert build_strix_env(openai, "o3")["STRIX_LLM"] == "openai/o3"
    claude = LLMProfile(user_id=1, name="p", provider=Provider.claude, model="claude-sonnet-4-6", models=[], api_key_enc=encrypt(KEY), api_key_hint="…")
    assert build_strix_env(claude, None)["STRIX_LLM"] == "anthropic/claude-sonnet-4-6"


def test_the_worker_starts_the_scan_with_its_own_model():
    user = make_user("w@example.com")
    profile = make_profile(user.id)
    with SessionLocal() as db:
        db.get(LLMProfile, profile.id).models = ["claude-opus-5-5"]
        db.commit()
    runner = make_runner()
    scan = make_scan(user.id, runner.id, profile_id=profile.id, llm_model="claude-opus-5-5")
    snap = tasks._claim(scan.id)
    assert snap.env["STRIX_LLM"] == "anthropic/claude-opus-5-5"
    default_scan = make_scan(user.id, runner.id, profile_id=profile.id)
    assert tasks._claim(default_scan.id).env["STRIX_LLM"] == "anthropic/claude-sonnet-4-6"


# ── continuing ────────────────────────────────────────────────────────────

_runs = iter(range(1, 1000))


def finished(user, runner, profile_id, model):
    name = f"run-{next(_runs)}"  # Strix names containers after the run: one run can be active only once, so each scan has its own
    scan = make_scan(user.id, runner.id, profile_id=profile_id, llm_model=model, status=ScanStatus.failed, run_name=name)
    _fake_run_dir(scan.id, name)  # the run files Strix would have left, so the scan can be continued
    return scan


def test_a_continuation_keeps_the_model_unless_told_otherwise(client, operator, queued_only):
    user = make_user("c@example.com")
    h = auth_headers(client, "c@example.com")
    runner = make_runner(RunnerKind.local)  # a mock run carries no LLM profile, so no model either
    pid = user_and_profile(client, h, models=["a", "b"])
    # a run can be active only once, so each case continues a scan of its own
    r = client.post(f"/api/scans/{finished(user, runner, pid, 'a').id}/continue", headers=h, json={"as_new_scan": True})
    assert r.status_code in (200, 201, 202), r.text
    assert r.json()["llm_model"] == "a"
    r = client.post(f"/api/scans/{finished(user, runner, pid, 'a').id}/continue", headers=h, json={"as_new_scan": True, "llm_model": "b"})
    assert r.status_code in (200, 201, 202) and r.json()["llm_model"] == "b"
    r = client.post(f"/api/scans/{finished(user, runner, pid, 'a').id}/continue", headers=h, json={"as_new_scan": True, "llm_model": "nope"})
    assert r.status_code == 422


def test_a_model_that_left_the_profiles_list_is_dropped_on_continuing(client, queued_only):
    user = make_user("c3@example.com")
    h = auth_headers(client, "c3@example.com")
    runner = make_runner(RunnerKind.local)
    pid = user_and_profile(client, h, models=["gone-soon"])
    scan = finished(user, runner, pid, "gone-soon")
    client.patch(f"/api/llm-profiles/{pid}", json={"models": ["other"]}, headers=h)  # the provider stopped serving it
    r = client.post(f"/api/scans/{scan.id}/continue", headers=h, json={"as_new_scan": True})
    assert r.status_code in (200, 201, 202) and r.json()["llm_model"] == "m-default"


def test_moving_a_continuation_to_another_profile_drops_the_old_profiles_model(client, queued_only):
    user = make_user("c2@example.com")
    h = auth_headers(client, "c2@example.com")
    runner = make_runner(RunnerKind.local)
    first = user_and_profile(client, h, name="first", models=["only-here"])
    second = user_and_profile(client, h, name="second", model="second-default", models=[])
    scan = finished(user, runner, first, "only-here")
    r = client.post(f"/api/scans/{scan.id}/continue", headers=h, json={"as_new_scan": True, "llm_profile_id": second})
    assert r.status_code in (200, 201, 202), r.text
    assert r.json()["llm_profile_id"] == second and r.json()["llm_model"] == "second-default"


# ── schedules ─────────────────────────────────────────────────────────────

def test_a_schedule_remembers_its_model_and_its_scans_use_it(client, operator, queued_only):
    make_runner()
    pid = user_and_profile(client, operator, models=["a", "b"])
    r = client.post("/api/schedules", headers=operator, json={"name": "n", "cron": "0 3 * * *", "targets": ["https://example.com"],
                                                               "llm_profile_id": pid, "llm_model": "b", "max_budget": 5})
    assert r.status_code == 201, r.text
    assert r.json()["llm_model"] == "b"
    sid = r.json()["id"]
    assert client.post(f"/api/schedules/{sid}/run", headers=operator).status_code in (200, 201, 202)
    scans = client.get("/api/scans", headers=operator).json()["items"]
    assert scans and scans[0]["llm_model"] == "b"


def test_a_schedule_refuses_an_unknown_model_and_resets_it_with_the_profile(client, operator, queued_only):
    make_runner()
    first = user_and_profile(client, operator, name="first", models=["a"])
    second = user_and_profile(client, operator, name="second", model="s-default", models=[])
    base = {"name": "n", "cron": "0 3 * * *", "targets": ["https://example.com"], "max_budget": 5}
    assert client.post("/api/schedules", headers=operator, json={**base, "llm_profile_id": first, "llm_model": "zzz"}).status_code == 422
    sid = client.post("/api/schedules", headers=operator, json={**base, "llm_profile_id": first, "llm_model": "a"}).json()["id"]
    r = client.patch(f"/api/schedules/{sid}", headers=operator, json={"llm_profile_id": second})
    assert r.status_code == 200, r.text
    assert r.json()["llm_model"] == "s-default"


# ── «Агент» and «Операции» use the chosen model too ──────────────────────────────

def test_an_agent_session_runs_with_the_chosen_model(client, operator, monkeypatch):
    from app.core.config import settings
    from app.services import agent as agent_service

    monkeypatch.setattr(settings, "agent_token", "t" * 20)
    pid = user_and_profile(client, operator, models=["resp-small", "resp-big"])
    r = client.post("/api/agent/sessions", headers=operator, json={"llm_profile_id": pid, "llm_model": "resp-big"})
    assert r.status_code == 201, r.text
    assert r.json()["llm_model"] == "resp-big"
    sid = r.json()["id"]

    seen = {}

    def fake_stream(**kw):
        seen.update(kw)
        yield {"type": "agentd.exit", "code": 0}

    monkeypatch.setattr(agent_service, "stream_turn", fake_stream)
    assert client.post(f"/api/agent/sessions/{sid}/messages", headers=operator, json={"text": "hi"}).status_code == 202
    assert seen["model"] == "resp-big" and seen["base_url"] == "http://gw.internal/v1"


def test_an_agent_session_without_a_choice_uses_the_profiles_default(client, operator, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_token", "t" * 20)
    pid = user_and_profile(client, operator, models=["other"])
    r = client.post("/api/agent/sessions", headers=operator, json={"llm_profile_id": pid})
    assert r.status_code == 201 and r.json()["llm_model"] == "m-default"


def test_an_agent_session_refuses_a_model_the_profile_does_not_have(client, operator, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_token", "t" * 20)
    pid = user_and_profile(client, operator, models=["a"])
    r = client.post("/api/agent/sessions", headers=operator, json={"llm_profile_id": pid, "llm_model": "nope"})
    assert r.status_code == 422 and "LLM-профили" in r.json()["detail"]


def test_the_agents_endpoint_follows_the_chosen_model():
    custom = LLMProfile(user_id=1, name="p", provider=Provider.custom, model="d", models=["x"], base_url="http://gw/v1", api_key_enc=encrypt(KEY), api_key_hint="…")
    assert llm_agent_endpoint(custom) == ("http://gw/v1", "d")
    assert llm_agent_endpoint(custom, "x") == ("http://gw/v1", "x")
    grok = LLMProfile(user_id=1, name="p", provider=Provider.grok, model="grok-4", models=["grok-3"], api_key_enc=encrypt(KEY), api_key_hint="…")
    assert llm_agent_endpoint(grok, "grok-3") == ("https://api.x.ai/v1", "grok-3")


def llm_agent_endpoint(profile, model=None):
    from app.services.agent import endpoint_for

    return endpoint_for(profile, model)


def ops_body(pid, runner_ids, **over):
    body = {"goal": "обновить пакеты", "llm_profile_id": pid, "runner_ids": runner_ids}
    body.update(over)
    return body


def test_an_ops_run_is_drafted_with_the_chosen_model(client, admin, fake_host, monkeypatch):
    from tests.conftest import make_ssh_runner

    seen = {}

    def fake_chat(provider, base_url, api_key, model, *, system, user):
        seen["model"] = model
        return "#!/bin/sh\nset -eu\necho hi\n"

    monkeypatch.setattr("app.services.ops.chat_once", fake_chat)
    monkeypatch.setattr("app.worker.tasks.ops.run_on_host", lambda *a, **k: ("succeeded", 0, "hi\n"))
    runner = make_ssh_runner(fake_host, trusted=True, status=None)
    from app.models import RunnerStatus
    from app.core.db import SessionLocal
    from app.models import Runner

    with SessionLocal() as db:
        db.get(Runner, runner.id).status = RunnerStatus.online
        db.commit()
    pid = user_and_profile(client, admin, models=["ops-a", "ops-b"])
    r = client.post("/api/ops", headers=admin, json=ops_body(pid, [runner.id], llm_model="ops-b"))
    assert r.status_code == 201, r.text
    assert r.json()["llm_model"] == "ops-b"
    assert seen["model"] == "ops-b"


def test_an_ops_run_refuses_a_model_the_profile_does_not_have(client, admin, fake_host):
    from tests.conftest import make_ssh_runner

    runner = make_ssh_runner(fake_host, trusted=True)
    pid = user_and_profile(client, admin, models=["ops-a"])
    r = client.post("/api/ops", headers=admin, json=ops_body(pid, [runner.id], llm_model="nope"))
    assert r.status_code == 422 and "LLM-профили" in r.json()["detail"]
