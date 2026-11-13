"""Live log streaming over WebSocket."""
import json
import threading
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api import scans as scans_api
from app.models import ScanStatus
from app.services.scan_files import LogWriter
from app.worker.tasks import scan_run
from tests.conftest import auth_headers, make_runner, make_scan, make_user
from app.models import Role


def token(headers):
    return headers["Authorization"].split(" ", 1)[1]


def collect(ws, until="end", limit=500):
    frames = []
    for _ in range(limit):
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == until:
            break
    return frames


def connect(client, scan_id, tok, **extra):
    ws = client.websocket_connect(f"/api/scans/{scan_id}/ws")
    return ws, json.dumps({"token": tok, **extra})


@pytest.fixture
def finished_scan(client, operator):
    runner = make_runner()
    me = client.get("/api/auth/me", headers=operator).json()["id"]
    scan = make_scan(me, runner.id)
    scan_run(scan.id)
    return scan.id


def test_replays_the_whole_log_then_reports_the_final_status(client, operator, finished_scan):
    with client.websocket_connect(f"/api/scans/{finished_scan}/ws") as ws:
        ws.send_text(json.dumps({"token": token(operator)}))
        frames = collect(ws)
    assert frames[0] == {"type": "status", "status": "vulns_found"}
    logs = [f for f in frames if f["type"] == "log"]
    assert [f["seq"] for f in logs] == list(range(len(logs))) and len(logs) > 8
    assert "Scan completed" in logs[-1]["line"]
    assert frames[-1] == {"type": "end", "status": "vulns_found"}


def test_can_resume_from_a_sequence_number(client, operator, finished_scan):
    with client.websocket_connect(f"/api/scans/{finished_scan}/ws") as ws:
        ws.send_text(json.dumps({"token": token(operator), "from": 5}))
        frames = collect(ws)
    logs = [f for f in frames if f["type"] == "log"]
    assert logs[0]["seq"] == 5


@pytest.mark.parametrize("first_frame", ["not json", json.dumps({}), json.dumps({"token": 5}), json.dumps({"token": "garbage"}), json.dumps([1])])
def test_unauthenticated_connections_are_closed_with_4401(client, finished_scan, first_frame):
    with client.websocket_connect(f"/api/scans/{finished_scan}/ws") as ws:
        ws.send_text(first_frame)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_cannot_watch_a_scan_you_cannot_see(client, operator, admin):
    runner = make_runner()
    admin_id = client.get("/api/auth/me", headers=admin).json()["id"]
    foreign = make_scan(admin_id, runner.id).id
    with client.websocket_connect(f"/api/scans/{foreign}/ws") as ws:
        ws.send_text(json.dumps({"token": token(operator)}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4404
    with client.websocket_connect(f"/api/scans/999999/ws") as ws:
        ws.send_text(json.dumps({"token": token(operator)}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4404


def test_a_deactivated_users_token_is_refused(client, admin):
    user = make_user("gone@example.com", active=False)
    from app.core.security import create_access_token
    runner = make_runner()
    scan = make_scan(user.id, runner.id)
    with client.websocket_connect(f"/api/scans/{scan.id}/ws") as ws:
        ws.send_text(json.dumps({"token": create_access_token(str(user.id))}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_streams_live_while_the_scan_runs(client, operator, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "mock_delay_seconds", 0.08)
    monkeypatch.setattr(scans_api, "_WS_POLL_SECONDS", 0.05)
    runner = make_runner()
    me = client.get("/api/auth/me", headers=operator).json()["id"]
    scan = make_scan(me, runner.id)

    with client.websocket_connect(f"/api/scans/{scan.id}/ws") as ws:
        ws.send_text(json.dumps({"token": token(operator)}))
        assert ws.receive_json() == {"type": "status", "status": "queued"}  # connected before the worker started
        worker = threading.Thread(target=scan_run, args=(scan.id,))
        t0 = time.monotonic()
        worker.start()
        first_log_at = None
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "log" and first_log_at is None:
                first_log_at = time.monotonic() - t0
            if frame["type"] == "end":
                break
        worker.join()
    statuses = [f["status"] for f in frames if f["type"] == "status"]
    assert statuses == ["running", "vulns_found"]
    assert first_log_at < 1.0  # delivered while the scan was still running, not after
    logs = [f for f in frames if f["type"] == "log"]
    assert [f["seq"] for f in logs] == list(range(len(logs)))
    assert "Scan completed" in logs[-1]["line"]


def test_live_lines_arrive_complete_never_half_written(client, operator, monkeypatch):
    monkeypatch.setattr(scans_api, "_WS_POLL_SECONDS", 0.02)
    runner = make_runner()
    me = client.get("/api/auth/me", headers=operator).json()["id"]
    scan = make_scan(me, runner.id, status=ScanStatus.running)
    writer = LogWriter(scan.id)
    expected = [f"line-{i:03d}-" + "x" * 50 for i in range(60)]

    def produce():
        for line in expected:
            writer.write(line)
            time.sleep(0.003)
        writer.close()
        from sqlalchemy import update
        from app.core.db import SessionLocal
        from app.models import Scan
        with SessionLocal() as db:
            db.execute(update(Scan).where(Scan.id == scan.id).values(status=ScanStatus.succeeded))
            db.commit()

    with client.websocket_connect(f"/api/scans/{scan.id}/ws") as ws:
        ws.send_text(json.dumps({"token": token(operator)}))
        thread = threading.Thread(target=produce)
        thread.start()
        frames = collect(ws)
        thread.join()
    assert [f["line"] for f in frames if f["type"] == "log"] == expected
