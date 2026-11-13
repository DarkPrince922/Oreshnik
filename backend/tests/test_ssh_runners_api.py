"""SSH runners in the panel: first contact, host key confirmation, health checks."""
import pytest

from app.core.crypto import decrypt
from app.models import RunnerKind, RunnerStatus
from app.services.ssh import fingerprint
from app.worker.tasks import check_ssh_runners
from tests.conftest import auth_headers, load_runner, make_runner, make_ssh_runner, make_user


def check(client, admin, rid):
    r = client.post(f"/api/runners/{rid}/check", headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


def trust(client, admin, rid, fp):
    return client.post(f"/api/runners/{rid}/trust-host-key", headers=admin, json={"fingerprint": fp})


def create(client, admin, host, **extra):
    body = {"name": "remote", "kind": "ssh", "ssh_host": "127.0.0.1", "ssh_port": host.port, "ssh_user": host.user,
            "ssh_private_key": host.client_key_pem, **extra}
    return client.post("/api/runners", headers=admin, json=body)


# ── first contact ───────────────────────────────────────────────────────

def test_the_first_check_shows_the_fingerprint_and_sends_no_credentials(client, admin, fake_host):
    r = create(client, admin, fake_host)
    assert r.status_code == 201 and r.json()["status"] == "offline" and r.json()["ssh_host_key_fingerprint"] is None
    out = check(client, admin, r.json()["id"])
    fp = fingerprint(fake_host.host_key_line)
    assert out["status"] == "offline" and out["ssh_host_key_pending_fingerprint"] == fp and out["ssh_host_key_fingerprint"] is None
    assert fp in out["status_detail"] and "ssh-keygen" in out["status_detail"]
    assert fake_host.attempts == 0  # the panel only looked at the key
    assert load_runner(out["id"]).ssh_host_key is None  # and trusts nothing yet


def test_confirming_the_right_fingerprint_pins_the_key_and_checks_the_host(client, admin, fake_host):
    rid = create(client, admin, fake_host).json()["id"]
    pending = check(client, admin, rid)["ssh_host_key_pending_fingerprint"]
    r = trust(client, admin, rid, pending)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "online" and out["ssh_host_key_fingerprint"] == pending and out["ssh_host_key_pending_fingerprint"] is None
    assert "Strix" in out["status_detail"] and "Docker" in out["status_detail"] and out["last_seen_at"] is not None
    assert load_runner(rid).ssh_host_key == fake_host.host_key_line and fake_host.logins >= 1


def test_a_wrong_fingerprint_pins_nothing(client, admin, fake_host):
    rid = create(client, admin, fake_host).json()["id"]
    check(client, admin, rid)
    r = trust(client, admin, rid, "SHA256:" + "A" * 43)
    assert r.status_code == 409 and "не совпадает" in r.json()["detail"]
    assert load_runner(rid).ssh_host_key is None and fake_host.attempts == 0


def test_there_must_be_something_to_confirm(client, admin, fake_host):
    rid = create(client, admin, fake_host).json()["id"]
    assert trust(client, admin, rid, "SHA256:" + "A" * 43).status_code == 409


def test_a_fingerprint_that_changed_between_looking_and_confirming_is_refused(client, admin, fake_host):
    """The administrator compared fingerprint A; by the time they click, the host shows B. B must not be pinned."""
    rid = create(client, admin, fake_host).json()["id"]
    seen_by_admin = check(client, admin, rid)["ssh_host_key_pending_fingerprint"]
    fake_host.rotate_host_key()
    check(client, admin, rid)  # the panel looks again and now holds the new key as pending
    r = trust(client, admin, rid, seen_by_admin)
    assert r.status_code == 409 and load_runner(rid).ssh_host_key is None


def test_only_admins_manage_host_keys(client, admin, operator, viewer, fake_host):
    rid = create(client, admin, fake_host).json()["id"]
    check(client, admin, rid)
    for headers in (operator, viewer):
        assert client.post(f"/api/runners/{rid}/trust-host-key", headers=headers, json={"fingerprint": "SHA256:x" * 6}).status_code == 403
        assert client.post(f"/api/runners/{rid}/forget-host-key", headers=headers).status_code == 403
    assert client.post(f"/api/runners/{rid}/trust-host-key", json={"fingerprint": "SHA256:x" * 6}).status_code == 401


def test_the_actions_exist_only_for_ssh_runners(client, admin):
    mock = make_runner()
    assert client.post(f"/api/runners/{mock.id}/trust-host-key", headers=admin, json={"fingerprint": "SHA256:" + "A" * 43}).status_code == 422
    assert client.post(f"/api/runners/{mock.id}/forget-host-key", headers=admin).status_code == 422
    assert client.post("/api/runners/9999/forget-host-key", headers=admin).status_code == 404


# ── a trusted host ──────────────────────────────────────────────────────

def test_a_ready_host_is_online(client, admin, fake_host):
    runner = make_ssh_runner(fake_host, trusted=True)
    out = check(client, admin, runner.id)
    assert out["status"] == "online" and "Strix 1.7.0 + Docker" in out["status_detail"]  # the version, whatever else the tool prints


def test_missing_software_is_named(client, admin, fake_host):
    runner = make_ssh_runner(fake_host, trusted=True)
    (fake_host.home / ".local" / "bin" / "strix").unlink()
    out = check(client, admin, runner.id)
    assert out["status"] == "offline" and "нет Strix" in out["status_detail"] and "Docker" not in out["status_detail"].split("нет")[1]
    (fake_host.home / ".local" / "bin" / "docker").unlink()
    out = check(client, admin, runner.id)
    assert out["status"] == "offline" and "Docker" in out["status_detail"] and "Strix" in out["status_detail"]


def test_docker_present_but_not_usable_is_told_apart_from_missing(client, admin, fake_host):
    runner = make_ssh_runner(fake_host, trusted=True)
    (fake_host.home / "docker-broken").write_text("1")
    out = check(client, admin, runner.id)
    assert out["status"] == "offline" and "доступ к Docker" in out["status_detail"]


def test_a_changed_host_key_is_an_error_and_is_never_pinned_automatically(client, admin, fake_host):
    runner = make_ssh_runner(fake_host, trusted=True)
    old_fp = fingerprint(fake_host.host_key_line)
    fake_host.rotate_host_key()
    out = check(client, admin, runner.id)
    new_fp = fingerprint(fake_host.host_key_line)
    assert out["status"] == "error" and "подмена" in out["status_detail"]
    assert old_fp in out["status_detail"] and new_fp in out["status_detail"]
    assert out["ssh_host_key_fingerprint"] == old_fp and out["ssh_host_key_pending_fingerprint"] == new_fp
    assert fake_host.attempts == 0  # not a single credential was offered to the new key
    # one click is not enough: the old key has to be dropped deliberately first
    r = trust(client, admin, runner.id, new_fp)
    assert r.status_code == 409 and "сбросьте старый ключ" in r.json()["detail"]
    assert fingerprint(load_runner(runner.id).ssh_host_key) == old_fp  # still the old one


def test_after_forgetting_the_old_key_the_new_one_can_be_confirmed(client, admin, fake_host):
    runner = make_ssh_runner(fake_host, trusted=True)
    fake_host.rotate_host_key()
    check(client, admin, runner.id)
    forgot = client.post(f"/api/runners/{runner.id}/forget-host-key", headers=admin).json()
    assert forgot["ssh_host_key_fingerprint"] is None and forgot["status"] == "offline"
    shown = check(client, admin, runner.id)["ssh_host_key_pending_fingerprint"]
    assert shown == fingerprint(fake_host.host_key_line)
    assert trust(client, admin, runner.id, shown).json()["status"] == "online"


def test_a_key_the_host_rejects_is_an_error(client, admin, fake_host):
    runner = make_ssh_runner(fake_host, trusted=True)
    import asyncssh
    from app.core.crypto import encrypt
    from app.core.db import SessionLocal
    with SessionLocal() as db:
        row = db.get(type(runner), runner.id)
        row.ssh_key_enc = encrypt(asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode())
        db.commit()
    out = check(client, admin, runner.id)
    assert out["status"] == "error" and "authorized_keys" in out["status_detail"]


def test_an_unreachable_host_is_offline_with_a_reason(client, admin, fake_host):
    runner = make_ssh_runner(fake_host, trusted=True)
    fake_host.stop()
    out = check(client, admin, runner.id)
    assert out["status"] == "offline" and out["status_detail"]


def test_editing_the_address_forgets_the_confirmed_key_but_other_edits_keep_it(client, admin, fake_host):
    runner = make_ssh_runner(fake_host, trusted=True)
    r = client.patch(f"/api/runners/{runner.id}", headers=admin, json={"name": "renamed", "ssh_user": fake_host.user})
    assert r.status_code == 200 and r.json()["ssh_host_key_fingerprint"] is not None
    r = client.patch(f"/api/runners/{runner.id}", headers=admin, json={"ssh_port": fake_host.port})  # same value: not a move
    assert r.json()["ssh_host_key_fingerprint"] is not None
    r = client.patch(f"/api/runners/{runner.id}", headers=admin, json={"ssh_host": "10.1.2.3"})
    assert r.status_code == 200 and r.json()["ssh_host_key_fingerprint"] is None and "заново" in r.json()["status_detail"]
    assert load_runner(runner.id).ssh_host_key is None


def test_secrets_stay_secret(client, admin, operator, fake_host):
    rid = create(client, admin, fake_host).json()["id"]
    check(client, admin, rid)
    row = load_runner(rid)
    assert row.ssh_key_enc != fake_host.client_key_pem and decrypt(row.ssh_key_enc) == fake_host.client_key_pem
    body = client.get("/api/runners", headers=admin).text
    assert fake_host.client_key_pem.splitlines()[1] not in body and "PRIVATE KEY" not in body
    # an operator sees the runner but none of its connection details or key fingerprints
    seen = next(r for r in client.get("/api/runners", headers=operator).json() if r["id"] == rid)
    assert seen["ssh_host"] is None and seen["ssh_host_key_pending_fingerprint"] is None and seen["has_ssh_key"] is False


# ── the periodic check ──────────────────────────────────────────────────

def test_the_periodic_check_refreshes_trusted_hosts_only(client, admin, fake_host, tmp_path):
    from tests.ssh_server import FakeHost

    other = FakeHost(tmp_path / "other-home").start()
    try:
        ready = make_ssh_runner(fake_host, "ready", trusted=True, status=RunnerStatus.error)
        unconfirmed = make_ssh_runner(other, "unconfirmed", trusted=False)
        assert check_ssh_runners() == 1
        assert load_runner(ready.id).status == RunnerStatus.online
        assert load_runner(unconfirmed.id).status == RunnerStatus.offline and other.attempts == 0 and other.connections == []
        fake_host.stop()
        check_ssh_runners()
        assert load_runner(ready.id).status == RunnerStatus.offline  # "online" does not outlive the host
    finally:
        other.stop()


def test_the_periodic_check_leaves_an_installation_alone(client, admin, fake_host):
    runner = make_ssh_runner(fake_host, trusted=True, status=RunnerStatus.provisioning)
    from datetime import datetime, timezone
    from app.core.db import SessionLocal
    from app.models import Runner
    with SessionLocal() as db:  # what the installation endpoint records when it starts
        db.get(Runner, runner.id).provision_started_at = datetime.now(timezone.utc)
        db.commit()
    check_ssh_runners()
    assert load_runner(runner.id).status == RunnerStatus.provisioning and fake_host.attempts == 0
