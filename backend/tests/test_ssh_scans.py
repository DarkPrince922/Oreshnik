"""Scans on a remote host: the whole path through the SSH runner, against a real SSH server and a fake `strix` on it."""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.db import SessionLocal
from app.engine import ScanSpec
from app.engine.ssh import SshRunner, build_script, cleanup_remote, remote_dir
from app.models import RunnerKind, Scan, ScanStatus
from app.services.scan_files import activity_path, scan_dir
from app.worker import tasks
from app.worker.tasks import reap_stale_scans, scan_run
from tests.conftest import load_scan, make_profile, make_runner, make_scan, make_ssh_runner, make_user

KEY = "sk-live-key-9999999999"


def me(client, headers) -> int:
    return client.get("/api/auth/me", headers=headers).json()["id"]


@pytest.fixture
def remote(client, operator, fake_host, monkeypatch):
    """A trusted SSH runner on a fake host, and a profile of the operator's."""
    monkeypatch.setattr(settings, "ssh_sync_interval_seconds", 0.2)
    monkeypatch.setattr(settings, "strix_cancel_grace_seconds", 1)
    monkeypatch.setattr(tasks._CancelProbe, "INTERVAL", 0.05)
    runner = make_ssh_runner(fake_host, trusted=True)
    profile = make_profile(me(client, operator), key=KEY)
    return runner, profile


def new_scan(client, operator, remote, instruction="ok_findings", **extra):
    runner, profile = remote
    return make_scan(me(client, operator), runner.id, profile_id=profile.id, instruction=instruction, **extra)


def api_create(client, headers, remote, **body):
    runner, profile = remote
    body.setdefault("targets", ["https://example.com"])
    return client.post("/api/scans", headers=headers, json={"runner_id": runner.id, "llm_profile_id": profile.id, **body})


def processes_under(home: Path) -> list[int]:
    """Processes whose working directory is somewhere in the fake host's home (that is where Strix runs)."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if os.readlink(entry / "cwd").startswith(str(home)):
                    found.append(int(entry.name))
            except OSError:
                pass
    return found


def wait_until(predicate, timeout=20, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def run_in_thread(scan_id):
    t = threading.Thread(target=lambda: scan_run(scan_id))
    t.start()
    return t


# ── the whole path ──────────────────────────────────────────────────────

def test_a_scan_runs_on_the_host_and_its_results_come_home(client, operator, remote, fake_host):
    out = api_create(client, operator, remote, instruction="ok_findings", scan_mode="standard")
    assert out.status_code == 201, out.text
    scan = out.json()
    assert scan["status"] == "vulns_found" and scan["findings_total"] == 3 and scan["cost"] == 0.42
    assert scan["run_name"] == "fake-run" and scan["runner_kind"] == "ssh" and scan["exit_code"] == 2
    # the panel's own scan directory holds the results, exactly as for a local scan
    run = scan_dir(scan["id"]) / "strix_runs" / "fake-run"
    assert json.loads((run / "run.json").read_text())["status"] == "completed" and (run / "penetration_test_report.md").is_file()
    assert len(client.get(f"/api/scans/{scan['id']}/findings", headers=operator).json()) == 3
    assert "Security Penetration Test Report" in client.get(f"/api/scans/{scan['id']}/report", headers=operator).text
    log = "\n".join(client.get(f"/api/scans/{scan['id']}/log?limit=200", headers=operator).json()["lines"])
    assert "Подключено к scan@127.0.0.1" in log and "done: 3 findings" in log and "--target=https://example.com" in log
    # the host is left clean: no scan directory, no containers
    assert not (fake_host.home / "oreshnik" / f"scan-{scan['id']}").exists() and processes_under(fake_host.home) == []
    assert any(f"label=strix-run-id=oreshnik-scan-{scan['id']}" in c for c in fake_host.docker_calls())


def test_the_activity_feed_and_roster_arrive_too(client, operator, remote):
    scan = api_create(client, operator, remote, instruction="ok_findings").json()
    page = client.get(f"/api/scans/{scan['id']}/activity", headers=operator).json()
    kinds = [e["kind"] for e in page["events"]]
    assert kinds.count("tool_call") == 1 and any(e.get("text") == "child works" for e in page["events"])
    assert [e["seq"] for e in page["events"]] == list(range(len(kinds)))
    assert KEY not in json.dumps(page) and "key=***" in json.dumps(page)  # what the agents printed is redacted as well
    assert {a["id"] for a in page["agents"]} == {"root", "child"} and {a["status"] for a in page["agents"]} == {"completed"}


def test_a_failing_strix_fails_the_scan_with_its_message(client, operator, remote):
    scan = api_create(client, operator, remote, instruction="fail").json()
    assert scan["status"] == "failed" and scan["exit_code"] == 1 and "DOCKER NOT AVAILABLE" in scan["status_detail"]


def test_a_clean_scan_is_a_clean_scan(client, operator, remote):
    scan = api_create(client, operator, remote, instruction="ok_clean").json()
    assert scan["status"] == "succeeded" and scan["findings_total"] == 0 and scan["exit_code"] == 0


def test_two_scans_on_one_host_do_not_get_in_each_others_way(client, operator, remote, fake_host):
    a = new_scan(client, operator, remote, "slow")
    b = new_scan(client, operator, remote, "slow")
    ta, tb = run_in_thread(a.id), run_in_thread(b.id)
    assert wait_until(lambda: len(processes_under(fake_host.home)) >= 2)
    dirs = {p.name for p in (fake_host.home / "oreshnik").iterdir()}
    assert dirs == {f"scan-{a.id}", f"scan-{b.id}"}
    client.post(f"/api/scans/{a.id}/cancel", headers=operator)
    ta.join(timeout=30)
    assert load_scan(a.id).status == ScanStatus.cancelled and load_scan(b.id).status == ScanStatus.running
    assert (fake_host.home / "oreshnik" / f"scan-{b.id}").exists()  # the other scan was not touched
    client.post(f"/api/scans/{b.id}/cancel", headers=operator)
    tb.join(timeout=30)
    assert load_scan(b.id).status == ScanStatus.cancelled and processes_under(fake_host.home) == []


# ── secrets ─────────────────────────────────────────────────────────────

def test_the_llm_key_never_appears_in_a_command_line_or_on_the_remote_disk(client, operator, remote, fake_host):
    scan = new_scan(client, operator, remote, "slow")
    t = run_in_thread(scan.id)
    remote_scan = fake_host.home / "oreshnik" / f"scan-{scan.id}"
    assert wait_until(lambda: (remote_scan / ".pid").exists() and (remote_scan / "argv.json").exists())
    # (argv.json is the fake strix's own dump of its environment; every other file is something the panel wrote)
    panel_files = [p for p in remote_scan.rglob("*") if p.is_file() and p.name != "argv.json" and not p.is_relative_to(remote_scan / "strix_runs")]
    assert panel_files and all(KEY not in p.read_text(errors="replace") for p in panel_files)
    assert all(KEY not in c for c in fake_host.commands)  # visible to every user of the host through `ps`
    assert any(KEY in i for i in fake_host.inputs)  # it travelled on stdin, where only the shell reads it
    client.post(f"/api/scans/{scan.id}/cancel", headers=operator)
    t.join(timeout=30)
    assert KEY not in "\n".join(client.get(f"/api/scans/{scan.id}/log?limit=500", headers=operator).json()["lines"])


def test_a_key_echoed_by_strix_is_redacted_in_the_log_and_the_feed(client, operator, remote):
    scan = api_create(client, operator, remote, instruction="secret_echo").json()
    log = "\n".join(client.get(f"/api/scans/{scan['id']}/log?limit=200", headers=operator).json()["lines"])
    assert "LLM_API_KEY=***" in log and KEY not in log


def test_the_ssh_private_key_is_never_logged(client, operator, remote, fake_host):
    scan = api_create(client, operator, remote).json()
    body = json.dumps(client.get(f"/api/scans/{scan['id']}/log?limit=500", headers=operator).json())
    assert fake_host.client_key_pem.splitlines()[1] not in body


def test_hostile_targets_and_values_stay_literal(tmp_path, fake_host):
    """A target is user input that ends up on a shell command line. It must arrive at strix exactly as typed."""
    bindir = fake_host.home / ".local" / "bin"
    (bindir / "strix").write_text('#!/bin/sh\nfor a in "$@"; do printf "ARG<%s>\\n" "$a"; done\nprintenv STRIX_RUN_ID LLM_API_BASE\n')
    (bindir / "strix").chmod(0o755)
    pwned_arg, pwned_env = tmp_path / "pwned-arg", tmp_path / "pwned-env"  # unique per run: a stale file cannot fool the test
    hostile = f"https://x.example/?q=$(touch {pwned_arg});`id`'\"\\&|>"
    base_url = f"https://llm.example/v1?x=$(touch {pwned_env})"
    lines: list[str] = []
    spec = ScanSpec(scan_id=77, targets=[hostile, "192.168.0.1"], scan_mode="deep", instruction=None, max_budget=2.5,
                    work_dir=tmp_path / "scan", run_label="oreshnik-scan-77", env={"LLM_API_BASE": base_url, "LLM_API_KEY": KEY},
                    ssh=fake_host.target())
    result = SshRunner().run(spec, lines.append, lambda: False)
    assert result.error is None and result.exit_code == 0, result
    args = [l[4:-1] for l in lines if l.startswith("ARG<")]
    assert args == ["-n", "-m", "deep", f"--target={hostile}", "--target=192.168.0.1", "--max-budget=2.5"]
    assert "oreshnik-scan-77" in lines and base_url in lines
    assert not pwned_arg.exists() and not pwned_env.exists()


def test_the_script_is_built_from_quoted_words_only():
    spec = ScanSpec(scan_id=5, targets=["x"], scan_mode="quick", instruction=None, max_budget=None, work_dir=Path("/x"),
                    run_label="oreshnik-scan-5", env={"STRIX_LLM": "anthropic/claude", "LLM_API_KEY": "a b'c$d"})
    script = build_script(spec, ["strix", "-n", "--target=a b;c"], remote_dir(5))
    assert "export LLM_API_KEY='a b'\"'\"'c$d'" in script and "'--target=a b;c'" in script
    assert 'cd "$HOME/oreshnik/scan-5"' in script and script.rstrip().endswith("</dev/null")
    bad = ScanSpec(scan_id=5, targets=["x"], scan_mode="quick", instruction=None, max_budget=None, work_dir=Path("/x"),
                   run_label="l", env={"A;rm -rf /": "x"})
    with pytest.raises(Exception):
        build_script(bad, ["strix"], remote_dir(5))


# ── cancelling and timing out ───────────────────────────────────────────

@pytest.mark.parametrize("mode", ["slow", "ignore_sigint", "spawn_child"])
def test_cancel_stops_the_whole_process_group_on_the_host(client, operator, remote, fake_host, mode):
    scan = new_scan(client, operator, remote, mode)
    t = run_in_thread(scan.id)
    assert wait_until(lambda: processes_under(fake_host.home))
    assert wait_until(lambda: load_scan(scan.id).status == ScanStatus.running)
    time.sleep(0.5)
    started = time.time()
    assert client.post(f"/api/scans/{scan.id}/cancel", headers=operator).status_code == 200
    t.join(timeout=40)
    row = load_scan(scan.id)
    assert row.status == ScanStatus.cancelled and not t.is_alive()
    assert wait_until(lambda: processes_under(fake_host.home) == [], timeout=10), "Strix (or its child) is still running on the host"
    if mode != "slow":
        assert time.time() - started < 30  # SIGINT is ignored: the escalation (TERM, then KILL) is what ended it
    assert any("label=strix-run-id" in c for c in fake_host.docker_calls())  # and the sandbox containers were removed


def test_a_graceful_cancel_keeps_what_was_found_so_far(client, operator, remote):
    scan = new_scan(client, operator, remote, "slow")
    t = run_in_thread(scan.id)
    assert wait_until(lambda: load_scan(scan.id).status == ScanStatus.running)
    time.sleep(1.0)
    client.post(f"/api/scans/{scan.id}/cancel", headers=operator)
    t.join(timeout=30)
    row = load_scan(scan.id)
    assert row.status == ScanStatus.cancelled and row.run_status == "interrupted"  # the run directory was fetched after SIGINT
    assert "Interrupted" in "\n".join(client.get(f"/api/scans/{scan.id}/log?limit=500", headers=operator).json()["lines"])


def test_the_time_limit_stops_a_scan_on_the_host(tmp_path, fake_host):
    (fake_host.home / "x").mkdir()
    lines: list[str] = []
    spec = ScanSpec(scan_id=3, targets=["https://example.com"], scan_mode="quick", instruction="slow", max_budget=None,
                    work_dir=tmp_path / "scan", run_label="oreshnik-scan-3", timeout_seconds=2, ssh=fake_host.target())
    started = time.time()
    result = SshRunner().run(spec, lines.append, lambda: False)
    assert result.timed_out and not result.cancelled and time.time() - started < 30
    assert wait_until(lambda: processes_under(fake_host.home) == [])


# ── when the connection goes wrong ──────────────────────────────────────

def test_a_connection_lost_mid_scan_fails_the_scan_and_stops_strix_on_the_host(client, operator, remote, fake_host):
    scan = new_scan(client, operator, remote, "slow")
    t = run_in_thread(scan.id)
    assert wait_until(lambda: processes_under(fake_host.home))
    time.sleep(0.5)
    fake_host.kick()
    t.join(timeout=40)
    row = load_scan(scan.id)
    assert row.status == ScanStatus.failed and "Потеряно соединение" in row.status_detail
    assert wait_until(lambda: processes_under(fake_host.home) == [], timeout=15), "the orphaned Strix was left running on the host"


def test_an_unreachable_host_fails_the_scan_with_the_reason(client, operator, remote, fake_host):
    scan = new_scan(client, operator, remote)
    fake_host.stop()
    assert scan_run(scan.id) == "failed"
    detail = load_scan(scan.id).status_detail
    assert any(word in detail for word in ("отклонено", "недоступен", "вовремя"))  # the reason, not a bare "failed"


def test_a_host_key_that_changed_since_it_was_confirmed_stops_the_scan_before_any_secret_leaves(client, operator, remote, fake_host):
    scan = new_scan(client, operator, remote)
    fake_host.rotate_host_key()
    assert scan_run(scan.id) == "failed"
    row = load_scan(scan.id)
    assert "подмена" in row.status_detail
    assert fake_host.attempts == 0 and fake_host.inputs == [] and KEY not in " ".join(fake_host.commands)


def test_a_missing_confirmation_is_caught_before_the_run(client, operator, fake_host):
    runner = make_ssh_runner(fake_host, trusted=False)
    profile = make_profile(me(client, operator))
    scan = make_scan(me(client, operator), runner.id, profile_id=profile.id)
    assert scan_run(scan.id) == "failed"
    assert "не подтверждён" in load_scan(scan.id).status_detail and fake_host.attempts == 0 and fake_host.connections == []


def test_a_scan_without_a_profile_is_refused_before_connecting(client, operator, remote, fake_host):
    runner, _ = remote
    scan = make_scan(me(client, operator), runner.id)
    assert scan_run(scan.id) == "failed"
    assert "LLM-профиль" in load_scan(scan.id).status_detail and fake_host.connections == []


def test_a_deleted_runner_is_reported(client, operator, remote, fake_host):
    scan = new_scan(client, operator, remote)
    with SessionLocal() as db:
        from app.models import Runner
        db.query(Runner).filter(Runner.id == remote[0].id).delete()
        db.commit()
    assert scan_run(scan.id) == "failed" and "Раннер был удалён" in load_scan(scan.id).status_detail


def test_results_that_cannot_be_fetched_keep_the_remote_copy_and_say_where(client, operator, remote, fake_host, monkeypatch):
    monkeypatch.setattr(settings, "ssh_fetch_max_files", 1)  # the run directory has more than one file
    scan = new_scan(client, operator, remote)
    assert scan_run(scan.id) == "failed"
    row = load_scan(scan.id)
    assert "слишком велик" in row.status_detail
    assert (fake_host.home / "oreshnik" / f"scan-{scan.id}" / "strix_runs" / "fake-run" / "run.json").exists()  # nothing lost
    assert f"oreshnik/scan-{scan.id}" in "\n".join(client.get(f"/api/scans/{scan.id}/log?limit=300", headers=operator).json()["lines"])


# ── live mirroring ──────────────────────────────────────────────────────

def test_cost_and_the_agent_feed_are_live_while_the_scan_runs(client, operator, remote, fake_host):
    scan = new_scan(client, operator, remote, "live_agents")
    t = run_in_thread(scan.id)
    costs, texts = [], set()

    def watch():
        row = load_scan(scan.id)
        if row.status == ScanStatus.running:
            if row.cost and row.cost not in costs:
                costs.append(row.cost)
            if activity_path(scan.id).exists():
                texts.update(json.loads(l).get("text") for l in activity_path(scan.id).read_text().splitlines() if l.strip())
        return len(costs) >= 2 and any(x and x.startswith("live step") for x in texts) and row.status == ScanStatus.running

    assert wait_until(watch, timeout=30), f"nothing live: costs={costs} texts={texts}"
    t.join(timeout=40)
    done = load_scan(scan.id)
    assert done.status == ScanStatus.succeeded and done.cost == 0.3 and costs == sorted(costs) and costs[-1] < 0.3 + 1e-9
    events = [json.loads(l) for l in activity_path(scan.id).read_text().splitlines()]
    assert [e["seq"] for e in events] == list(range(len(events)))  # contiguous
    steps = [e["text"] for e in events if (e.get("text") or "").startswith("live step")]
    assert steps == [f"live step {i}" for i in range(1, 7)]  # every row of the live WAL database, once, in order


# ── continuing ──────────────────────────────────────────────────────────

def test_a_scan_on_a_host_continues_in_place(client, operator, remote, fake_host):
    scan = api_create(client, operator, remote, instruction="ok_findings").json()
    out = client.post(f"/api/scans/{scan['id']}/continue", headers=operator, json={"instruction": "check the admin panel"})
    assert out.status_code == 200, out.text
    again = out.json()
    assert again["id"] == scan["id"] and again["status"] == "vulns_found" and again["findings_total"] == 4
    assert again["cost"] == 0.84 and again["resume_count"] == 1
    log = "\n".join(client.get(f"/api/scans/{scan['id']}/log?limit=500", headers=operator).json()["lines"])
    assert "Прогон fake-run передан на хост" in log and "--resume=fake-run" in log and "Resumed fake-run: 4 findings in total" in log
    assert "--target" not in log.split("--resume=fake-run")[1].splitlines()[0]
    assert not (fake_host.home / "oreshnik" / f"scan-{scan['id']}").exists()


def test_a_scan_on_a_host_can_be_continued_as_a_new_scan(client, operator, remote):
    scan = api_create(client, operator, remote, instruction="ok_findings").json()
    child = client.post(f"/api/scans/{scan['id']}/continue", headers=operator, json={"instruction": "x", "as_new_scan": True})
    assert child.status_code == 201 and child.json()["id"] != scan["id"] and child.json()["findings_total"] == 4
    assert client.get(f"/api/scans/{scan['id']}", headers=operator).json()["findings_total"] == 3  # the original is untouched


def test_a_startup_error_on_a_continued_remote_run_is_not_taken_for_findings(client, operator, remote):
    """The uploaded run holds the findings of the first run; exit code 2 without Strix having touched run.json is an error.
    (The run comes back from the host as a new file, so this must be judged by content, not by file times.)"""
    scan = api_create(client, operator, remote, instruction="ok_findings").json()
    out = client.post(f"/api/scans/{scan['id']}/continue", headers=operator, json={"instruction": "startup_error please"}).json()
    assert out["status"] == "failed" and out["exit_code"] == 2 and out["findings_total"] == 3 and out["cost"] == 0.42


def test_a_run_name_from_the_panel_side_cannot_escape_the_remote_directory(tmp_path, fake_host):
    spec = ScanSpec(scan_id=1, targets=["x"], scan_mode="quick", instruction=None, max_budget=None, work_dir=tmp_path,
                    run_label="l", resume_run="../../etc", ssh=fake_host.target())
    assert SshRunner().run(spec, lambda _l: None, lambda: False).error == "Недопустимое имя прогона"


# ── creating scans on hosts ─────────────────────────────────────────────

def test_a_scan_cannot_be_created_on_a_host_that_is_not_confirmed(client, operator, fake_host):
    runner = make_ssh_runner(fake_host, trusted=False)
    profile = make_profile(me(client, operator))
    r = client.post("/api/scans", headers=operator, json={"targets": ["https://example.com"], "runner_id": runner.id, "llm_profile_id": profile.id})
    assert r.status_code == 422 and "не подтверждён" in r.json()["detail"]


def test_local_paths_are_refused_on_a_host(client, admin, fake_host):
    runner = make_ssh_runner(fake_host, trusted=True)
    profile = make_profile(me(client, admin))
    r = client.post("/api/scans", headers=admin, json={"targets": ["/srv/app"], "runner_id": runner.id, "llm_profile_id": profile.id})
    assert r.status_code == 422 and "SSH-раннере" in r.json()["detail"]


def test_a_schedule_can_run_on_a_host(client, operator, remote):
    from app.services.schedule import fire_scheduled
    from tests.conftest import make_schedule

    runner, profile = remote
    schedule = make_schedule(me(client, operator), runner.id, profile_id=profile.id, max_budget=5.0)
    with SessionLocal() as db:
        assert fire_scheduled(db, schedule.id, lambda sid: scan_run(sid), datetime.now(timezone.utc)) == "started"
    scan = client.get("/api/scans", headers=operator).json()["items"][0]
    assert scan["schedule_id"] == schedule.id and scan["runner_kind"] == "ssh" and scan["status"] in ("succeeded", "vulns_found")


# ── what is left behind when the worker is lost ─────────────────────────

def test_the_reaper_stops_a_scan_that_is_still_running_on_the_host(client, operator, remote, fake_host):
    scan = new_scan(client, operator, remote, status=ScanStatus.running)
    with SessionLocal() as db:
        row = db.get(Scan, scan.id)
        row.started_at = row.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
    # a Strix that the vanished worker had started on the host
    from app.services.ssh import SshConnection
    with SshConnection(fake_host.target()) as conn:
        conn.run(f"mkdir -p $HOME/oreshnik/scan-{scan.id} && cd $HOME/oreshnik/scan-{scan.id} && (setsid sh -c 'echo $$ > .pid; exec sleep 300' >/dev/null 2>&1 </dev/null &)")
    (fake_host.home / "containers.txt").write_text("abc123\n")
    assert wait_until(lambda: processes_under(fake_host.home))
    assert reap_stale_scans() == 1
    assert load_scan(scan.id).status == ScanStatus.failed
    assert wait_until(lambda: processes_under(fake_host.home) == [], timeout=15), "the reaper left Strix running on the host"
    assert any(c.startswith("docker rm -f abc123") for c in fake_host.docker_calls())


def test_cleanup_of_an_unreachable_host_never_raises(fake_host):
    target = fake_host.target()
    fake_host.stop()
    assert cleanup_remote(target, 1, "oreshnik-scan-1") is False


def test_leftovers_of_an_earlier_attempt_on_the_host_do_not_leak_into_the_new_run(client, operator, remote, fake_host):
    scan = new_scan(client, operator, remote)
    stale = fake_host.home / "oreshnik" / f"scan-{scan.id}" / "strix_runs" / "old-run"
    stale.mkdir(parents=True)
    (stale / "run.json").write_text('{"status": "completed", "llm_usage": {"cost": 99}}')
    (stale.parent.parent / "junk.txt").write_text("from an earlier attempt")
    assert scan_run(scan.id) == "vulns_found"
    assert sorted(p.name for p in (scan_dir(scan.id) / "strix_runs").iterdir()) == ["fake-run"]
    assert load_scan(scan.id).cost == 0.42 and load_scan(scan.id).run_name == "fake-run"
