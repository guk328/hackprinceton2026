"""Admin login and strict request bodies: nothing malformed or anonymous gets through."""
import pytest
from fastapi.testclient import TestClient

from gateway.api import create_app
from gateway.auth import COOKIE, MAX_FAILS, SESSION_S, Auth
from gateway.events import EventStore
from gateway.notify import OperatorChannel


@pytest.fixture
def kit(tmp_path):
    store = EventStore("sqlite:///:memory:")
    store.save_event({"decision": "HOLD", "reasons": []})
    channel = OperatorChannel(store)
    channel.send("evt_0001", "Held. Reply BLOCK or ALLOW")
    app = create_app(store, channel=channel, auth=Auth("s3cret"), reports_dir=tmp_path,
                     dashboard_dir=tmp_path / "none")
    yield TestClient(app), channel
    store.close()


def reply_body(channel, **over):
    return {"reply": "ALLOW", "event_id": "evt_0001", "code": channel.inbox()[0]["code"], **over}


def test_anonymous_can_watch_but_not_touch_the_phone(kit):
    client, channel = kit
    assert client.get("/events").status_code == 200
    assert client.get("/agent/log").status_code == 200
    assert client.get("/me").json() == {"admin": False}
    assert client.get("/operator/inbox").status_code == 401
    assert client.post("/operator/reply", json=reply_body(channel)).status_code == 401
    assert channel.decision("evt_0001") is None
    code = channel.inbox()[0]["code"]
    assert code not in client.get("/agent/log").text        # the code is only on the phone


def test_login_then_reply(kit):
    client, channel = kit
    assert client.post("/login", json={"password": "nope"}).status_code == 401
    r = client.post("/login", json={"password": "s3cret"})
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert client.get("/me").json() == {"admin": True}
    assert client.post("/operator/reply", json=reply_body(channel)).status_code == 202
    assert channel.decision("evt_0001") == "ALLOW"
    client.post("/logout")
    assert client.get("/operator/inbox").status_code == 401


@pytest.mark.parametrize("bad", [
    {"extra": 1}, {"code": 123456}, {"code": "12345"}, {"code": "1234567"}, {"code": "12345a"},
    {"reply": "allow"}, {"reply": ["ALLOW"]}, {"event_id": "evt_1; DROP TABLE events"},
    {"event_id": "../../etc/passwd"}, {"event_id": None}, {"event_id": 1},
])
def test_malformed_reply_is_422_and_changes_nothing(kit, bad):
    client, channel = kit
    client.post("/login", json={"password": "s3cret"})
    assert client.post("/operator/reply", json=reply_body(channel, **bad)).status_code == 422
    assert channel.decision("evt_0001") is None and channel.inbox()[0]["open"]
    assert channel.reply("evt_0001", "ALLOW", channel.inbox()[0]["code"])[0]   # no attempt was used up


def test_malformed_login_and_ids(kit):
    client, _ = kit
    assert client.post("/login", json={"password": "s3cret", "admin": True}).status_code == 422
    assert client.post("/login", json={"password": 5}).status_code == 422
    assert client.post("/login", content=b"password=s3cret",
                       headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 422
    assert client.post("/operator/reply", content=b"{not json",
                       headers={"content-type": "application/json"}).status_code in (401, 422)
    assert client.get("/events/evt_1%27%20OR%201").status_code == 422
    assert client.get("/events?limit=100000").status_code == 422


def test_forged_and_expired_cookies(kit):
    client, _ = kit
    t = [1000.0]
    auth = Auth("pw", clock=lambda: t[0])
    token = auth.token()
    assert auth.valid(token)
    assert not auth.valid(token[:-1] + ("0" if token[-1] != "0" else "1"))
    assert not auth.valid(f"{int(t[0]) + 10**6}.{token.split('.')[1]}")     # stretched expiry
    assert not auth.valid("") and not auth.valid("abc") and not auth.valid(None)
    assert not Auth("pw").valid(token)                                      # another key
    t[0] += SESSION_S + 1
    assert not auth.valid(token)
    client.cookies.set(COOKIE, "9999999999.deadbeef")
    assert client.get("/operator/inbox").status_code == 401


def test_password_guessing_is_rate_limited(kit):
    client, _ = kit
    for _ in range(MAX_FAILS):
        assert client.post("/login", json={"password": "guess"}).status_code == 401
    assert client.post("/login", json={"password": "s3cret"}).status_code == 429
