"""Continuing a finished scan with `strix --resume`."""
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from app.api import scans as scans_api
from app.core.config import settings
from app.engine import LocalRunner, ScanSpec
from app.models import RunnerKind, ScanStatus
from app.services.activity import ActivityTailer, Normalizer
from app.services.resume import ResumeError, check_resumable, stage_run
from app.services.scan_files import activity_path, scan_dir
from app.worker.tasks import scan_run
from tests.conftest import load_scan, make_profile, make_runner, make_scan, make_user

FAKE = Path(__file__).parent / "fake_strix.py"


def create(client, headers, **body):
    body.setdefault("targets", ["https://example.com"])
    return client.post("/api/scans", headers=headers, json=body)


def cont(client, headers, sid, **body):
    return client.post(f"/api/scans/{sid}/continue", headers=headers, json=body)


@pytest.fixture
def mock_runner():
    return make_runner()


@pytest.fixture
def queued_only(monkeypatch):
    monkeypatch.setattr(scans_api.scan_run, "delay", lambda *_a, **_k: None)


@pytest.fixture
def local_strix(tmp_path, monkeypatch):
    target = tmp_path / "bin" / "strix"
    target.parent.mkdir()
    shutil.copy(FAKE, target)
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(settings, "strix_bin", str(target))
    from app.engine import local
    monkeypatch.setattr(local, "cleanup_containers", lambda label: 0)


def run_json(scan_id):
    run = next((scan_dir(scan_id) / "strix_runs").iterdir())
    return json.loads((run / "run.json").read_text())


# ── API, with the simulated runner ──────────────────────────────────────

def test_continuing_adds_to_the_findings_and_leaves_the_parent_untouched(client, operator, mock_runner):
    parent = create(client, operator).json()
    assert parent["status"] == "vulns_found"
    parent_run = (scan_dir(parent["id"]) / "strix_runs" / parent["run_name"] / "vulnerabilities.json").read_bytes()

    r = cont(client, operator, parent["id"], instruction="Look closer at the admin area")
    assert r.status_code == 201
    child = r.json()
    assert child["parent_scan_id"] == parent["id"] and child["instruction"] == "Look closer at the admin area"
    assert child["targets"] == parent["targets"] and child["scan_mode"] == parent["scan_mode"]
    assert child["run_name"] == parent["run_name"]  # the same Strix run, continued
    assert child["status"] == "vulns_found"
    assert child["findings_total"] == parent["findings_total"] + 1  # cumulative, like the run directory

    # the parent keeps its own results and now points at its continuation
    again = client.get(f"/api/scans/{parent['id']}", headers=operator).json()
    assert again["findings_total"] == parent["findings_total"] and again["continuations"] == [child["id"]]
    assert (scan_dir(parent["id"]) / "strix_runs" / parent["run_name"] / "vulnerabilities.json").read_bytes() == parent_run
    assert client.get("/api/scans", headers=operator).json()["items"][0]["parent_scan_id"] == parent["id"]


def test_the_continuations_agent_feed_starts_after_the_parents_history(client, operator, mock_runner):
    parent = create(client, operator).json()
    parent_events = client.get(f"/api/scans/{parent['id']}/activity", headers=operator).json()["events"]
    child = cont(client, operator, parent["id"], instruction="check the admin area").json()
    page = client.get(f"/api/scans/{child['id']}/activity", headers=operator).json()
    kinds = [(e["kind"], e["role"]) for e in page["events"]]
    assert kinds[0] == ("message", "user")  # the instruction, NOT mistaken for the root agent's original task
    assert page["events"][0]["text"] == "check the admin area"
    assert "task" not in [k for k, _ in kinds[:1]] and len(page["events"]) < len(parent_events)
    assert any(e["kind"] == "tool_call" and e["tool"] == "create_agent" for e in page["events"])
    assert {a["id"] for a in page["agents"]} >= {"agent_root", "agent_recon"}  # the whole roster is still there
    assert {a["status"] for a in page["agents"]} == {"completed"}


def test_the_instruction_is_required_after_a_finished_scan_but_optional_after_a_failed_one(client, operator, mock_runner):
    done = create(client, operator).json()
    r = cont(client, operator, done["id"])
    assert r.status_code == 422 and "что агентам делать дальше" in r.json()["detail"]
    assert cont(client, operator, done["id"], instruction="   ").status_code == 422
    failed = create(client, operator, instruction="MOCK:fail").json()
    assert failed["status"] == "failed"
    assert cont(client, operator, failed["id"]).status_code == 201  # simply pick it up again


def test_cost_baseline_is_recorded_so_the_continuations_own_spend_is_known(client, operator, mock_runner):
    parent = make_scan(client.get("/api/auth/me", headers=operator).json()["id"], mock_runner.id, status=ScanStatus.cancelled,
                       run_name="r1", cost=1.5)
    _fake_run_dir(parent.id, "r1")
    child = cont(client, operator, parent.id, instruction="go on").json()
    assert child["cost_base"] == 1.5


def _fake_run_dir(scan_id, name="r1"):
    from app.engine.mock_agents import MockAgents
    run = scan_dir(scan_id) / "strix_runs" / name
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"targets_info": [{"original": "https://example.com"}], "status": "completed"}))
    agents = MockAgents(run)
    agents.spawn("agent_root", "Root Agent", None, "task")
    agents.close()
    return run


@pytest.mark.parametrize("status", [ScanStatus.queued, ScanStatus.running, ScanStatus.cancelling])
def test_only_finished_scans_can_be_continued(client, operator, mock_runner, status):
    me = client.get("/api/auth/me", headers=operator).json()["id"]
    scan = make_scan(me, mock_runner.id, status=status, run_name="r1")
    _fake_run_dir(scan.id)
    r = cont(client, operator, scan.id, instruction="x")
    assert r.status_code == 409 and "ещё выполняется" in r.json()["detail"]


def test_a_scan_without_run_data_cannot_be_continued(client, operator, mock_runner):
    me = client.get("/api/auth/me", headers=operator).json()["id"]
    never_ran = make_scan(me, mock_runner.id, status=ScanStatus.failed)
    assert "не дошёл до запуска" in cont(client, operator, never_ran.id, instruction="x").json()["detail"]
    scan = make_scan(me, mock_runner.id, status=ScanStatus.failed, run_name="r1")
    run = _fake_run_dir(scan.id)
    (run / ".state" / "agents.json").unlink()
    r = cont(client, operator, scan.id, instruction="x")
    assert r.status_code == 409 and "agents.json" in r.json()["detail"]


def test_permissions(client, admin, operator, viewer, mock_runner):
    sid = create(client, operator).json()["id"]
    assert cont(client, viewer, sid, instruction="x").status_code == 403
    assert client.post(f"/api/scans/{sid}/continue", json={"instruction": "x"}).status_code == 401
    other = make_user("op2@example.com")
    from tests.conftest import auth_headers
    assert cont(client, auth_headers(client, "op2@example.com"), sid, instruction="x").status_code == 404
    assert cont(client, admin, sid, instruction="x").status_code == 201  # admins may continue anyone's scan
    assert cont(client, operator, 99999, instruction="x").status_code == 404


def test_the_continuation_belongs_to_whoever_started_it(client, admin, operator, mock_runner):
    sid = create(client, operator).json()["id"]
    child = cont(client, admin, sid, instruction="x").json()
    assert child["owner_email"] == "admin@example.com"
    ops_view = client.get(f"/api/scans/{sid}", headers=operator).json()
    assert ops_view["continuations"] == []  # the operator may not see the admin's scan, so no id is leaked


def test_a_run_can_only_be_active_once(client, operator, mock_runner, queued_only):
    me = client.get("/api/auth/me", headers=operator).json()["id"]
    parent = make_scan(me, mock_runner.id, status=ScanStatus.succeeded, run_name="r1")
    _fake_run_dir(parent.id)
    first = cont(client, operator, parent.id, instruction="a")
    assert first.status_code == 201 and first.json()["status"] == "queued"
    second = cont(client, operator, parent.id, instruction="b")
    assert second.status_code == 409 and f"#{first.json()['id']}" in second.json()["detail"]
    client.post(f"/api/scans/{first.json()['id']}/cancel", headers=operator)
    assert cont(client, operator, parent.id, instruction="b").status_code == 201


def test_deleting_a_parent_is_blocked_while_a_continuation_still_needs_it(client, operator, mock_runner, queued_only):
    me = client.get("/api/auth/me", headers=operator).json()["id"]
    parent = make_scan(me, mock_runner.id, status=ScanStatus.succeeded, run_name="r1")
    _fake_run_dir(parent.id)
    child = cont(client, operator, parent.id, instruction="a").json()
    r = client.delete(f"/api/scans/{parent.id}", headers=operator)
    assert r.status_code == 409 and f"#{child['id']}" in r.json()["detail"]
    client.post(f"/api/scans/{child['id']}/cancel", headers=operator)
    assert client.delete(f"/api/scans/{parent.id}", headers=operator).status_code == 204
    assert client.get(f"/api/scans/{child['id']}", headers=operator).json()["parent_scan_id"] is None  # link cleared, scan kept


def test_the_active_scan_limit_applies(client, operator, mock_runner, queued_only, monkeypatch):
    me = client.get("/api/auth/me", headers=operator).json()["id"]
    parent = make_scan(me, mock_runner.id, status=ScanStatus.succeeded, run_name="r1")
    _fake_run_dir(parent.id)
    monkeypatch.setattr(settings, "max_active_scans_per_user", 1)
    make_scan(me, mock_runner.id)  # one active scan already
    assert cont(client, operator, parent.id, instruction="a").status_code == 429


def test_a_real_runner_uses_your_own_profile(client, admin, operator, local_strix, queued_only):
    local = make_runner(RunnerKind.local, "local")
    op_id = client.get("/api/auth/me", headers=operator).json()["id"]
    profile = make_profile(op_id, name="mine")
    parent = make_scan(op_id, local.id, status=ScanStatus.succeeded, run_name="r1", profile_id=profile.id)
    _fake_run_dir(parent.id)
    child = cont(client, operator, parent.id, instruction="a")
    assert child.status_code == 201 and child.json()["llm_profile_name"] == "mine"  # defaults to the parent's, as it is yours
    # an admin does not inherit somebody else's key...
    client.post(f"/api/scans/{child.json()['id']}/cancel", headers=operator)
    r = cont(client, admin, parent.id, instruction="x")
    assert r.status_code == 422 and "свой LLM-профиль" in r.json()["detail"]
    # ...and cannot borrow it either
    assert cont(client, admin, parent.id, instruction="x", llm_profile_id=profile.id).status_code == 404
    # a mock run needs no profile at all
    assert cont(client, operator, parent.id, instruction="a", llm_profile_id=profile.id).status_code == 201


@pytest.mark.parametrize("limit,status", [(1.5, 422), (2.0, 422), (2.01, 201), (50, 201), (None, 201)])
def test_strix_budget_is_for_the_whole_run_so_a_limit_below_the_spend_is_refused(client, operator, mock_runner, queued_only, limit, status):
    me = client.get("/api/auth/me", headers=operator).json()["id"]
    parent = make_scan(me, mock_runner.id, status=ScanStatus.succeeded, run_name="r1", cost=2.0)
    _fake_run_dir(parent.id)
    body = {"instruction": "go on"}
    if limit is not None:
        body["max_budget"] = limit
    r = cont(client, operator, parent.id, **body)
    assert r.status_code == status
    if status == 422:
        assert "$2.00" in r.json()["detail"]
    else:
        assert r.json()["max_budget"] == limit


def test_a_deleted_runner_blocks_continuing(client, admin, operator, mock_runner):
    sid = create(client, operator).json()["id"]
    client.delete(f"/api/runners/{mock_runner.id}", headers=admin)
    r = cont(client, operator, sid, instruction="x")
    assert r.status_code == 409 and "Раннер" in r.json()["detail"]


# ── the worker with a fake strix executable ─────────────────────────────

def local_parent(client, operator, tmp_state="ok_findings"):
    me = client.get("/api/auth/me", headers=operator).json()["id"]
    runner = make_runner(RunnerKind.local, "local")
    profile = make_profile(me, key="sk-live-key-9999999999")
    parent = make_scan(me, runner.id, profile_id=profile.id, instruction=tmp_state)
    assert scan_run(parent.id) == "vulns_found"
    return parent, runner, profile


def test_resume_runs_the_real_cli_shape_on_a_copy_of_the_run(client, operator, local_strix):
    parent, _, _ = local_parent(client, operator)
    parent_before = run_json(parent.id)
    r = cont(client, operator, parent.id, instruction="focus on the admin panel", max_budget=4)
    assert r.status_code == 201
    child = r.json()
    assert child["status"] == "vulns_found"

    argv = json.loads((scan_dir(child["id"]) / "argv.json").read_text())["argv"]
    assert "--resume=fake-run" in argv and not any(a.startswith(("--target", "-t")) for a in argv)  # CLI refuses both together
    assert "--instruction-file=instruction.txt" in argv and "--max-budget=4" in argv
    assert (scan_dir(child["id"]) / "instruction.txt").read_text() == "focus on the admin panel"

    # cumulative results, from the copy; the parent's own run directory is untouched
    assert child["findings_total"] == 4 and child["cost"] == 0.84 and child["cost_base"] == 0.42
    assert run_json(parent.id) == parent_before and load_scan(parent.id).cost == 0.42
    assert len(client.get(f"/api/scans/{parent.id}/findings", headers=operator).json()) == 3

    # the feed shows only the continuation; the instruction is the first thing in it
    events = [json.loads(l) for l in activity_path(child["id"]).read_text().splitlines()]
    assert [(e["kind"], e["role"]) for e in events] == [("message", "user"), ("message", "assistant")]
    assert events[0]["text"] == "focus on the admin panel"
    log = "\n".join(client.get(f"/api/scans/{child['id']}/log?limit=100", headers=operator).json()["lines"])
    assert "Продолжение скана" in log and "Resumed fake-run" in log


def test_the_api_key_is_passed_to_the_continuation_too(client, operator, local_strix):
    parent, _, _ = local_parent(client, operator)
    child = cont(client, operator, parent.id, instruction="x").json()
    env = json.loads((scan_dir(child["id"]) / "argv.json").read_text())["env"]
    assert env["LLM_API_KEY"] == "sk-live-key-9999999999" and env["STRIX_LLM"].startswith("anthropic/")


def test_a_startup_error_is_not_mistaken_for_findings_inherited_from_the_parent(client, operator, local_strix):
    """Exit code 2 is also what argparse returns. The run directory still holds the parent's 3 findings,
    but Strix never wrote to it, so this must be a failure, not 'vulnerabilities found'."""
    parent, _, _ = local_parent(client, operator)
    child = cont(client, operator, parent.id, instruction="startup_error please").json()
    assert child["status"] == "failed" and child["exit_code"] == 2
    assert "startup problem" in child["status_detail"]


def test_continuing_when_the_parent_vanished_before_the_worker_started(client, operator, local_strix, queued_only):
    parent, _, _ = local_parent(client, operator)
    child = cont(client, operator, parent.id, instruction="x").json()
    assert child["status"] == "queued"
    shutil.rmtree(scan_dir(parent.id))
    assert scan_run(child["id"]) == "failed"
    assert "нет файлов прогона" in load_scan(child["id"]).status_detail


def test_a_continuation_can_itself_be_continued(client, operator, local_strix):
    parent, _, _ = local_parent(client, operator)
    first = cont(client, operator, parent.id, instruction="one").json()
    second = cont(client, operator, first["id"], instruction="two").json()
    assert second["parent_scan_id"] == first["id"] and second["cost_base"] == 0.84
    assert second["findings_total"] == 5


# ── units ───────────────────────────────────────────────────────────────

def test_argv_for_a_resume(tmp_path):
    spec = ScanSpec(scan_id=1, targets=["https://a.com"], scan_mode="quick", instruction="more", max_budget=3.5,
                    work_dir=tmp_path, run_label="x", resume_run="my-run_1a2b")
    assert LocalRunner("strix").build_argv(spec) == ["strix", "-n", "-m", "quick", "--resume=my-run_1a2b",
                                                     "--instruction-file=instruction.txt", "--max-budget=3.5"]


@pytest.mark.parametrize("name", ["../x", "a/b", "..", ".hidden", "-flag", "a b", "x" * 300, "a\nb"])
def test_run_names_cannot_escape_the_scan_directory(name):
    # Must be refused as an invalid NAME — not merely fail later because no such directory exists.
    with pytest.raises(ResumeError, match="Недопустимое имя"):
        check_resumable(1, name)


def test_traversal_is_refused_even_if_the_name_pattern_were_loosened(monkeypatch):
    """Second line of defence: the resolved path has to stay inside the parent's strix_runs/."""
    import re
    from app.services import resume
    monkeypatch.setattr(resume, "RUN_NAME_RE", re.compile(r"^.{1,200}$"))
    for name in ("../x", "../../etc", "..", "a/../../x"):
        with pytest.raises(ResumeError, match="Недопустимое имя"):
            check_resumable(1, name)


def test_stage_run_copies_and_never_follows_symlinks(tmp_path):
    src = _fake_run_dir(1, "r1")
    (src / "note.txt").write_text("hi")
    os.symlink("/etc/passwd", src / "evil-link")
    dst = stage_run(1, "r1", scan_dir(2))
    assert (dst / "note.txt").read_text() == "hi" and (dst / ".state" / "agents.db").is_file()
    assert (dst / "evil-link").is_symlink()  # copied as a link, the target was NOT read into the new scan
    assert not (src.parent / "r1" / "copied-from").exists()
    with pytest.raises(ResumeError, match="уже существует"):
        stage_run(1, "r1", scan_dir(2))


def test_normalizer_treats_the_injected_instruction_as_a_user_message_not_a_task():
    n = Normalizer()
    n.seen_user.add("root")  # what skip_existing does for agents that already have history
    e = n.convert("root", {"role": "user", "content": "now test the admin area"}, None)
    assert (e["kind"], e["role"]) == ("message", "user")
    assert Normalizer().convert("root", {"role": "user", "content": "now test the admin area"}, None)["kind"] == "task"
