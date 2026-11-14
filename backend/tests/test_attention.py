"""The lights on «Сканы» and «Агент»: what is new for the user since they last looked."""
from datetime import datetime, timedelta, timezone

from app.core.db import SessionLocal
from app.models import Role, ScanStatus
from app.models.agent import AgentEvent, AgentSession, AgentSessionStatus
from tests.conftest import auth_headers, make_runner, make_scan, make_user

NOW = datetime.now(timezone.utc)
BEFORE = (NOW - timedelta(hours=1)).isoformat()


def get(client, headers, **params):
    r = client.get("/api/attention", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def user(client, email="u@example.com", role=Role.operator):
    u = make_user(email, role=role)
    return u, auth_headers(client, email)


def session(user_id: int, status=AgentSessionStatus.idle, events=()) -> int:
    with SessionLocal() as db:
        s = AgentSession(user_id=user_id, title="s", status=status)
        db.add(s)
        db.flush()
        for seq, (kind, payload, when) in enumerate(events):
            db.add(AgentEvent(session_id=s.id, seq=seq, kind=kind, payload=payload, created_at=when))
        db.commit()
        return s.id


def test_needs_a_login(client):
    assert client.get("/api/attention").status_code == 401


def test_the_first_call_reports_nothing_and_gives_the_servers_clock(client):
    _, h = user(client)
    body = get(client, h)
    assert body["scans"] == {"finished": 0, "failed": 0, "findings": 0} and body["agent"] == {"finished": 0, "waiting": 0}
    assert abs((datetime.fromisoformat(body["now"]) - datetime.now(timezone.utc)).total_seconds()) < 30


def test_scans_that_finished_since_the_user_looked_light_up(client):
    u, h = user(client)
    runner = make_runner()
    make_scan(u.id, runner.id, status=ScanStatus.succeeded, finished_at=NOW)
    make_scan(u.id, runner.id, status=ScanStatus.vulns_found, finished_at=NOW)
    make_scan(u.id, runner.id, status=ScanStatus.failed, finished_at=NOW)
    make_scan(u.id, runner.id, status=ScanStatus.cancelled, finished_at=NOW)                       # the user did that themselves: no news
    make_scan(u.id, runner.id, status=ScanStatus.succeeded, finished_at=NOW - timedelta(hours=2))  # seen already
    make_scan(u.id, runner.id, status=ScanStatus.running)                                          # not finished
    assert get(client, h, scans_since=BEFORE)["scans"] == {"finished": 3, "failed": 1, "findings": 1}
    assert get(client, h, scans_since=(NOW + timedelta(minutes=1)).isoformat())["scans"]["finished"] == 0  # after opening the page


def test_other_peoples_scans_do_not_light_up_even_for_an_admin(client):
    owner, _ = user(client, "owner@example.com")
    make_scan(owner.id, make_runner().id, status=ScanStatus.succeeded, finished_at=NOW)
    _, admin = user(client, "boss@example.com", Role.admin)
    assert get(client, admin, scans_since=BEFORE)["scans"]["finished"] == 0


def test_an_agent_turn_that_ended_lights_up_once_the_session_is_idle(client):
    u, h = user(client)
    session(u.id, events=[("turn.completed", {}, NOW)])
    session(u.id, events=[("turn.error", {"message": "x"}, NOW)])
    session(u.id, events=[("turn.completed", {}, NOW), ("turn.completed", {}, NOW)])               # one session, counted once
    session(u.id, events=[("turn.completed", {}, NOW - timedelta(hours=2))])                       # seen already
    session(u.id, events=[("turn.stopped", {}, NOW)])                                              # the user stopped it: no news
    session(u.id, status=AgentSessionStatus.running, events=[("turn.completed", {}, NOW)])         # a goal still working
    other, _ = user(client, "other@example.com")
    session(other.id, events=[("turn.completed", {}, NOW)])
    assert get(client, h, agent_since=BEFORE)["agent"] == {"finished": 3, "waiting": 0}
    assert get(client, h, agent_since=(NOW + timedelta(minutes=1)).isoformat())["agent"]["finished"] == 0


def test_an_agent_waiting_for_an_answer_lights_up_until_it_is_answered(client):
    u, h = user(client)
    ask = lambda rid: ("approval.requested", {"request_id": rid, "approval": {"kind": "command"}}, NOW - timedelta(hours=3))  # noqa: E731
    done = lambda rid: ("approval.resolved", {"request_id": rid}, NOW - timedelta(hours=3))  # noqa: E731
    session(u.id, status=AgentSessionStatus.running, events=[ask("1")])
    session(u.id, status=AgentSessionStatus.running, events=[ask("1"), done("1"), ask("2")])
    session(u.id, status=AgentSessionStatus.running, events=[ask("1"), done("1")])   # answered
    session(u.id, status=AgentSessionStatus.idle, events=[ask("1")])                 # the turn is over: nothing waits
    # however long ago it asked, and whenever the page was last opened
    assert get(client, h, agent_since=(NOW + timedelta(minutes=1)).isoformat())["agent"]["waiting"] == 2
    assert get(client, h)["agent"]["waiting"] == 2


def test_a_viewer_gets_no_agent_light(client):
    u, h = user(client, "v@example.com", Role.viewer)
    session(u.id, status=AgentSessionStatus.running, events=[("approval.requested", {"request_id": "1"}, NOW)])
    assert get(client, h, agent_since=BEFORE)["agent"] == {"finished": 0, "waiting": 0}


def test_times_without_a_zone_and_nonsense_are_handled(client):
    _, h = user(client)
    assert get(client, h, scans_since="2026-01-01T00:00:00")["scans"]["finished"] == 0
    assert client.get("/api/attention", params={"scans_since": "yesterday"}, headers=h).status_code == 422
