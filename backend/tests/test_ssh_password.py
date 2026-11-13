"""Logging in to a host with a password (alone, or next to a key)."""
import asyncssh
import pytest

from app.core.crypto import decrypt
from app.core.db import SessionLocal
from app.models import Runner, RunnerStatus, ScanStatus
from app.services.ssh import SshConnection, SshError, SshErrorKind, SshTarget
from app.services.ssh_runner import check_ssh_runner
from app.worker.tasks import check_ssh_runners
from tests.conftest import load_runner, make_profile, make_scan, make_ssh_runner
from tests.ssh_server import FakeHost

PW = "correct horse battery staple"


@pytest.fixture
def pw_host(tmp_path):
    """A host that allows both key and password login."""
    host = FakeHost(tmp_path / "pw-home", password=PW).start()
    host.install_tools()
    yield host
    host.stop()


def check(client, admin, rid):
    r = client.post(f"/api/runners/{rid}/check", headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


def create(client, admin, host, **extra):
    body = {"name": "remote", "kind": "ssh", "ssh_host": "127.0.0.1", "ssh_port": host.port, "ssh_user": host.user, **extra}
    return client.post("/api/runners", headers=admin, json=body)


def other_key_pem() -> str:
    return asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()


# ── the connection layer ────────────────────────────────────────────────

def test_a_password_alone_is_enough(pw_host):
    with SshConnection(pw_host.password_target()) as conn:
        assert conn.run("echo ok").stdout == "ok\n"
    assert pw_host.password_logins == 1 and pw_host.attempts == 0  # no key was involved


def test_a_wrong_password_is_an_auth_error_that_does_not_repeat_itself_and_does_not_leak(pw_host):
    wrong = "not-the-password-" + "x" * 6
    with pytest.raises(SshError) as exc:
        SshConnection(pw_host.password_target(wrong)).open()
    assert exc.value.kind == SshErrorKind.auth and "пароль" in exc.value.message
    assert wrong not in exc.value.message + repr(exc.value) + repr(pw_host.password_target(wrong))
    assert pw_host.password_attempts == 1  # one attempt per connection, not a salvo


def test_the_key_goes_first_and_the_password_is_not_sent_when_the_key_works(pw_host):
    target = SshTarget("127.0.0.1", pw_host.port, pw_host.user, pw_host.client_key_pem, pw_host.host_key_line, "an-unused-password")
    with SshConnection(target) as conn:
        assert conn.run("echo ok").stdout == "ok\n"
    assert pw_host.password_attempts == 0


def test_a_rejected_key_falls_back_to_the_password(pw_host):
    target = SshTarget("127.0.0.1", pw_host.port, pw_host.user, other_key_pem(), pw_host.host_key_line, PW)
    with SshConnection(target) as conn:
        assert conn.run("echo ok").stdout == "ok\n"
    assert pw_host.attempts >= 1 and pw_host.password_logins == 1


def test_neither_a_key_nor_a_password_is_refused_without_touching_the_host(pw_host):
    with pytest.raises(SshError) as exc:
        SshConnection(SshTarget("127.0.0.1", pw_host.port, pw_host.user, None, pw_host.host_key_line, None)).open()
    assert exc.value.kind == SshErrorKind.auth and "ни ключ, ни пароль" in exc.value.message
    assert pw_host.connections == []


def test_a_host_that_does_not_allow_passwords_says_so(tmp_path):
    host = FakeHost(tmp_path / "keyonly").start()  # no password support
    try:
        with pytest.raises(SshError) as exc:
            SshConnection(host.password_target("whatever-it-is")).open()
        assert exc.value.kind == SshErrorKind.auth and "разрешён вход по паролю" in exc.value.message
        assert host.password_attempts == 0
    finally:
        host.stop()


def test_the_password_never_goes_to_a_host_whose_key_is_not_confirmed_or_changed(pw_host):
    with pytest.raises(SshError) as exc:
        SshConnection(pw_host.password_target(trusted=False)).open()
    assert exc.value.kind == SshErrorKind.host_key_untrusted
    old = pw_host.host_key_line
    pw_host.rotate_host_key()
    with pytest.raises(SshError) as exc:
        SshConnection(SshTarget("127.0.0.1", pw_host.port, pw_host.user, None, old, PW)).open()
    assert exc.value.kind == SshErrorKind.host_key_changed
    assert pw_host.password_attempts == 0  # a man in the middle would have been handed the password in the clear


def test_the_password_is_not_part_of_any_command_or_input(pw_host):
    with SshConnection(pw_host.password_target()) as conn:
        conn.run("echo hello")
        conn.start("sh -s", input="echo more\n")
    assert all(PW not in c for c in pw_host.commands) and all(PW not in i for i in pw_host.inputs)


def test_the_probe_works_over_a_password(client, pw_host):
    runner = make_ssh_runner(pw_host, trusted=True, password=PW, with_key=False)
    result = check_ssh_runner(load_runner(runner.id))
    assert result.status == RunnerStatus.online and "Strix" in result.detail


# ── the API ─────────────────────────────────────────────────────────────

def test_a_runner_can_be_created_with_a_password_alone_and_it_is_stored_encrypted(client, admin, operator, pw_host):
    r = create(client, admin, pw_host, ssh_password=PW)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["has_ssh_password"] is True and out["has_ssh_key"] is False
    row = load_runner(out["id"])
    assert row.ssh_password_enc != PW and decrypt(row.ssh_password_enc) == PW and row.ssh_key_enc is None
    everything = client.get("/api/runners", headers=admin).text + client.get("/api/runners", headers=operator).text + r.text
    assert PW not in everything
    seen = next(x for x in client.get("/api/runners", headers=operator).json() if x["id"] == out["id"])
    assert seen["has_ssh_password"] is False and seen["ssh_host"] is None  # operators see no connection details


def test_a_runner_needs_some_way_to_log_in_and_may_have_both(client, admin, pw_host):
    assert create(client, admin, pw_host).status_code == 422
    assert create(client, admin, pw_host, ssh_password="").status_code == 422  # an empty password is no password
    both = create(client, admin, pw_host, name="both", ssh_password=PW, ssh_private_key=pw_host.client_key_pem)
    assert both.status_code == 201 and both.json()["has_ssh_key"] and both.json()["has_ssh_password"]
    assert client.post("/api/runners", headers=admin, json={"name": "m", "kind": "mock", "ssh_password": PW}).json()["has_ssh_password"] is False


@pytest.mark.parametrize("password,ok", [("  spaces matter  ", True), ("pä$$wörd✓", True), ("a" * 1024, True), ("a" * 1025, False), ("nul\x00byte", False)])
def test_password_validation(client, admin, pw_host, password, ok):
    r = create(client, admin, pw_host, ssh_password=password)
    assert (r.status_code == 201) is ok
    if ok:
        assert decrypt(load_runner(r.json()["id"]).ssh_password_enc) == password  # kept exactly as typed


def test_the_first_check_with_a_password_still_only_looks_at_the_host_key(client, admin, pw_host):
    rid = create(client, admin, pw_host, ssh_password=PW).json()["id"]
    out = check(client, admin, rid)
    assert out["ssh_host_key_pending_fingerprint"] and out["status"] == "offline"
    assert pw_host.password_attempts == 0 and pw_host.attempts == 0  # nothing was offered before the key was confirmed
    trusted = client.post(f"/api/runners/{rid}/trust-host-key", headers=admin, json={"fingerprint": out["ssh_host_key_pending_fingerprint"]}).json()
    assert trusted["status"] == "online" and pw_host.password_logins >= 1


def test_a_wrong_password_is_an_error_and_is_not_retried_by_the_periodic_check(client, admin, pw_host):
    runner = make_ssh_runner(pw_host, trusted=True, password="wrong-password-1", with_key=False)
    out = check(client, admin, runner.id)
    assert out["status"] == "error" and "пароль" in out["status_detail"] and "wrong-password-1" not in out["status_detail"]
    tried = pw_host.password_attempts
    assert tried == 1
    check_ssh_runners()
    check_ssh_runners()
    assert pw_host.password_attempts == tried  # a repeated wrong password is what gets a client locked out by fail2ban
    # fixing the password brings it back into the rotation
    r = client.patch(f"/api/runners/{runner.id}", headers=admin, json={"ssh_password": PW})
    assert r.status_code == 200 and r.json()["status"] == "offline" and "Учётные данные изменены" in r.json()["status_detail"]
    assert r.json()["ssh_host_key_fingerprint"] is not None  # the host did not change: its key stays confirmed
    check_ssh_runners()
    assert load_runner(runner.id).status == RunnerStatus.online


def test_credentials_can_be_replaced_and_removed_but_never_all_of_them(client, admin, pw_host):
    rid = create(client, admin, pw_host, name="both", ssh_password=PW, ssh_private_key=pw_host.client_key_pem).json()["id"]
    r = client.patch(f"/api/runners/{rid}", headers=admin, json={"clear_ssh_key": True})
    assert r.status_code == 200 and r.json()["has_ssh_key"] is False and r.json()["has_ssh_password"] is True
    r = client.patch(f"/api/runners/{rid}", headers=admin, json={"clear_ssh_password": True})
    assert r.status_code == 422 and "хотя бы один способ входа" in r.json()["detail"]
    row = load_runner(rid)
    assert row.ssh_password_enc is not None  # nothing was removed by the refused request
    r = client.patch(f"/api/runners/{rid}", headers=admin, json={"clear_ssh_password": True, "ssh_private_key": pw_host.client_key_pem})
    assert r.status_code == 200 and r.json()["has_ssh_key"] and not r.json()["has_ssh_password"]  # swapping is one step
    r = client.patch(f"/api/runners/{rid}", headers=admin, json={"ssh_password": "second-password"})
    assert decrypt(load_runner(rid).ssh_password_enc) == "second-password"
    assert client.patch(f"/api/runners/{rid}", headers=admin, json={"name": "renamed"}).json()["has_ssh_password"] is True  # untouched by other edits


# ── scans over a password ───────────────────────────────────────────────

def test_a_scan_runs_over_a_password_and_the_password_stays_out_of_everything(client, operator, pw_host):
    runner = make_ssh_runner(pw_host, trusted=True, password=PW, with_key=False)
    op = client.get("/api/auth/me", headers=operator).json()["id"]
    profile = make_profile(op, key="sk-live-key-9999999999")
    r = client.post("/api/scans", headers=operator, json={"targets": ["https://example.com"], "runner_id": runner.id,
                                                          "llm_profile_id": profile.id, "instruction": "ok_findings"})
    assert r.status_code == 201, r.text
    scan = r.json()
    assert scan["status"] == "vulns_found" and scan["findings_total"] == 3 and scan["runner_kind"] == "ssh"
    log = "\n".join(client.get(f"/api/scans/{scan['id']}/log?limit=300", headers=operator).json()["lines"])
    assert PW not in log and PW not in str(client.get(f"/api/scans/{scan['id']}", headers=operator).json())
    assert all(PW not in c for c in pw_host.commands) and all(PW not in i for i in pw_host.inputs)
    assert not any(PW in f.read_text(errors="replace") for f in pw_host.home.rglob("*") if f.is_file() and f.stat().st_size < 1_000_000 and not f.is_symlink() and "sysbin" not in f.parts)
    assert pw_host.password_logins >= 1 and pw_host.attempts == 0


def test_a_runner_with_no_way_to_log_in_cannot_get_scans(client, operator, pw_host):
    runner = make_ssh_runner(pw_host, trusted=True, with_key=False)  # neither key nor password
    profile = make_profile(client.get("/api/auth/me", headers=operator).json()["id"])
    r = client.post("/api/scans", headers=operator, json={"targets": ["https://example.com"], "runner_id": runner.id, "llm_profile_id": profile.id})
    assert r.status_code == 422 and "ключ или пароль" in r.json()["detail"]
