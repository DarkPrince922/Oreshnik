"""«Операции»: a goal turned into a script by an LLM and run on existing SSH runners, with no approval step —
so what must hold instead: the script is always visible, the safety guard actually refuses what it says it
refuses, each host's own outcome never bleeds into another's, and only an admin can do any of this."""
import httpx
import pytest

from app.core.crypto import encrypt
from app.core.db import SessionLocal
from app.models import LLMProfile, Provider
from app.models.ops import OpsHostStatus, OpsRun, OpsRunHost, OpsRunStatus
from app.models.runner import Runner, RunnerKind
from app.services import ops
from app.services.llm import LlmCallError
from tests.conftest import make_profile, make_runner, make_ssh_runner, make_user

GOAL = "обнови пакеты и перезапусти nginx"


def create(client, headers, **body):
    body.setdefault("goal", GOAL)
    body.setdefault("name", "")
    return client.post("/api/ops", headers=headers, json=body)


def stub_script(monkeypatch, script: str = "#!/bin/sh\nset -eu\necho hi\n") -> list[tuple]:
    """Replace the LLM call with a canned script; returns the list of (profile, goal, runner_names) it was called with."""
    calls = []

    def fake(profile, goal, runner_names):
        calls.append((profile, goal, runner_names))
        return script

    monkeypatch.setattr(ops, "draft_script", fake)
    return calls


def me(client, headers) -> int:
    return client.get("/api/auth/me", headers=headers).json()["id"]


def load_run(run_id: int) -> OpsRun:
    with SessionLocal() as db:
        run = db.get(OpsRun, run_id)
        db.refresh(run, attribute_names=["hosts"])
        for h in run.hosts:
            db.refresh(h)
        return run


# ── permissions ───────────────────────────────────────────────────────────

def test_only_admin_can_create_list_or_read(client, operator, viewer, admin):
    profile = make_profile(me(client, admin))
    runner = make_runner(RunnerKind.ssh)
    for headers in (operator, viewer):
        assert create(client, headers, llm_profile_id=profile.id, runner_ids=[runner.id]).status_code == 403
        assert client.get("/api/ops", headers=headers).status_code == 403


def test_unauthenticated_is_refused(client):
    assert client.get("/api/ops").status_code == 401


# ── creating a run: validation before anything is touched ─────────────────

def test_needs_an_own_llm_profile(client, admin, operator):
    runner = make_runner(RunnerKind.ssh)
    other_profile = make_profile(me(client, operator))
    r = create(client, admin, llm_profile_id=other_profile.id, runner_ids=[runner.id])
    assert r.status_code == 404


def test_runners_must_exist_and_be_ssh(client, admin):
    profile = make_profile(me(client, admin))
    mock_runner = make_runner(RunnerKind.mock)
    assert create(client, admin, llm_profile_id=profile.id, runner_ids=[999999]).status_code == 404
    assert create(client, admin, llm_profile_id=profile.id, runner_ids=[mock_runner.id]).status_code == 422


def test_an_unconfigured_ssh_runner_is_refused_up_front(client, admin, monkeypatch):
    profile = make_profile(me(client, admin))
    runner = make_runner(RunnerKind.ssh)  # no host/key: check_runner_usable refuses it
    calls = stub_script(monkeypatch)
    r = create(client, admin, llm_profile_id=profile.id, runner_ids=[runner.id])
    assert r.status_code == 422
    assert calls == []  # the LLM is never even asked


def test_an_empty_goal_or_no_runners_is_rejected(client, admin):
    profile = make_profile(me(client, admin))
    runner = make_runner(RunnerKind.ssh)
    assert create(client, admin, llm_profile_id=profile.id, runner_ids=[runner.id], goal="   ").status_code == 422
    assert create(client, admin, llm_profile_id=profile.id, runner_ids=[]).status_code == 422


def test_duplicate_runner_ids_are_deduplicated(client, admin, fake_host, monkeypatch):
    profile = make_profile(me(client, admin))
    runner = make_ssh_runner(fake_host, trusted=True)
    stub_script(monkeypatch)
    r = create(client, admin, llm_profile_id=profile.id, runner_ids=[runner.id, runner.id, runner.id])
    assert r.status_code == 201 and len(r.json()["hosts"]) == 1


# ── the real end-to-end path: a real sshd, a stubbed LLM ──────────────────

def test_a_run_drafts_once_and_executes_on_a_real_host(client, admin, fake_host, monkeypatch):
    profile = make_profile(me(client, admin))
    runner = make_ssh_runner(fake_host, trusted=True)
    calls = stub_script(monkeypatch, "#!/bin/sh\nset -eu\necho from-the-host\npwd\n")

    r = create(client, admin, llm_profile_id=profile.id, runner_ids=[runner.id])
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "done" and body["script"] == "#!/bin/sh\nset -eu\necho from-the-host\npwd\n"
    assert len(calls) == 1 and calls[0][1] == GOAL and calls[0][2] == [runner.name]
    [host] = body["hosts"]
    assert host["status"] == "succeeded" and host["exit_code"] == 0
    assert "from-the-host" in host["output"]


def test_a_failing_script_is_reported_without_touching_the_run_status(client, admin, fake_host, monkeypatch):
    profile = make_profile(me(client, admin))
    runner = make_ssh_runner(fake_host, trusted=True)
    stub_script(monkeypatch, "#!/bin/sh\necho oops >&2\nexit 7\n")
    r = create(client, admin, llm_profile_id=profile.id, runner_ids=[runner.id])
    body = r.json()
    assert body["status"] == "done"  # the task ran to completion; the *host* failed
    [host] = body["hosts"]
    assert host["status"] == "failed" and host["exit_code"] == 7 and "oops" in host["output"]


def test_an_unreachable_host_does_not_stop_the_others(client, admin, fake_host, monkeypatch):
    profile = make_profile(me(client, admin))
    good = make_ssh_runner(fake_host, trusted=True, name="good")
    with SessionLocal() as db:
        bad = Runner(
            name="bad", kind=RunnerKind.ssh, ssh_host="127.0.0.1", ssh_port=1, ssh_user=fake_host.user,
            ssh_key_enc=encrypt(fake_host.client_key_pem), ssh_host_key=fake_host.host_key_line,
        )
        db.add(bad)
        db.commit()
        db.refresh(bad)
    stub_script(monkeypatch, "#!/bin/sh\necho ok\n")
    r = create(client, admin, llm_profile_id=profile.id, runner_ids=[good.id, bad.id])
    by_name = {h["runner_name"]: h for h in r.json()["hosts"]}
    assert by_name["good"]["status"] == "succeeded"
    assert by_name["bad"]["status"] == "unreachable" and by_name["bad"]["exit_code"] is None


def test_deleting_a_runner_does_not_lose_the_audit_trail(client, admin, fake_host, monkeypatch):
    profile = make_profile(me(client, admin))
    runner = make_ssh_runner(fake_host, trusted=True, name="to-be-deleted")
    stub_script(monkeypatch, "#!/bin/sh\necho ok\n")
    run_id = create(client, admin, llm_profile_id=profile.id, runner_ids=[runner.id]).json()["id"]
    client.delete(f"/api/runners/{runner.id}", headers=admin)
    body = client.get(f"/api/ops/{run_id}", headers=admin).json()
    assert body["hosts"][0]["runner_name"] == "to-be-deleted" and body["hosts"][0]["runner_id"] is None


# ── the safety guard: refuses before any host is touched ──────────────────

def test_a_catastrophic_script_is_blocked_on_every_host_without_running_it(client, admin, fake_host, monkeypatch):
    profile = make_profile(me(client, admin))
    runner = make_ssh_runner(fake_host, trusted=True)
    sentinel = fake_host.home / "untouched"
    stub_script(monkeypatch, f"#!/bin/sh\nrm -rf /\ntouch {sentinel}\n")
    r = create(client, admin, llm_profile_id=profile.id, runner_ids=[runner.id])
    body = r.json()
    assert body["status"] == "failed"
    assert body["blocked_reason"] and "корня" in body["blocked_reason"]
    [host] = body["hosts"]
    assert host["status"] == "blocked" and host["exit_code"] is None
    assert not sentinel.exists()  # proof the script never ran at all


def test_draft_failure_fails_every_host_with_a_reason(client, admin, fake_host, monkeypatch):
    profile = make_profile(me(client, admin))
    runner = make_ssh_runner(fake_host, trusted=True)

    def boom(profile, goal, runner_names):
        raise ops.OpsError("модель недоступна")

    monkeypatch.setattr(ops, "draft_script", boom)
    r = create(client, admin, llm_profile_id=profile.id, runner_ids=[runner.id])
    body = r.json()
    assert body["status"] == "failed" and "недоступна" in body["script_error"]
    assert body["hosts"][0]["status"] == "failed"


# ── cancel ──────────────────────────────────────────────────────────────

def test_cancel_is_refused_once_the_run_is_over(client, admin, fake_host, monkeypatch):
    profile = make_profile(me(client, admin))
    runner = make_ssh_runner(fake_host, trusted=True)
    stub_script(monkeypatch, "#!/bin/sh\necho ok\n")
    run_id = create(client, admin, llm_profile_id=profile.id, runner_ids=[runner.id]).json()["id"]
    assert client.post(f"/api/ops/{run_id}/cancel", headers=admin).status_code == 409


def test_cancel_flag_actually_skips_remaining_hosts(monkeypatch, fake_host):
    """Worker-level: cancel_requested is honoured between hosts. Not reachable through the API test above, since
    eager Celery (tests run synchronously) means the task is already done before a second request could cancel it."""
    from app.worker.tasks import ops_run as ops_run_task

    user = make_user("cancel-worker@example.com")
    profile = make_profile(user.id)
    a = make_ssh_runner(fake_host, trusted=True, name="a")
    b = make_ssh_runner(fake_host, trusted=True, name="b")

    with SessionLocal() as db:
        run = OpsRun(user_id=user.id, name="x", goal=GOAL, llm_profile_id=profile.id, status=OpsRunStatus.drafting)
        db.add(run)
        db.flush()
        db.add(OpsRunHost(ops_run_id=run.id, runner_id=a.id, runner_name=a.name))
        db.add(OpsRunHost(ops_run_id=run.id, runner_id=b.id, runner_name=b.name))
        db.commit()
        run_id = run.id

    monkeypatch.setattr(ops, "draft_script", lambda *a, **k: "#!/bin/sh\necho ok\n")
    real_run_on_host = ops.run_on_host

    def cancel_after_first_host(runner, script, *, timeout=600.0):
        result = real_run_on_host(runner, script, timeout=timeout)
        with SessionLocal() as db:
            db.query(OpsRun).filter(OpsRun.id == run_id).update({"cancel_requested": True})
            db.commit()
        return result

    monkeypatch.setattr(ops, "run_on_host", cancel_after_first_host)
    ops_run_task(run_id)

    row = load_run(run_id)
    statuses = {h.runner_name: h.status for h in row.hosts}
    assert statuses["a"] == OpsHostStatus.succeeded
    assert statuses["b"] == OpsHostStatus.cancelled


# ── the draft/guard functions, unit level ──────────────────────────────────

def test_strip_code_fence():
    assert ops.strip_code_fence("```sh\necho hi\n```") == "echo hi"
    assert ops.strip_code_fence("```\necho hi\n```") == "echo hi"
    assert ops.strip_code_fence("echo hi") == "echo hi"
    assert ops.strip_code_fence("  echo hi  \n") == "echo hi"


@pytest.mark.parametrize("script", [
    "#!/bin/sh\nrm -rf /\n",
    "#!/bin/sh\nrm -rf /*\n",
    "#!/bin/sh\nrm --recursive --force /\n",
    ":(){ :|:& };:",
    "mkfs.ext4 /dev/sdb1",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    "wipefs -a /dev/sdb",
    "rm -rf --no-preserve-root /opt/app\n",  # the flag alone is the tell, whatever the path
])
def test_looks_catastrophic_catches_the_obvious_ones(script):
    assert ops.looks_catastrophic(script) is not None


@pytest.mark.parametrize("script", [
    "#!/bin/sh\nset -eu\napt-get update -y\nsystemctl restart nginx\n",
    "#!/bin/sh\nrm -rf /var/log/myapp/*.log\n",
    "#!/bin/sh\nrm -rf /opt/old-release\n",
    "#!/bin/sh\ndd if=/dev/zero of=/tmp/testfile bs=1M count=10\n",
    "#!/bin/sh\nfind /var/log -name '*.gz' -mtime +30 -delete\n",
])
def test_looks_catastrophic_leaves_ordinary_admin_scripts_alone(script):
    assert ops.looks_catastrophic(script) is None


def test_draft_script_wraps_llm_errors(monkeypatch):
    def boom(*a, **k):
        raise LlmCallError("провайдер недоступен")

    monkeypatch.setattr(ops, "chat_once", boom)
    profile = LLMProfile(id=1, user_id=1, name="x", provider=Provider.claude, model="m", api_key_enc=encrypt("sk-x"), api_key_hint="")
    with pytest.raises(ops.OpsError, match="недоступ"):
        ops.draft_script(profile, GOAL, ["r1"])


def test_draft_script_rejects_an_empty_reply(monkeypatch):
    monkeypatch.setattr(ops, "chat_once", lambda *a, **k: "   ")
    profile = LLMProfile(id=1, user_id=1, name="x", provider=Provider.claude, model="m", api_key_enc=encrypt("sk-x"), api_key_hint="")
    with pytest.raises(ops.OpsError, match="пуст"):
        ops.draft_script(profile, GOAL, ["r1"])


def test_chat_once_sends_the_bare_model_id_never_a_strix_prefix():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    from app.services.llm import chat_once

    chat_once(
        Provider.claude, None, "ak", "claude-sonnet-4-6", system="sys", user="usr",
        transport=httpx.MockTransport(handler),
    )
    assert seen["body"]["model"] == "claude-sonnet-4-6"  # never "anthropic/claude-sonnet-4-6"
    assert seen["headers"]["x-api-key"] == "ak"


def test_a_missing_llm_profile_fails_the_run_instead_of_being_skipped(fake_host):
    """The worker must notice a gone profile and fail the run with a reason — not silently do nothing."""
    from app.worker.tasks import ops_run as ops_run_task

    user = make_user("missing-profile@example.com")
    runner = make_ssh_runner(fake_host, trusted=True)
    with SessionLocal() as db:
        run = OpsRun(user_id=user.id, name="x", goal=GOAL, llm_profile_id=None, status=OpsRunStatus.drafting)
        db.add(run)
        db.flush()
        db.add(OpsRunHost(ops_run_id=run.id, runner_id=runner.id, runner_name=runner.name))
        db.commit()
        run_id = run.id

    ops_run_task(run_id)

    row = load_run(run_id)
    assert row.status == OpsRunStatus.failed and row.script_error and "профиль" in row.script_error.lower()
    assert all(h.status == OpsHostStatus.failed for h in row.hosts)
