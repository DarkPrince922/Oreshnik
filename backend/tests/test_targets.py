import pytest

from app.services.targets import TargetKind, classify_all, classify_target

REMOTE = [
    "https://example.com", "http://example.com/app?x=1", "https://user@example.com/x", "example.com",
    "sub.example.com:8080", "example.com/path/x", "192.168.1.42", "::1", "git@github.com:org/repo.git",
    "git://github.com/org/repo", "https://github.com/org/repo",
]
PATHLIKE = ["/etc", "~", "~/.ssh", "./app", "../secrets", "/root/.ssh/id_rsa", "localhost", "..", "etc/passwd", "C:\\Users"]
INVALID = ["", "   ", "-evil", "--target=x", "-t", "a b", "http://", "ftp://example.com", "file:///etc/passwd",
           "postman://abc", "exa\nmple.com", "x" * 3000, "https://exa mple.com",
           "a.b/../../etc", "example.com/./x", "example.com/.."]


@pytest.mark.parametrize("raw", REMOTE)
def test_remote_targets_are_safe_for_everyone(raw):
    assert classify_target(raw) == (raw, TargetKind.remote)


@pytest.mark.parametrize("raw", PATHLIKE)
def test_anything_that_could_be_a_local_path_is_admin_only(raw):
    # Strix would mount an existing path into the sandbox WRITABLE, so these must never
    # be classified as remote.
    assert classify_target(raw)[1] == TargetKind.path


@pytest.mark.parametrize("raw", INVALID)
def test_invalid_targets_are_rejected(raw):
    with pytest.raises(ValueError):
        classify_target(raw)


def test_whitespace_is_trimmed_and_duplicates_dropped_in_order():
    out = classify_all([" https://a.com ", "b.com", "https://a.com", "b.com"])
    assert out == [("https://a.com", TargetKind.remote), ("b.com", TargetKind.remote)]


def test_a_dash_hidden_behind_a_scheme_is_fine_but_a_leading_dash_never_is():
    assert classify_target("https://-x.example.com")[1] == TargetKind.remote
    with pytest.raises(ValueError):
        classify_target("-https://example.com")


def test_file_like_names_that_look_like_domains_are_remote_because_cwd_is_private():
    # Strix would try Path("openapi.yaml").exists() first, but the run's cwd is an empty per-scan
    # directory, so this can only ever become "https://openapi.yaml".
    assert classify_target("openapi.yaml")[1] == TargetKind.remote


def test_traversal_through_a_host_like_prefix_is_rejected():
    for raw in ("a.b/../../../etc/passwd", "example.com/../x", "example.com/./x"):
        with pytest.raises(ValueError):
            classify_target(raw)
    assert classify_target("example.com/a..b/c")[1] == TargetKind.remote  # ".." only matters as a whole segment
