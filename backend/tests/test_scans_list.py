"""The scans list: search, the active/finished split and the per-status counts behind the tiles."""
from app.models import ScanStatus
from tests.conftest import make_scan
from tests.test_continue import mock_runner  # noqa: F401
from tests.test_resume_in_place import me


def ls(client, headers, **params):
    r = client.get("/api/scans", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def seed(client, headers, runner):
    uid = me(client, headers)
    return {
        "run": make_scan(uid, runner.id, status=ScanStatus.running, targets=["https://shop.example.com", "https://cdn.example.com"]),
        "queue": make_scan(uid, runner.id, status=ScanStatus.queued, targets=["https://example.com"]),
        "stop": make_scan(uid, runner.id, status=ScanStatus.cancelling, targets=["https://api.example.com"]),
        "vuln": make_scan(uid, runner.id, status=ScanStatus.vulns_found, targets=["https://shop.example.com"]),
        "fail": make_scan(uid, runner.id, status=ScanStatus.failed, targets=["https://api.example.com"]),
        "odd": make_scan(uid, runner.id, status=ScanStatus.succeeded, targets=["https://100%_real.example.org"]),
    }


def ids(page):
    return [s["id"] for s in page["items"]]


def test_counts_cover_every_status_and_ignore_the_status_filter(client, operator, mock_runner):
    seed(client, operator, mock_runner)
    want = {"queued": 1, "running": 1, "cancelling": 1, "succeeded": 1, "vulns_found": 1, "failed": 1, "cancelled": 0}
    assert ls(client, operator)["counts"] == want
    narrowed = ls(client, operator, status="failed")
    assert narrowed["total"] == 1 and narrowed["counts"] == want  # the tiles do not change when one of them is picked


def test_active_splits_what_is_going_on_from_the_history(client, operator, mock_runner):
    s = seed(client, operator, mock_runner)
    assert set(ids(ls(client, operator, active="true"))) == {s["run"].id, s["queue"].id, s["stop"].id}
    assert set(ids(ls(client, operator, active="false"))) == {s["vuln"].id, s["fail"].id, s["odd"].id}
    assert ls(client, operator, active="false")["total"] == 3 and ls(client, operator)["total"] == 6


def test_search_finds_a_part_of_any_target_ignoring_case(client, operator, mock_runner):
    s = seed(client, operator, mock_runner)
    assert set(ids(ls(client, operator, q="SHOP"))) == {s["run"].id, s["vuln"].id}
    assert ids(ls(client, operator, q="cdn.example")) == [s["run"].id]  # not only the first target
    found = ls(client, operator, q=" api.example.com ")
    assert set(ids(found)) == {s["stop"].id, s["fail"].id}
    assert found["counts"]["failed"] == 1 and found["counts"]["running"] == 0  # the tiles follow the search
    assert ls(client, operator, q="nothing-like-this")["total"] == 0


def test_search_by_number_and_with_wildcard_characters(client, operator, mock_runner):
    s = seed(client, operator, mock_runner)
    assert s["fail"].id in ids(ls(client, operator, q=str(s["fail"].id)))
    assert s["fail"].id in ids(ls(client, operator, q=f"#{s['fail'].id}"))
    assert ids(ls(client, operator, q="100%_real")) == [s["odd"].id]
    assert ls(client, operator, q="%")["total"] == 1 and ls(client, operator, q="_")["total"] == 1  # literal, not "anything"
    assert ls(client, operator, q="   ")["total"] == 6  # blank = no search


def test_an_operator_still_sees_and_counts_only_their_own(client, admin, operator, mock_runner):
    seed(client, operator, mock_runner)
    make_scan(me(client, admin), mock_runner.id, status=ScanStatus.failed, targets=["https://shop.example.com"])
    assert ls(client, operator)["counts"]["failed"] == 1 and ls(client, admin)["counts"]["failed"] == 2
    assert ls(client, operator, q="shop")["total"] == 2 and ls(client, admin, q="shop")["total"] == 3
