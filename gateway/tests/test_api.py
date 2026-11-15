"""API routes: store-backed endpoints via TestClient, /stream over a real socket."""
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from gateway.api import create_app
from gateway.events import EventStore
from gateway.notify import OperatorChannel
from gateway.station import StationSnapshot
from test_events import EVENT


class FakePoller:
    snapshot = StationSnapshot(holding={"DOSER_RATE_PCT": 10}, connected=True, ts=1.0)


@pytest.fixture
def store():
    s = EventStore("sqlite:///:memory:")
    yield s
    s.close()


@pytest.fixture
def client(store, tmp_path):
    (tmp_path / "evt_0001.md").write_text("# report\n")
    app = create_app(store, FakePoller(), lambda snap: {"presence": False},
                     channel=OperatorChannel(store), reports_dir=tmp_path)
    return TestClient(app)


def test_events_list_omits_trajectory_but_detail_has_it(client, store):
    store.save_event(EVENT)
    store.save_event({**EVENT, "decision": "ALLOW"})
    rows = client.get("/events").json()
    assert [r["id"] for r in rows] == ["evt_0002", "evt_0001"]        # newest first
    assert all("trajectory" not in r["prediction"] for r in rows)
    assert rows[1]["prediction"]["safe"] is False
    one = client.get("/events/evt_0001").json()
    assert one["prediction"]["trajectory"] == [[0, 60.0, 1.0]]
    assert one["reasons"] == EVENT["reasons"]
    assert client.get("/events/evt_9999").status_code == 404
    assert client.get("/events?limit=1").json()[0]["id"] == "evt_0002"
    assert client.get("/events?limit=0").status_code == 422


def test_state(client):
    s = client.get("/state").json()
    assert s["connected"] is True and s["holding"] == {"DOSER_RATE_PCT": 10}
    assert s["station"] == {"presence": False}


def test_operator_reply_needs_the_one_time_code(client, store):
    store.save_event(EVENT)
    channel = client.app.state.channel
    channel.send("evt_0001", "Held. Reply BLOCK or ALLOW")
    (msg,) = client.get("/operator/inbox").json()
    body = {"reply": "BLOCK", "event_id": "evt_0001", "code": msg["code"]}
    wrong = "000000" if msg["code"] != "000000" else "111111"
    assert client.post("/operator/reply", json={**body, "code": wrong}).status_code == 403
    assert client.post("/operator/reply", json={"reply": "BLOCK", "event_id": "evt_0001"}).status_code == 422
    assert client.post("/operator/reply", json={**body, "reply": "MAYBE"}).status_code == 422
    assert client.post("/operator/reply", json={**body, "extra": 1}).status_code == 422
    assert client.post("/operator/reply", json={**body, "event_id": "evt_9999"}).status_code == 403
    assert channel.decision("evt_0001") is None
    assert client.post("/operator/reply", json=body).status_code == 202
    assert channel.decision("evt_0001") == "BLOCK"
    assert client.post("/operator/reply", json=body).status_code == 403      # single use


def test_reports(client):
    r = client.get("/reports/evt_0001")
    assert r.status_code == 200 and r.text == "# report\n"
    assert client.get("/reports/evt_0002").status_code == 404
    assert client.get("/reports/..%2Fapi.py").status_code == 404
    assert client.get("/reports/evt_0001.md").status_code == 404


def test_stream_delivers_new_events_without_trajectory(store):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(create_app(store), host="127.0.0.1", port=port,
                                           log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        while not server.started:
            time.sleep(0.05)
        with httpx.stream("GET", f"http://127.0.0.1:{port}/stream", timeout=5) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            for _ in range(100):
                if store._subs:
                    break
                time.sleep(0.05)
            saved = store.save_event(EVENT)
            data = next(json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: "))
        assert data["id"] == saved["id"] and data["decision"] == "HOLD"
        assert "trajectory" not in data["prediction"]
        for _ in range(100):                         # disconnect unsubscribes
            if not store._subs:
                break
            time.sleep(0.05)
        assert not store._subs
    finally:
        server.should_exit = True
        t.join(5)


def test_dashboard_served_at_root_without_shadowing_routes(client, store):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert '<script src="app.js">' in r.text
    js = client.get("/app.js")
    assert js.status_code == 200 and 'new EventSource("/stream")' in js.text
    store.save_event(EVENT)
    assert client.get("/events").json()[0]["id"] == "evt_0001"   # API routes still win
    assert client.get("/nope.html").status_code == 404
