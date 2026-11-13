"""Installing Docker and Strix on a remote host."""
import base64
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.core.db import SessionLocal
from app.models import Runner, RunnerStatus
from app.services import provision
from app.services.provision import InstallCommands, PRELUDE, ProvisionError, default_commands, plan
from app.services.ssh_runner import RemoteState, describe_state, parse_probe
from app.worker.tasks import runner_provision
from tests.conftest import load_runner, make_profile, make_ssh_runner
from tests.ssh_server import DOCKER_STUB, FAKE_STRIX


def state(**kw) -> RemoteState:
    """A bare Ubuntu host with root and curl, unless said otherwise."""
    base = dict(os_id="ubuntu", os_version="24.04", user="root", uid=0, can_root=True, curl=True)
    return RemoteState(**{**base, **kw})


READY = dict(docker_cli=True, docker_ok=True, strix_path="/h/.local/bin/strix", python3=True, setsid=True, uv=True)


def keys(steps):
    return [s.key for s in steps]


CMDS = default_commands()


# ── the plan ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("overrides,expected", [
    (READY, []),
    ({**READY, "strix_path": "", "uv": True}, ["strix", "image"]),
    ({**READY, "strix_path": "", "uv": False}, ["uv", "strix", "image"]),
    ({**READY, "docker_cli": False, "docker_ok": False}, ["docker", "image"]),
    ({**READY, "docker_ok": False}, ["docker-start", "docker-access", "image"]),
    ({**READY, "python3": False}, ["python3"]),
    ({**READY, "setsid": False}, ["setsid"]),
    ({}, ["docker", "python3", "setsid", "uv", "strix", "image"]),
])
def test_only_what_is_missing_is_installed(overrides, expected):
    assert keys(plan(state(**overrides), CMDS)) == expected


def test_system_packages_need_root_or_passwordless_sudo():
    with pytest.raises(ProvisionError) as exc:
        plan(state(can_root=False, user="deploy", **{**READY, "docker_cli": False, "docker_ok": False}), CMDS)
    assert "root или sudo без пароля" in str(exc.value) and "deploy" in str(exc.value) and "Docker" in str(exc.value)


def test_a_user_without_root_can_still_get_strix():
    steps = plan(state(can_root=False, user="deploy", **{**READY, "strix_path": "", "uv": False}), CMDS)
    assert keys(steps) == ["uv", "strix", "image"] and not any(s.needs_root for s in steps)


def test_other_distributions_are_not_touched():
    with pytest.raises(ProvisionError) as exc:
        plan(state(os_id="fedora", **{**READY, "docker_cli": False, "docker_ok": False}), CMDS)
    assert "Debian и Ubuntu" in str(exc.value) and "fedora" in str(exc.value)
    # ...but what needs no system package works anywhere
    assert keys(plan(state(os_id="fedora", **{**READY, "strix_path": ""}), CMDS)) == ["strix", "image"]  # no system package needed


def test_curl_is_needed_for_the_user_space_installers():
    with pytest.raises(ProvisionError) as exc:
        plan(state(can_root=False, curl=False, **{**READY, "strix_path": "", "uv": False}), CMDS)
    assert "curl" in str(exc.value)


def test_the_default_installers_are_pinned_and_well_formed():
    c = default_commands(version="1.7.0", image="ghcr.io/usestrix/strix-sandbox:1.3.0")
    assert "strix-agent==1.7.0" in c.strix and "strix-sandbox:1.3.0" in c.pull_image
    assert "get.docker.com" in c.docker and "astral.sh/uv/install.sh" in c.uv
    for name in ("docker", "start_docker", "docker_access", "python3", "setsid", "uv", "strix", "pull_image"):
        checked = subprocess.run(["sh", "-n"], input=PRELUDE + getattr(c, name), capture_output=True, text=True)
        assert checked.returncode == 0, f"{name}: {checked.stderr}"
    assert "$SUDO" in c.docker and "curl -fsSL https://get.docker.com -o" in c.docker  # downloaded first, then run: never `curl | sudo sh`


def test_a_hostile_version_or_image_cannot_break_out_of_the_command(tmp_path):
    """Run the real command text with stubs for uv/strix/docker that print the arguments they receive."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("uv", "strix", "docker"):
        (bindir / tool).write_text('#!/bin/sh\nfor a in "$@"; do printf "ARG<%s>\\n" "$a"; done\n')
        (bindir / tool).chmod(0o755)
    pwned_version, pwned_image = tmp_path / "pwned-version", tmp_path / "pwned-image"  # sentinels: unique per run, never stale
    evil_version = f"1.0'; touch {pwned_version}; '"
    evil_image = f"x; touch {pwned_image}"
    c = default_commands(version=evil_version, image=evil_image)
    out = subprocess.run(["sh", "-s"], input=c.strix + c.pull_image, capture_output=True, text=True,
                         env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)}).stdout
    assert f"ARG<strix-agent=={evil_version}>" in out and f"ARG<{evil_image}>" in out  # each arrived as ONE argument
    assert not pwned_version.exists() and not pwned_image.exists()


def test_the_probe_output_is_understood():
    out = "os=ubuntu\nos_version=24.04\nuser=scan\nuid=1000\ndocker_cli=1\ndocker_ok=0\nstrix=\npython3=/usr/bin/python3\nsetsid=/usr/bin/setsid\ncurl=/usr/bin/curl\nuv=\ncan_root=0\nfree_mb=51200\nnoise without equals\n"
    s = parse_probe(out)
    assert (s.os_id, s.user, s.uid, s.docker_cli, s.docker_ok, s.strix_path, s.python3, s.can_root, s.free_mb) == \
           ("ubuntu", "scan", 1000, True, False, "", True, False, 51200)
    assert s.missing() == ["доступ к Docker", "Strix"]
    assert parse_probe("").missing()  # an empty answer means "nothing is there", not a crash


# ── running it, against a real SSH server and harmless installers ───────

def harmless_commands() -> InstallCommands:
    """Install into the fake host's home instead of the real machine."""
    strix_b64 = base64.b64encode(FAKE_STRIX.read_bytes()).decode()
    docker_b64 = base64.b64encode(DOCKER_STUB.encode()).decode()
    put = lambda b64, name: f'mkdir -p "$HOME/.local/bin"\necho {b64} | base64 -d > "$HOME/.local/bin/{name}"\nchmod +x "$HOME/.local/bin/{name}"\n'
    return InstallCommands(
        docker="echo 'installing docker'\n" + put(docker_b64, "docker"),
        start_docker="echo 'starting docker'\nrm -f \"$HOME/docker-broken\"\n",
        docker_access="echo 'adding to the docker group'\nrm -f \"$HOME/docker-broken\"\n",
        python3="echo 'installing python3'\n",
        setsid="echo 'installing setsid'\n",
        uv="echo 'installing uv'\nmkdir -p \"$HOME/.local/bin\"\nprintf '#!/bin/sh\\n' > \"$HOME/.local/bin/uv\"\nchmod +x \"$HOME/.local/bin/uv\"\n",
        strix="echo 'installing strix'\n" + put(strix_b64, "strix") + "strix --version || true\n",
        pull_image="echo 'pulling the image'\ntouch \"$HOME/image-pulled\"\n",
    )


@pytest.fixture
def bare_host(fake_host, monkeypatch):
    """A fake host with nothing installed, and installers that only touch its home directory."""
    shutil.rmtree(fake_host.home / ".local", ignore_errors=True)
    monkeypatch.setattr(provision, "default_commands", harmless_commands)
    return fake_host


def api_provision(client, admin, rid):
    return client.post(f"/api/runners/{rid}/provision", headers=admin)


def test_a_bare_host_gets_everything_and_comes_online(client, admin, bare_host):
    runner = make_ssh_runner(bare_host, trusted=True)
    checked = client.post(f"/api/runners/{runner.id}/check", headers=admin).json()
    assert checked["status"] == "offline" and "Docker" in checked["status_detail"] and "Strix" in checked["status_detail"]
    assert "Установить" in checked["status_detail"]  # it says what to do about it
    out = api_provision(client, admin, runner.id)
    assert out.status_code == 200, out.text
    assert out.json()["status"] == "online" and "Strix" in out.json()["status_detail"]
    log = client.get(f"/api/runners/{runner.id}/provision-log", headers=admin).json()
    assert log["running"] is False
    for line in ("▶ Шаг 1/", "installing docker", "installing uv", "installing strix", "pulling the image", "✓ Готово"):
        assert line in log["log"], line
    assert (bare_host.home / "image-pulled").exists() and (bare_host.home / "oreshnik").is_dir()
    assert load_runner(runner.id).provision_started_at is None
    assert next(r for r in client.get("/api/runners", headers=admin).json() if r["id"] == runner.id)["has_provision_log"] is True


def test_the_steps_run_in_order_and_each_is_reported(client, admin, bare_host):
    runner = make_ssh_runner(bare_host, trusted=True)
    api_provision(client, admin, runner.id)
    text = client.get(f"/api/runners/{runner.id}/provision-log", headers=admin).json()["log"]
    order = [text.index(marker) for marker in ("installing docker", "installing uv", "installing strix", "pulling the image")]
    assert order == sorted(order)
    assert text.count("▶ Шаг") == text.count("✓ Готово") == 4  # docker, uv, strix, image: every step began and ended


def test_a_host_that_is_ready_needs_nothing(client, admin, fake_host, monkeypatch):
    monkeypatch.setattr(provision, "default_commands", harmless_commands)
    runner = make_ssh_runner(fake_host, trusted=True)
    out = api_provision(client, admin, runner.id).json()
    assert out["status"] == "online"
    log = client.get(f"/api/runners/{runner.id}/provision-log", headers=admin).json()["log"]
    assert "делать нечего" in log and "installing" not in log and "▶" not in log
    assert not any("installing" in i for i in fake_host.inputs)  # not a single installer was even sent to the host


def test_a_failing_step_stops_the_installation_and_says_which(client, admin, bare_host, monkeypatch):
    broken = harmless_commands().__class__(**{**harmless_commands().__dict__, "strix": "echo 'installing strix'\necho 'no matching distribution' >&2\nexit 3\n"})
    monkeypatch.setattr(provision, "default_commands", lambda: broken)
    runner = make_ssh_runner(bare_host, trusted=True)
    out = api_provision(client, admin, runner.id).json()
    assert out["status"] == "offline" and "Установка Strix" in out["status_detail"] and "журнале" in out["status_detail"]
    log = client.get(f"/api/runners/{runner.id}/provision-log", headers=admin).json()["log"]
    assert "no matching distribution" in log and "✗ Шаг завершился с кодом 3" in log
    assert "pulling the image" not in log and not (bare_host.home / "image-pulled").exists()  # nothing after the failure ran


def test_docker_that_exists_but_cannot_be_used_is_fixed(client, admin, fake_host, monkeypatch):
    monkeypatch.setattr(provision, "default_commands", harmless_commands)
    (fake_host.home / "docker-broken").write_text("1")
    runner = make_ssh_runner(fake_host, trusted=True)
    out = api_provision(client, admin, runner.id).json()
    log = client.get(f"/api/runners/{runner.id}/provision-log", headers=admin).json()["log"]
    assert out["status"] == "online" and "starting docker" in log and "adding to the docker group" in log and "installing strix" not in log


def test_an_unreachable_host_is_reported_in_the_log(client, admin, bare_host):
    runner = make_ssh_runner(bare_host, trusted=True)
    bare_host.stop()
    out = api_provision(client, admin, runner.id).json()
    assert out["status"] == "offline"
    assert "Не удалось подключиться" in client.get(f"/api/runners/{runner.id}/provision-log", headers=admin).json()["log"]


def test_missing_privileges_are_explained_before_anything_runs(client, admin, bare_host, monkeypatch):
    monkeypatch.setattr(provision, "plan", lambda *_a, **_k: (_ for _ in ()).throw(ProvisionError("Для установки нужны права root")))
    runner = make_ssh_runner(bare_host, trusted=True)
    out = api_provision(client, admin, runner.id).json()
    assert out["status"] == "offline" and "права root" in out["status_detail"]
    assert not any("installing" in i for i in bare_host.inputs)


def test_the_log_grows_while_the_installation_runs(client, admin, bare_host, monkeypatch):
    slow = harmless_commands().__class__(**{**harmless_commands().__dict__, "docker": "echo 'docker step started'\nsleep 3\necho 'docker step done'\n" + harmless_commands().docker})
    monkeypatch.setattr(provision, "default_commands", lambda: slow)
    monkeypatch.setattr(provision.LogBuffer.__init__, "__defaults__", (0.0,), raising=False)
    runner = make_ssh_runner(bare_host, trusted=True)
    with SessionLocal() as db:  # what the endpoint does before handing over to the worker
        row = db.get(Runner, runner.id)
        row.status, row.provision_started_at, row.provision_log = RunnerStatus.provisioning, datetime.now(timezone.utc), ""
        db.commit()
    t = threading.Thread(target=lambda: runner_provision(runner.id))
    t.start()
    seen = None
    deadline = time.time() + 20
    while time.time() < deadline:
        body = client.get(f"/api/runners/{runner.id}/provision-log", headers=admin).json()
        if "docker step started" in body["log"] and "docker step done" not in body["log"]:
            seen = body
            break
        time.sleep(0.1)
    t.join(timeout=60)
    assert seen is not None and seen["running"] is True, "the log was not available while the step was still running"
    assert client.get(f"/api/runners/{runner.id}/provision-log", headers=admin).json()["running"] is False


def test_output_is_cleaned_and_capped(client, admin, bare_host, monkeypatch):
    base = harmless_commands()
    noisy = base.__class__(**{**base.__dict__,
        "uv": base.uv + "printf '\\033[31mred\\033[0m text\\n'\nhead -c 300000 /dev/zero | tr '\\0' 'x' | fold -w 100\n"})
    monkeypatch.setattr(provision, "default_commands", lambda: noisy)
    runner = make_ssh_runner(bare_host, trusted=True)
    api_provision(client, admin, runner.id)
    full = load_runner(runner.id).provision_log
    assert "\x1b" not in full and "red text" in full  # escape sequences are stripped
    assert len(full) <= provision.LOG_LIMIT + 100 and "[журнал обрезан]" in full  # and the log cannot grow without bound
    served = client.get(f"/api/runners/{runner.id}/provision-log", headers=admin).json()["log"]
    assert len(served) <= 100_000 and served.endswith("[журнал обрезан]\n")  # the API serves the tail


def test_a_crash_in_the_installation_does_not_leave_the_runner_installing(client, admin, bare_host, monkeypatch):
    monkeypatch.setattr(provision, "run_provision", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    runner = make_ssh_runner(bare_host, trusted=True)
    out = api_provision(client, admin, runner.id).json()
    assert out["status"] == "error" and "внутренняя ошибка" in out["status_detail"]
    assert load_runner(runner.id).provision_started_at is None


# ── what the API allows ─────────────────────────────────────────────────

def test_only_a_confirmed_host_is_provisioned(client, admin, bare_host):
    runner = make_ssh_runner(bare_host, trusted=False)
    r = api_provision(client, admin, runner.id)
    assert r.status_code == 409 and "ключ хоста" in r.json()["detail"].lower()
    assert bare_host.attempts == 0 and bare_host.connections == []


def test_roles_and_kinds(client, admin, operator, viewer, bare_host):
    from tests.conftest import make_runner
    runner = make_ssh_runner(bare_host, trusted=True)
    for headers in (operator, viewer):
        assert client.post(f"/api/runners/{runner.id}/provision", headers=headers).status_code == 403
        assert client.get(f"/api/runners/{runner.id}/provision-log", headers=headers).status_code == 403
    mock = make_runner()
    assert client.post(f"/api/runners/{mock.id}/provision", headers=admin).status_code == 422
    assert client.post("/api/runners/9999/provision", headers=admin).status_code == 404


def test_one_installation_at_a_time_and_nothing_else_changes_meanwhile(client, admin, operator, bare_host):
    runner = make_ssh_runner(bare_host, trusted=True, status=RunnerStatus.provisioning)
    with SessionLocal() as db:
        db.get(Runner, runner.id).provision_started_at = datetime.now(timezone.utc)
        db.commit()
    assert api_provision(client, admin, runner.id).status_code == 409
    assert client.patch(f"/api/runners/{runner.id}", headers=admin, json={"ssh_host": "10.0.0.9"}).status_code == 409
    assert client.post(f"/api/runners/{runner.id}/forget-host-key", headers=admin).status_code == 409
    profile = make_profile(client.get("/api/auth/me", headers=operator).json()["id"])
    r = client.post("/api/scans", headers=operator, json={"targets": ["https://example.com"], "runner_id": runner.id, "llm_profile_id": profile.id})
    assert r.status_code == 409 and "идёт установка" in r.json()["detail"]
    assert bare_host.connections == []  # and the periodic check does not poke at it either


def test_an_installation_that_was_lost_with_its_worker_does_not_block_the_runner_forever(client, admin, bare_host, monkeypatch):
    monkeypatch.setattr(provision, "default_commands", harmless_commands)
    runner = make_ssh_runner(bare_host, trusted=True, status=RunnerStatus.provisioning)
    with SessionLocal() as db:
        db.get(Runner, runner.id).provision_started_at = datetime.now(timezone.utc) - timedelta(hours=3)
        db.commit()
    out = client.post(f"/api/runners/{runner.id}/check", headers=admin).json()
    assert out["status"] == "offline" and "Strix" in out["status_detail"]  # checked normally again
    assert api_provision(client, admin, runner.id).status_code == 200


def test_the_hint_says_what_can_be_done_with_the_rights_the_user_has():
    ready = dict(READY)
    only_strix = describe_state(state(can_root=False, user="deploy", **{**ready, "strix_path": ""}))
    assert only_strix[0] == RunnerStatus.offline and "нет Strix" in only_strix[1] and "нажмите «Установить»" in only_strix[1]
    no_docker = describe_state(state(can_root=False, user="deploy", **{**ready, "docker_cli": False, "docker_ok": False}))
    assert "root или sudo без пароля" in no_docker[1] and "«Установить»" not in no_docker[1]
    no_access = describe_state(state(can_root=False, user="deploy", **{**ready, "docker_ok": False}))
    assert "группу docker" in no_access[1]
    assert "«Установить»" in describe_state(state(can_root=True, **{**ready, "docker_cli": False, "docker_ok": False}))[1]
    assert describe_state(state(**ready))[0] == RunnerStatus.online and describe_state(state(strix_version="strix 1.7.0", **ready))[1].startswith("Strix 1.7.0 + Docker")
