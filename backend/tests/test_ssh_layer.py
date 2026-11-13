"""The SSH client layer, over a real SSH connection to a test server."""
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from app.services.ssh import (
    SshConnection,
    SshError,
    SshErrorKind,
    SshTarget,
    fetch_host_key,
    fingerprint,
    key_algorithm,
    q,
)


def connect(host, **kw) -> SshConnection:
    return SshConnection(host.target(), **kw).open()


# ── the host key is pinned ──────────────────────────────────────────────

def test_the_host_key_can_be_fetched_without_logging_in(fake_host):
    line = fetch_host_key("127.0.0.1", fake_host.port)
    assert line == fake_host.host_key_line and key_algorithm(line) == "ssh-ed25519"
    fp = fingerprint(line)
    assert fp.startswith("SHA256:") and "=" not in fp  # the way `ssh-keygen -lf` prints it
    assert fp == fake_host.host_key.get_fingerprint("sha256")
    assert fake_host.attempts == 0  # nothing was sent but the handshake: no credentials left the panel


def test_it_refuses_to_connect_without_a_confirmed_key_and_does_not_even_try(fake_host):
    with pytest.raises(SshError) as exc:
        SshConnection(fake_host.target(trusted=False)).open()
    assert exc.value.kind == SshErrorKind.host_key_untrusted
    assert fake_host.attempts == 0 and fake_host.connections == []


def test_a_different_host_key_is_refused_before_any_credential_is_sent(fake_host):
    old = fake_host.host_key_line
    fake_host.rotate_host_key()
    target = SshTarget("127.0.0.1", fake_host.port, fake_host.user, fake_host.client_key_pem, old)
    with pytest.raises(SshError) as exc:
        SshConnection(target).open()
    assert exc.value.kind == SshErrorKind.host_key_changed and "подмена" in exc.value.message
    assert fake_host.attempts == 0  # a man in the middle never gets to see a login attempt


def test_a_pinned_key_keeps_working_across_connections(fake_host):
    for _ in range(3):
        with connect(fake_host) as conn:
            assert conn.run("echo ok").stdout == "ok\n"
    assert len(fake_host.connections) == 3 and fake_host.logins >= 3


# ── failures are classified, and never leak the key ─────────────────────

def test_a_wrong_user_is_an_auth_error(fake_host):
    target = SshTarget("127.0.0.1", fake_host.port, "somebody-else", fake_host.client_key_pem, fake_host.host_key_line)
    with pytest.raises(SshError) as exc:
        SshConnection(target).open()
    assert exc.value.kind == SshErrorKind.auth and "authorized_keys" in exc.value.message


def test_a_key_the_host_does_not_know_is_an_auth_error(fake_host):
    import asyncssh

    other = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()
    with pytest.raises(SshError) as exc:
        SshConnection(SshTarget("127.0.0.1", fake_host.port, fake_host.user, other, fake_host.host_key_line)).open()
    assert exc.value.kind == SshErrorKind.auth


def test_garbage_instead_of_a_key_is_reported_not_crashed(fake_host):
    with pytest.raises(SshError) as exc:
        SshConnection(SshTarget("127.0.0.1", fake_host.port, fake_host.user, "not a key", fake_host.host_key_line)).open()
    assert exc.value.kind == SshErrorKind.auth
    assert "not a key" not in exc.value.message


def test_nothing_listens(fake_host):
    with socket.socket() as s:  # a port that was free a moment ago
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(SshError) as exc:
        SshConnection(SshTarget("127.0.0.1", port, fake_host.user, fake_host.client_key_pem, fake_host.host_key_line), connect_timeout=3).open()
    assert exc.value.kind == SshErrorKind.unreachable
    with pytest.raises(SshError) as exc:
        fetch_host_key("127.0.0.1", port, timeout=3)
    assert exc.value.kind == SshErrorKind.unreachable


def test_an_unknown_host_name(fake_host):
    with pytest.raises(SshError) as exc:
        fetch_host_key("no-such-host.invalid", 22, timeout=5)
    assert exc.value.kind == SshErrorKind.unreachable


def test_a_silent_host_times_out(fake_host):
    """A server that accepts the TCP connection and never speaks SSH."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    try:
        started = time.monotonic()
        with pytest.raises(SshError) as exc:
            SshConnection(SshTarget("127.0.0.1", srv.getsockname()[1], "u", fake_host.client_key_pem, fake_host.host_key_line), connect_timeout=2).open()
        assert exc.value.kind in (SshErrorKind.unreachable, SshErrorKind.protocol) and time.monotonic() - started < 12
    finally:
        srv.close()


def test_the_private_key_never_appears_in_an_error(fake_host):
    leaks = []
    for target in (
        SshTarget("127.0.0.1", fake_host.port, "nobody", fake_host.client_key_pem, fake_host.host_key_line),
        SshTarget("127.0.0.1", fake_host.port, fake_host.user, fake_host.client_key_pem, None),
    ):
        try:
            SshConnection(target).open()
        except SshError as exc:
            leaks.append(fake_host.client_key_pem.splitlines()[1] in (exc.message + repr(target) + repr(exc)))
    assert leaks == [False, False]


# ── commands ────────────────────────────────────────────────────────────

def test_a_command_reports_status_and_keeps_stdout_and_stderr_apart(fake_host):
    with connect(fake_host) as conn:
        done = conn.run("echo out; echo err >&2; exit 3")
    assert (done.status, done.stdout, done.stderr) == (3, "out\n", "err\n")


def test_input_goes_to_the_command_not_into_its_command_line(fake_host):
    with connect(fake_host) as conn:
        done = conn.run("sh -s", input='SECRET="hunter2-not-in-argv"\necho "got $SECRET"\n')
    assert done.stdout == "got hunter2-not-in-argv\n"
    assert all("hunter2" not in c for c in fake_host.commands)  # the command line is visible to everybody on the host
    assert any("hunter2" in i for i in fake_host.inputs)


def test_a_command_that_takes_too_long_is_reported(fake_host):
    with connect(fake_host) as conn:
        with pytest.raises(SshError) as exc:
            conn.run("sleep 5", timeout=1)
    assert exc.value.kind == SshErrorKind.unreachable and "вовремя" in exc.value.message


def test_a_long_running_command_streams_its_output_as_it_goes(fake_host):
    with connect(fake_host) as conn:
        proc = conn.start("sh -s", input="echo one; sleep 0.4; echo two; echo oops >&2; exit 7\n")
        seen, stamps = [], []
        t0 = time.monotonic()
        while not proc.finished and time.monotonic() - t0 < 10:
            for line in proc.lines(0.2):
                seen.append(line)
                stamps.append(time.monotonic() - t0)
        assert seen == ["one", "two", "oops"] and proc.exit_status == 7 and proc.error is None
        assert stamps[1] - stamps[0] > 0.25  # "one" arrived before "two" was even produced


def test_a_dropped_connection_ends_the_stream_with_an_error(fake_host):
    with connect(fake_host) as conn:
        proc = conn.start("sh -s", input="echo started; sleep 30\n")
        assert proc.lines(3) == ["started"]
        fake_host.kick()
        assert proc.wait(10) and proc.exit_status is None
        assert proc.error is not None and proc.error.kind == SshErrorKind.protocol


def test_the_connection_can_be_used_from_several_threads(fake_host):
    with connect(fake_host) as conn:
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(conn.run(f"echo {i}").stdout.strip())) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert sorted(results) == sorted(str(i) for i in range(8))


# ── files ───────────────────────────────────────────────────────────────

def test_a_directory_goes_there_and_comes_back(fake_host, tmp_path):
    src = tmp_path / "run"
    (src / ".state").mkdir(parents=True)
    (src / "run.json").write_text('{"a": 1}')
    (src / ".state" / "agents.json").write_text("[]")
    with connect(fake_host) as conn:
        assert conn.put_tree(src, "oreshnik/scan-9/strix_runs/r1") == 2
        assert conn.run("cat $HOME/oreshnik/scan-9/strix_runs/r1/run.json").stdout == '{"a": 1}'
        back = tmp_path / "back"
        files, size = conn.get_tree("oreshnik/scan-9/strix_runs/r1", back)
    assert files == 2 and size == len('{"a": 1}') + 2
    assert (back / "run.json").read_text() == '{"a": 1}' and (back / ".state" / "agents.json").read_text() == "[]"
    assert not list(back.rglob("*.part"))  # no half-downloaded files are left behind


def test_symlinks_are_never_followed_in_either_direction(fake_host, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("panel secret")
    src = tmp_path / "run"
    src.mkdir()
    (src / "ok.txt").write_text("ok")
    os.symlink(secret, src / "link.txt")
    os.symlink(tmp_path, src / "dirlink")
    with connect(fake_host) as conn:
        assert conn.put_tree(src, "oreshnik/up") == 1
        assert conn.list_dir("oreshnik/up") == ["ok.txt"]  # neither link was uploaded
        # a hostile host plants links in the run directory
        conn.run("cd $HOME/oreshnik && mkdir -p evil/sub && echo data > evil/real.txt && ln -s /etc/passwd evil/passwd && ln -s / evil/rootlink && ln -s /etc evil/sub/etc")
        back = tmp_path / "evil"
        files, _ = conn.get_tree("oreshnik/evil", back)
    assert files == 1 and [p.name for p in back.rglob("*") if p.is_file()] == ["real.txt"]
    assert not (back / "passwd").exists() and not (back / "rootlink").exists()


def test_only_regular_files_are_fetched(fake_host, tmp_path):
    with connect(fake_host) as conn:
        conn.run("cd $HOME && mkdir -p oreshnik/odd && echo x > oreshnik/odd/f && mkfifo oreshnik/odd/pipe")
        files, _ = conn.get_tree("oreshnik/odd", tmp_path / "odd")
    assert files == 1 and [p.name for p in (tmp_path / "odd").iterdir()] == ["f"]


def test_a_host_that_hands_over_too_much_is_cut_off(fake_host, tmp_path):
    with connect(fake_host) as conn:
        conn.run("cd $HOME && mkdir -p oreshnik/big && for i in 1 2 3 4 5; do head -c 2000 /dev/zero > oreshnik/big/f$i; done")
        with pytest.raises(SshError) as exc:
            conn.get_tree("oreshnik/big", tmp_path / "a", max_bytes=5000)
        assert "слишком велик" in exc.value.message
        with pytest.raises(SshError):
            conn.get_tree("oreshnik/big", tmp_path / "b", max_files=3)
        assert conn.get_tree("oreshnik/big", tmp_path / "c", max_bytes=10_000, max_files=5)[0] == 5


def test_skipped_names_are_not_fetched(fake_host, tmp_path):
    with connect(fake_host) as conn:
        conn.run("cd $HOME && mkdir -p oreshnik/s/.state && echo 1 > oreshnik/s/run.json && echo 2 > oreshnik/s/.state/agents.db && echo 3 > oreshnik/s/.state/agents.db-wal")
        conn.get_tree("oreshnik/s", tmp_path / "s", skip=lambda rel: rel.startswith(".state/agents.db"))
    assert sorted(str(p.relative_to(tmp_path / "s")) for p in (tmp_path / "s").rglob("*") if p.is_file()) == ["run.json"]


def test_a_single_file_is_fetched_atomically_and_missing_ones_are_just_missing(fake_host, tmp_path):
    with connect(fake_host) as conn:
        conn.write_file("oreshnik/one.txt", b"hello")
        assert conn.get_file("oreshnik/one.txt", tmp_path / "x" / "one.txt") is True
        assert (tmp_path / "x" / "one.txt").read_bytes() == b"hello"
        assert conn.get_file("oreshnik/nope.txt", tmp_path / "nope.txt") is False and not (tmp_path / "nope.txt").exists()
        conn.run("ln -s /etc/hostname $HOME/oreshnik/link.txt")
        assert conn.get_file("oreshnik/link.txt", tmp_path / "link.txt") is False  # a symlink is not a regular file
        conn.run("head -c 5000 /dev/zero > $HOME/oreshnik/big.bin")
        with pytest.raises(SshError):
            conn.get_file("oreshnik/big.bin", tmp_path / "big.bin", max_bytes=1000)


def test_written_files_are_private(fake_host):
    with connect(fake_host) as conn:
        conn.write_file("oreshnik/secret.env", b"KEY=1")
    assert (fake_host.home / "oreshnik" / "secret.env").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("path", ["", "/", "/etc", "..", "oreshnik/../..", "other/dir", "oreshnik", "/oreshnik/scan-1", "~/oreshnik/x"])
def test_the_panel_only_deletes_inside_its_own_directory(fake_host, path):
    (fake_host.home / "keep").mkdir()
    with connect(fake_host) as conn:
        with pytest.raises(SshError):
            conn.remove_tree(path)
    assert (fake_host.home / "keep").exists()


def test_remove_tree_removes_the_scan_directory(fake_host):
    with connect(fake_host) as conn:
        conn.makedirs("oreshnik/scan-5/strix_runs/r")
        conn.write_file("oreshnik/scan-5/strix_runs/r/run.json", b"{}")
        conn.remove_tree("oreshnik/scan-5")
        assert not conn.exists("oreshnik/scan-5")
        conn.remove_tree("oreshnik/scan-5")  # already gone: fine


def test_shell_quoting_keeps_hostile_words_literal(fake_host, tmp_path):
    pwned = tmp_path / "pwned"  # a sentinel that is unique per run, so a stale file can never make this pass or fail
    hostile = f"https://x.example/$(touch {pwned}); `id` 'quote' \"dq\" \\ ; & | > <"
    with connect(fake_host) as conn:
        done = conn.run(f"printf %s {q(hostile)}")
    assert done.stdout == hostile and not pwned.exists()


def test_using_a_closed_connection_is_an_error_not_a_hang(fake_host):
    conn = connect(fake_host)
    conn.close()
    with pytest.raises(SshError):
        conn.run("echo hi")
    conn.close()  # closing twice is fine
