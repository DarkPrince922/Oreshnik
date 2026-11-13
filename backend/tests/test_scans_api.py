import pytest

from app.api import scans as scans_api
from app.core.config import settings
from app.models import RunnerKind, ScanStatus
from app.services.scan_files import scan_dir
from tests.conftest import auth_headers, load_scan, make_profile, make_runner, make_scan, make_user
from app.models import Role


@pytest.fixture
def queued_only(monkeypatch):
    """Creating a scan leaves it queued (no worker picks it up), so state transitions can be driven by hand."""
    monkeypatch.setattr(scans_api.scan_run, "delay", lambda *_a, **_k: None)


def create(client, headers, **body):
    body.setdefault("targets", ["https://example.com"])
    return client.post("/api/scans", headers=headers, json=body)


@pytest.fixture
def mock_runner():
    return make_runner()


# ── creation ────────────────────────────────────────────────────────────

def test_create_runs_on_the_default_mock_runner_and_needs_no_profile(client, operator, mock_runner):
    r = create(client, operator, targets=["https://example.com"], scan_mode="quick")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "vulns_found"  # the (inline) worker already finished it
    assert body["runner_name"] == "mock-runner" and body["owner_email"] == "op@example.com"
    assert body["scan_mode"] == "quick" and body["findings_total"] == sum(body["severity_counts"].values()) >= 3


def test_default_scan_mode_is_the_cheapest(client, operator, mock_runner, queued_only):
    assert create(client, operator).json()["scan_mode"] == "quick"


def test_viewers_cannot_create_scans(client, viewer, mock_runner):
    assert create(client, viewer).status_code == 403
    assert client.post("/api/scans", json={"targets": ["x.com"]}).status_code == 401


@pytest.mark.parametrize("bad", [
    {"targets": []}, {"targets": ["-evil"]}, {"targets": ["ftp://x.com"]}, {"targets": ["a b"]},
    {"targets": ["x.com"], "scan_mode": "extreme"}, {"targets": ["x.com"], "max_budget": 0},
    {"targets": ["x.com"], "max_budget": -1}, {"targets": ["x.com"] * 0 + ["x%d.com" % i for i in range(21)]},
    {"targets": ["x.com"], "instruction": "x" * 10001},
])
def test_invalid_input_is_rejected(client, operator, mock_runner, bad):
    assert client.post("/api/scans", headers=operator, json=bad).status_code == 422


def test_local_paths_are_admin_only(client, admin, operator, mock_runner, queued_only):
    for path in ("/etc", "~", "./app", "localhost"):
        r = create(client, operator, targets=[path])
        assert r.status_code == 403, path
    assert create(client, operator, targets=["https://ok.com", "/etc"]).status_code == 403
    assert create(client, admin, targets=["./my-project"]).status_code == 201


def test_duplicate_targets_collapse(client, operator, mock_runner, queued_only):
    assert create(client, operator, targets=["a.com", "https://b.com", "a.com"]).json()["targets"] == ["a.com", "https://b.com"]


def test_real_runner_requires_a_profile_and_it_must_be_yours(client, operator, admin, queued_only):
    local = make_runner(RunnerKind.local, "local")
    assert create(client, operator, runner_id=local.id).status_code == 422
    admin_id = client.get("/api/auth/me", headers=admin).json()["id"]
    foreign = make_profile(admin_id)
    assert create(client, operator, runner_id=local.id, llm_profile_id=foreign.id).status_code == 404
    op_id = client.get("/api/auth/me", headers=operator).json()["id"]
    mine = make_profile(op_id, name="mine")
    r = create(client, operator, runner_id=local.id, llm_profile_id=mine.id)
    assert r.status_code == 201 and r.json()["llm_profile_name"] == "mine" and r.json()["status"] == "queued"


def test_unknown_and_unsupported_runners(client, operator, queued_only):
    assert create(client, operator).status_code == 422  # no runner exists at all
    assert create(client, operator, runner_id=999).status_code == 404
    ssh = make_runner(RunnerKind.ssh, "remote")
    assert create(client, operator, runner_id=ssh.id).status_code == 422


def test_active_scan_limit_protects_against_runaway_spend(client, operator, mock_runner, queued_only, monkeypatch):
    monkeypatch.setattr(settings, "max_active_scans_per_user", 2)
    assert create(client, operator).status_code == 201
    assert create(client, operator).status_code == 201
    third = create(client, operator)
    assert third.status_code == 429 and "Лимит" in third.json()["detail"]


def test_the_limit_is_per_user_and_frees_up_when_scans_finish(client, admin, operator, mock_runner, queued_only, monkeypatch):
    monkeypatch.setattr(settings, "max_active_scans_per_user", 1)
    first = create(client, operator).json()["id"]
    assert create(client, operator).status_code == 429
    assert create(client, admin).status_code == 201  # someone else's quota is separate
    client.post(f"/api/scans/{first}/cancel", headers=operator)
    assert create(client, operator).status_code == 201


def test_queue_outage_marks_the_scan_failed_and_returns_503(client, operator, mock_runner, monkeypatch):
    def boom(*_a, **_k):
        raise ConnectionError("redis down")
    monkeypatch.setattr(scans_api.scan_run, "delay", boom)
    r = create(client, operator)
    assert r.status_code == 503
    listing = client.get("/api/scans", headers=operator).json()["items"]
    assert listing[0]["status"] == "failed" and "Очередь" in listing[0]["status_detail"]


# ── visibility ──────────────────────────────────────────────────────────

def test_operators_see_only_their_scans_while_admins_and_viewers_see_all(client, admin, operator, viewer, mock_runner, queued_only):
    mine = create(client, operator).json()["id"]
    theirs = create(client, admin).json()["id"]
    other_op = make_user("op2@example.com")
    other = make_scan(other_op.id, mock_runner.id).id

    def ids(headers):
        return sorted(s["id"] for s in client.get("/api/scans", headers=headers).json()["items"])

    assert ids(operator) == [mine]
    assert ids(admin) == sorted([mine, theirs, other])
    assert ids(viewer) == sorted([mine, theirs, other])
    # Direct access follows the same rule — a foreign scan is indistinguishable from a missing one.
    for path in ("", "/log", "/findings", "/report", "/export/json"):
        assert client.get(f"/api/scans/{theirs}{path}", headers=operator).status_code == 404, path
        assert client.get(f"/api/scans/{theirs}{path}", headers=viewer).status_code == 200, path
    assert client.post(f"/api/scans/{theirs}/cancel", headers=operator).status_code == 404
    assert client.delete(f"/api/scans/{theirs}", headers=operator).status_code == 404


def test_viewers_cannot_see_instructions_that_may_contain_credentials(client, operator, viewer, mock_runner, queued_only):
    sid = create(client, operator, instruction="login with admin:hunter2").json()["id"]
    assert client.get(f"/api/scans/{sid}", headers=operator).json()["instruction"] == "login with admin:hunter2"
    assert client.get(f"/api/scans/{sid}", headers=viewer).json()["instruction"] is None
    assert client.get("/api/scans", headers=viewer).json()["items"][0]["instruction"] is None


def test_viewers_cannot_modify(client, operator, viewer, mock_runner, queued_only):
    sid = create(client, operator).json()["id"]
    assert client.post(f"/api/scans/{sid}/cancel", headers=viewer).status_code == 403
    assert client.delete(f"/api/scans/{sid}", headers=viewer).status_code == 403


def test_listing_filters_and_paginates(client, operator, mock_runner, queued_only, monkeypatch):
    monkeypatch.setattr(settings, "max_active_scans_per_user", 50)
    ids = [create(client, operator).json()["id"] for _ in range(5)]
    client.post(f"/api/scans/{ids[0]}/cancel", headers=operator)
    page = client.get("/api/scans?limit=2&offset=1", headers=operator).json()
    assert page["total"] == 5 and [s["id"] for s in page["items"]] == [ids[3], ids[2]]  # newest first
    cancelled = client.get("/api/scans?status=cancelled", headers=operator).json()
    assert cancelled["total"] == 1 and cancelled["items"][0]["id"] == ids[0]
    assert client.get("/api/scans?status=bogus", headers=operator).status_code == 422
    assert client.get("/api/scans?limit=0", headers=operator).status_code == 422


# ── control ─────────────────────────────────────────────────────────────

def test_cancelling_a_queued_scan_is_immediate_and_final(client, operator, mock_runner, queued_only):
    sid = create(client, operator).json()["id"]
    r = client.post(f"/api/scans/{sid}/cancel", headers=operator)
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["finished_at"]
    from app.worker.tasks import scan_run
    assert scan_run(sid) == "skipped"  # the worker finds it cancelled and never starts it


def test_cancelling_a_running_scan_asks_the_worker_to_stop_and_is_idempotent(client, operator, mock_runner, queued_only):
    owner = client.get("/api/auth/me", headers=operator).json()["id"]
    sid = make_scan(owner, mock_runner.id, status=ScanStatus.running).id
    r = client.post(f"/api/scans/{sid}/cancel", headers=operator)
    assert r.status_code == 200 and r.json()["status"] == "cancelling"
    assert client.post(f"/api/scans/{sid}/cancel", headers=operator).json()["status"] == "cancelling"


def test_cancelling_a_finished_scan_conflicts(client, operator, mock_runner):
    sid = create(client, operator).json()["id"]  # finishes inline
    assert client.post(f"/api/scans/{sid}/cancel", headers=operator).status_code == 409


def test_admin_can_cancel_and_delete_anyones_scan(client, admin, operator, mock_runner, queued_only):
    sid = create(client, operator).json()["id"]
    assert client.post(f"/api/scans/{sid}/cancel", headers=admin).json()["status"] == "cancelled"
    assert client.delete(f"/api/scans/{sid}", headers=admin).status_code == 204


def test_delete_requires_a_finished_scan_and_removes_the_files(client, operator, mock_runner, queued_only):
    sid = create(client, operator).json()["id"]
    assert client.delete(f"/api/scans/{sid}", headers=operator).status_code == 409  # still queued
    client.post(f"/api/scans/{sid}/cancel", headers=operator)
    scan_dir(sid).mkdir(parents=True)
    (scan_dir(sid) / "oreshnik.log").write_text("x\n")
    assert client.delete(f"/api/scans/{sid}", headers=operator).status_code == 204
    assert not scan_dir(sid).exists()
    assert client.get(f"/api/scans/{sid}", headers=operator).status_code == 404


def test_deleting_a_scan_deletes_its_findings(client, operator, mock_runner):
    sid = create(client, operator).json()["id"]
    from app.core.db import SessionLocal
    from app.models import Finding
    with SessionLocal() as db:
        assert db.query(Finding).count() >= 3
    client.delete(f"/api/scans/{sid}", headers=operator)
    with SessionLocal() as db:
        assert db.query(Finding).count() == 0


def test_deleting_the_runner_or_profile_keeps_the_scan(client, admin, operator, mock_runner):
    sid = create(client, operator).json()["id"]
    assert client.delete(f"/api/runners/{mock_runner.id}", headers=admin).status_code == 204
    body = client.get(f"/api/scans/{sid}", headers=operator).json()
    assert body["runner_id"] is None and body["runner_name"] is None and body["status"] == "vulns_found"


def test_deleting_a_user_deletes_their_scans(client, admin, mock_runner):
    victim = make_user("v@example.com")
    sid = make_scan(victim.id, mock_runner.id, status=ScanStatus.succeeded).id
    assert client.delete(f"/api/users/{victim.id}", headers=admin).status_code == 204
    assert client.get(f"/api/scans/{sid}", headers=admin).status_code == 404


# ── results ─────────────────────────────────────────────────────────────

def test_findings_log_report_and_exports_of_a_finished_scan(client, operator, viewer, mock_runner):
    sid = create(client, operator, targets=["https://example.com"]).json()["id"]

    found = client.get(f"/api/scans/{sid}/findings", headers=operator).json()
    assert len(found) >= 3
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    assert [rank[f["severity"]] for f in found] == sorted(rank[f["severity"]] for f in found)
    detail = client.get(f"/api/scans/{sid}/findings/{found[0]['id']}", headers=viewer).json()
    assert detail["data"]["id"] == found[0]["vuln_id"] and "remediation_steps" in detail["data"]
    assert client.get(f"/api/scans/{sid}/findings/999999", headers=operator).status_code == 404

    page = client.get(f"/api/scans/{sid}/log?offset=0&limit=3", headers=operator).json()
    assert len(page["lines"]) == 3 and page["next_offset"] == 3 and page["finished"] is True
    rest = client.get(f"/api/scans/{sid}/log?offset=3&limit=5000", headers=operator).json()
    assert "Scan completed" in rest["lines"][-1]
    assert client.get(f"/api/scans/{sid}/log?limit=99999", headers=operator).status_code == 422

    report = client.get(f"/api/scans/{sid}/report", headers=viewer).json()["markdown"]
    assert report.startswith("# Security Penetration Test Report") and "[MOCK]" in report
    assert "## Targets" in report and "- https://example.com" in report  # real reports quote their targets too

    exported = client.get(f"/api/scans/{sid}/export/json", headers=viewer)
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    assert [f["id"] for f in exported.json()] == [f["vuln_id"] for f in sorted(found, key=lambda f: f["id"])]
    sarif = client.get(f"/api/scans/{sid}/export/sarif", headers=operator)
    assert sarif.json()["version"] == "2.1.0" and "attachment" in sarif.headers["content-disposition"]
    md = client.get(f"/api/scans/{sid}/export/md", headers=operator)
    assert md.text.startswith("# Security") and md.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/api/scans/{sid}/export/exe", headers=operator).status_code == 422


def test_results_of_a_scan_that_has_not_run_yet(client, operator, mock_runner, queued_only):
    sid = create(client, operator).json()["id"]
    assert client.get(f"/api/scans/{sid}/findings", headers=operator).json() == []
    assert client.get(f"/api/scans/{sid}/log", headers=operator).json() == {"lines": [], "next_offset": 0, "finished": False}
    assert client.get(f"/api/scans/{sid}/report", headers=operator).json() == {"markdown": None}
    assert client.get(f"/api/scans/{sid}/export/sarif", headers=operator).status_code == 404


def test_failed_scan_shows_why(client, operator, mock_runner):
    sid = create(client, operator, instruction="MOCK:fail").json()["id"]
    body = client.get(f"/api/scans/{sid}", headers=operator).json()
    assert body["status"] == "failed" and body["exit_code"] == 1 and "model connection failed" in body["status_detail"]


# ── agent activity ──────────────────────────────────────────────────────

def test_activity_endpoint_returns_the_transcript_and_the_roster(client, operator, mock_runner):
    sid = create(client, operator).json()["id"]
    page = client.get(f"/api/scans/{sid}/activity", headers=operator).json()
    assert page["finished"] is True and len(page["events"]) > 15
    assert {a["id"] for a in page["agents"]} >= {"agent_root", "agent_recon"}
    first = page["events"][0]
    assert first["kind"] == "task" and first["seq"] == 0 and first["agent"] == "agent_root"
    call = next(e for e in page["events"] if e["kind"] == "tool_call" and e["tool"] == "exec_command")
    assert call["args"]["cmd"] == call["summary"] or call["summary"] in call["args"]["cmd"]
    # nothing new after the cursor
    again = client.get(f"/api/scans/{sid}/activity?cursor={page['next_cursor']}", headers=operator).json()
    assert again["events"] == [] and again["next_cursor"] == page["next_cursor"]


def test_activity_paging_by_cursor_reassembles_the_whole_stream(client, operator, mock_runner, monkeypatch):
    sid = create(client, operator).json()["id"]
    whole = client.get(f"/api/scans/{sid}/activity", headers=operator).json()["events"]
    monkeypatch.setattr(scans_api, "ACTIVITY_PAGE_BYTES", 1500)
    cursor, collected, pages = 0, [], 0
    while True:
        page = client.get(f"/api/scans/{sid}/activity?cursor={cursor}", headers=operator).json()
        pages += 1
        if not page["events"]:
            break
        collected += page["events"]
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
    assert pages > 3 and collected == whole  # no event lost, duplicated or reordered across page boundaries
    # The cursor must land exactly on a line boundary: a byte too early would re-read the previous
    # newline (an empty line the endpoint silently drops), so check the arithmetic itself.
    from app.services.scan_files import activity_path
    raw = activity_path(sid).read_bytes()
    page = client.get(f"/api/scans/{sid}/activity?cursor=0", headers=operator).json()
    assert raw[: page["next_cursor"]].endswith(b"\n") and page["next_cursor"] <= len(raw)


def test_activity_is_not_available_to_viewers_and_follows_scan_visibility(client, admin, operator, viewer, mock_runner):
    sid = create(client, operator).json()["id"]
    assert client.get(f"/api/scans/{sid}/activity", headers=viewer).status_code == 403
    assert client.get(f"/api/scans/{sid}/activity", headers=admin).status_code == 200
    assert client.get(f"/api/scans/{sid}/activity").status_code == 401
    other = make_user("op2@example.com")
    foreign = make_scan(other.id, mock_runner.id).id
    assert client.get(f"/api/scans/{foreign}/activity", headers=operator).status_code == 404
    assert client.get("/api/scans/999999/activity", headers=operator).status_code == 404


def test_activity_of_a_scan_that_has_not_started(client, operator, mock_runner, queued_only):
    sid = create(client, operator).json()["id"]
    assert client.get(f"/api/scans/{sid}/activity", headers=operator).json() == {
        "events": [], "next_cursor": 0, "agents": [], "finished": False}


def test_a_corrupt_activity_line_does_not_hide_the_rest(client, operator, mock_runner):
    from app.services.scan_files import activity_path
    sid = create(client, operator).json()["id"]
    path = activity_path(sid)
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], "{ definitely not json", *lines[1:]]) + "\n")
    events = client.get(f"/api/scans/{sid}/activity", headers=operator).json()["events"]
    assert len(events) == len(lines)


def test_hostile_target_text_is_returned_verbatim_for_the_client_to_escape(client, operator, mock_runner):
    evil = "https://example.com/<script>alert(1)</script>"
    sid = create(client, operator, targets=[evil]).json()["id"]
    page = client.get(f"/api/scans/{sid}/activity", headers=operator).json()
    assert any(evil in (e["text"] or "") or evil in (e["summary"] or "") for e in page["events"])


def test_activity_of_a_finished_scan_without_a_feed_is_rebuilt_from_strix_data(client, operator, mock_runner):
    """Scans from before the feature existed (or from an older worker) still have Strix's agents.db."""
    from app.engine.mock_agents import MockAgents
    from app.services.scan_files import activity_path, scan_dir
    me = client.get("/api/auth/me", headers=operator).json()["id"]
    sid = make_scan(me, mock_runner.id, status=ScanStatus.succeeded).id
    run = scan_dir(sid) / "strix_runs" / "old-run"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}")
    agents = MockAgents(run)
    agents.spawn("agent_root", "Root Agent", None, "scan it")
    agents.run_command("agent_root", "whoami", "root")
    agents.close()
    assert not activity_path(sid).exists()
    page = client.get(f"/api/scans/{sid}/activity", headers=operator).json()
    assert [e["kind"] for e in page["events"]] == ["task", "tool_call", "tool_output"]
    assert page["agents"][0]["name"] == "Root Agent"
    again = client.get(f"/api/scans/{sid}/activity", headers=operator).json()
    assert again["events"] == page["events"]  # built once, not duplicated


def test_a_running_scan_is_never_backfilled_from_the_api_process(client, operator, mock_runner):
    from app.engine.mock_agents import MockAgents
    from app.services.scan_files import activity_path, scan_dir
    me = client.get("/api/auth/me", headers=operator).json()["id"]
    sid = make_scan(me, mock_runner.id, status=ScanStatus.running).id
    run = scan_dir(sid) / "strix_runs" / "live"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}")
    agents = MockAgents(run)
    agents.spawn("a", "A", None, "task")
    agents.close()
    page = client.get(f"/api/scans/{sid}/activity", headers=operator).json()
    assert page["events"] == [] and not activity_path(sid).exists()  # the worker's tailer owns running scans
