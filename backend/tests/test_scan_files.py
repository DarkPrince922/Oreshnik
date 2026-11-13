from app.core.config import settings
from app.services.scan_files import (
    LogWriter, clean_line, find_run_dir, log_path, read_log_lines, scan_dir, tail_from,
)


def test_clean_line_strips_ansi_control_chars_and_keeps_last_cr_segment():
    assert clean_line("\x1b[31mred\x1b[0m ok") == "red ok"
    assert clean_line("p 10%\rp 50%\rp 100%") == "p 100%"
    assert clean_line("a\x00b\x07c") == "abc"
    assert clean_line("\x1b]0;title\x07text") == "text"
    assert clean_line("x" * 9000).endswith("…[обрезано]")


def test_log_writer_redacts_secrets_and_tracks_last_line(tmp_path):
    w = LogWriter(1, secrets=["sk-secret-123456", "abc"])  # short values are ignored
    w.write("key is sk-secret-123456 and abc")
    w.write("")
    w.close()
    lines, nxt = read_log_lines(1)
    assert lines[0] == "key is *** and abc" and nxt == 2
    assert w.last_line == "key is *** and abc"


def test_log_writer_stops_at_the_size_limit(monkeypatch):
    monkeypatch.setattr(settings, "scan_log_max_bytes", 200)
    w = LogWriter(2)
    for i in range(100):
        w.write(f"line {i} " + "x" * 20)
    w.close()
    lines, _ = read_log_lines(2, limit=5000)
    assert lines[-1].startswith("[лог обрезан")
    assert len(lines) < 12
    assert log_path(2).stat().st_size < 400


def test_read_log_lines_pagination():
    w = LogWriter(3)
    for i in range(10):
        w.write(f"l{i}")
    w.close()
    assert read_log_lines(3, 0, 4) == (["l0", "l1", "l2", "l3"], 4)
    assert read_log_lines(3, 4, 4) == (["l4", "l5", "l6", "l7"], 8)
    assert read_log_lines(3, 8, 4) == (["l8", "l9"], 10)
    assert read_log_lines(3, 50, 4) == ([], 50)
    assert read_log_lines(999) == ([], 0)


def test_tail_from_returns_only_complete_lines(tmp_path):
    path = tmp_path / "t.log"
    path.write_bytes(b"one\ntwo\npart")
    lines, pos = tail_from(path, 0)
    assert lines == ["one", "two"] and pos == 8
    path.write_bytes(b"one\ntwo\npartial done\n")
    assert tail_from(path, pos) == (["partial done"], 21)
    assert tail_from(path, 21) == ([], 21)
    assert tail_from(tmp_path / "missing", 0) == ([], 0)


def test_find_run_dir_picks_the_newest_run_with_a_run_json(tmp_path):
    base = tmp_path / "strix_runs"
    (base / "empty").mkdir(parents=True)
    (base / "old").mkdir()
    (base / "old" / "run.json").write_text("{}")
    (base / "new").mkdir()
    (base / "new" / "run.json").write_text("{}")
    import os, time
    os.utime(base / "old" / "run.json", (time.time() - 100, time.time() - 100))
    assert find_run_dir(tmp_path).name == "new"
    assert find_run_dir(tmp_path / "nothing") is None


def test_scan_dir_is_built_from_the_integer_id_only():
    assert scan_dir(7).name == "scan-7"
    assert scan_dir("7").name == "scan-7"  # coerced; can never contain path separators


def test_last_line_skips_borders_and_blank_lines_so_failure_messages_are_useful():
    w = LogWriter(4)
    for line in ("╭──────────────╮", "│  DOCKER NOT AVAILABLE   │", "│                         │",
                 "│ Make sure Docker is running. │", "╰──────────────╯", "", "   "):
        w.write(line)
    w.close()
    assert w.last_line == "Make sure Docker is running."
