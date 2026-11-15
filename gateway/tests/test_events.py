"""Event store on its own, then the proxy writing to it (real software station)."""
import asyncio
import threading

import pytest

from gateway import policy
from gateway.events import EventStore, sqlite_path
from gateway.proxy import EXC_HOLD, Proxy
from sim import poke
from test_block import DOSER, PUMP, RATE, adu, fc6, fc16, send  # noqa: F401
from test_proxy import HOST, station  # noqa: F401 (fixture)

EVENT = {"src_ip": "203.0.113.7", "function": 6, "kind": "register", "address": 0,
         "name": "DOSER_RATE_PCT", "old_value": 10, "new_value": 100, "decision": "HOLD",
         "reasons": ["CHLORINE_HIGH in 11 min", "nobody on site"],
         "prediction": {"safe": False, "violations": [], "trajectory": [[0, 60.0, 1.0]]},
         "station": {"presence": False, "cabinet_open": False, "level_pct": 61.2,
                     "chlorine_mgl": 1.02}}


@pytest.fixture
def store():
    s = EventStore("sqlite:///:memory:")
    yield s
    s.close()


# ------------------------------------------------------------------ the store

def test_event_round_trips_with_readme_fields(store):
    saved = store.save_event(EVENT, traffic_id=7)
    assert saved["id"] == "evt_0001"
    got = store.get_event("evt_0001")
    assert {k: got[k] for k in EVENT} == EVENT
    assert got["traffic_id"] == 7
    assert got["ts"].endswith("Z") and len(got["ts"]) == 20     # 2026-11-14T03:12:09Z
    assert store.get_event("evt_9999") is None


def test_event_without_prediction_or_station(store):
    store.save_event({"src_ip": "10.0.0.9", "function": 8, "decision": "DENY",
                      "reasons": ["function code 8 is not allowed"]})
    got = store.get_event("evt_0001")
    assert got["prediction"] is None and got["station"] is None and got["address"] is None
    assert got["reasons"] == ["function code 8 is not allowed"]


def test_ids_continue_after_reopen(tmp_path):
    url = f"sqlite:///{tmp_path / 'e.db'}"
    a = EventStore(url)
    a.save_event(EVENT)
    a.save_event(EVENT)
    a.close()
    b = EventStore(url)
    assert b.save_event(EVENT)["id"] == "evt_0003"
    assert b.get_event("evt_0001")["name"] == "DOSER_RATE_PCT"
    b.close()


def test_url_parsing_and_unsupported_scheme(tmp_path):
    assert sqlite_path("sqlite:///backflow.db") == "backflow.db"
    assert sqlite_path("sqlite:////tmp/x.db") == "/tmp/x.db"
    assert sqlite_path("sqlite://") == ":memory:"
    with pytest.raises(ValueError, match="only sqlite"):
        EventStore("postgresql://u:p@host/db")


def test_query_traffic_filters_orders_and_limits(store):
    for i in range(5):
        store.save_traffic("10.0.0.1", "client->station", i, 3, 0, 1, None, b"\x00\x01")
    store.save_traffic("10.0.0.2", "client->station", 9, 6, 0, 1, [100], b"\x09")
    store.save_traffic("10.0.0.2", "gateway->client", 9, 0x86, values="exception 0x06")

    mine = store.query_traffic(src_ip="10.0.0.2")
    assert [r["direction"] for r in mine] == ["client->station", "gateway->client"]
    assert mine[0]["values"] == [100] and mine[0]["raw"] == "09"
    assert mine[1]["values"] == "exception 0x06" and mine[1]["raw"] is None

    last3 = store.query_traffic(src_ip="10.0.0.1", limit=3)
    assert [r["tid"] for r in last3] == [2, 3, 4]                # newest 3, oldest first
    assert len(store.query_traffic(minutes=1)) == 7
    assert store.query_traffic(src_ip="nobody") == []


def test_subscribers_each_get_every_event():
    async def go():
        s = EventStore("sqlite:///:memory:")
        a, b = s.subscribe(), s.subscribe()
        s.save_event(EVENT)
        s.save_event({**EVENT, "decision": "ALLOW"})
        got_a = [await asyncio.wait_for(a.get(), 1) for _ in range(2)]
        got_b = [await asyncio.wait_for(b.get(), 1) for _ in range(2)]
        assert [e["id"] for e in got_a] == [e["id"] for e in got_b] == ["evt_0001", "evt_0002"]
        s.unsubscribe(b)
        s.save_event(EVENT)
        assert (await asyncio.wait_for(a.get(), 1))["id"] == "evt_0003"
        assert b.empty()
    asyncio.run(go())


def test_full_queue_drops_oldest_not_newest():
    async def go():
        s = EventStore("sqlite:///:memory:")
        q = s.subscribe(maxsize=2)
        for _ in range(3):
            s.save_event(EVENT)
        await asyncio.sleep(0.05)
        assert [q.get_nowait()["id"], q.get_nowait()["id"]] == ["evt_0002", "evt_0003"]
    asyncio.run(go())


def test_save_event_from_a_worker_thread_reaches_the_loop_queue():
    async def go():
        s = EventStore("sqlite:///:memory:")
        q = s.subscribe()
        threading.Thread(target=s.save_event, args=(EVENT,)).start()
        assert (await asyncio.wait_for(q.get(), 2))["id"] == "evt_0001"
    asyncio.run(go())


# ------------------------------------------------------- proxy -> store

class Rig:
    def __init__(self, station, store):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.proxy = Proxy(HOST, station.port, host=HOST, port=0, connect_timeout=1.0, store=store)
        asyncio.run_coroutine_threadsafe(self.proxy.start(), self.loop).result(5)

    def close(self):
        asyncio.run_coroutine_threadsafe(self.proxy.close(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def rig(station, store):
    policy.reset_context()
    r = Rig(station, store)
    yield r
    r.close()


def test_held_write_is_one_event_with_reasons_and_one_traffic_row_per_frame(station, rig, store):
    poke.write("coil", PUMP, 1, HOST, rig.proxy.port)                 # scenario 0: allowed
    assert send(rig.proxy.port, fc6(RATE, 100)) == bytes([0x86, EXC_HOLD])   # scenario 1

    allow, hold = [store.get_event(f"evt_{i:04d}") for i in (1, 2)]
    assert store.get_event("evt_0003") is None                        # no other events

    assert (allow["decision"], allow["name"], allow["old_value"], allow["new_value"]) == \
        ("ALLOW", "PUMP_CMD", 0, 1)
    assert allow["kind"] == "coil" and allow["function"] == 5

    assert hold["decision"] == "HOLD" and hold["src_ip"] == HOST
    assert (hold["function"], hold["kind"], hold["address"], hold["name"]) == \
        (6, "register", 0, "DOSER_RATE_PCT")
    assert (hold["old_value"], hold["new_value"]) == (10, 100)
    assert any("CHLORINE_HIGH" in r for r in hold["reasons"])
    assert hold["prediction"]["safe"] is False
    assert any(v["code"] == "CHLORINE_HIGH" for v in hold["prediction"]["violations"])
    assert len(hold["prediction"]["trajectory"]) > 1
    assert set(hold["station"]) == {"presence", "cabinet_open", "level_pct", "chlorine_mgl"}
    assert hold["station"]["level_pct"] == 60.0 and hold["station"]["chlorine_mgl"] == 1.0

    # one traffic row per frame, and the event points at the request frame
    rows = store.query_traffic(src_ip=HOST)
    request = next(r for r in rows if r["id"] == hold["traffic_id"])
    assert request["direction"] == "client->station" and request["fc"] == 6
    assert request["raw"] == adu(fc6(RATE, 100)).hex()
    assert request["values"] == [100]
    c2s = [r for r in rows if r["direction"] == "client->station"]
    g2c = [r for r in rows if r["direction"] == "gateway->client"]
    s2c = [r for r in rows if r["direction"] == "station->client"]
    assert len(c2s) == 2 and len(g2c) == 1 and len(s2c) == 1          # 2 writes sent, 1 held, 1 relayed
    assert g2c[0]["fc"] == 0x86 and g2c[0]["values"] == "exception 0x06"
    assert g2c[0]["raw"] == bytes([0, 1, 0, 0, 0, 3, 1, 0x86, EXC_HOLD]).hex()
    assert on_station(station, RATE) == 10                            # and it never landed


def on_station(station, addr):
    return poke.read_all(HOST, station.port)[("holding_register", addr)]


def test_every_write_in_a_frame_gets_an_event_and_cites_the_same_frame(station, rig, store):
    assert send(rig.proxy.port, fc16(RATE, [20, 990, 450])) == bytes([0x90, EXC_HOLD])
    evs = [store.get_event(f"evt_{i:04d}") for i in (1, 2, 3)]
    assert store.get_event("evt_0004") is None
    assert [e["name"] for e in evs] == ["DOSER_RATE_PCT", "LEVEL_HIGH_SP", "LEVEL_LOW_SP"]
    assert {e["decision"] for e in evs} == {"HOLD"}                   # the whole frame is held
    assert len({e["traffic_id"] for e in evs}) == 1
    assert any("above max" in r for r in evs[1]["reasons"])
    assert any("because another write in it was HOLD" in r for r in evs[0]["reasons"])
    assert [e["old_value"] for e in evs] == [10, 850, 400]


def test_unknown_function_code_is_a_deny_event(station, rig, store):
    assert send(rig.proxy.port, bytes([8, 0, 0, 0, 0]))[:1] == bytes([0x88])
    ev = store.get_event("evt_0001")
    assert (ev["decision"], ev["function"], ev["kind"], ev["address"], ev["name"]) == \
        ("DENY", 8, None, None, None)
    assert ev["reasons"] == ["function code 8 is not allowed"]
    assert store.get_event(ev["id"])["traffic_id"] is not None


def test_twin_only_write_is_a_deny_event(station, rig, store):
    assert send(rig.proxy.port, fc6(100, 999))[0] == 0x86
    ev = store.get_event("evt_0001")
    assert ev["decision"] == "DENY" and ev["name"] == "TANK_LEVEL_PCT"
    assert any("twin only" in r for r in ev["reasons"])


def test_reads_make_traffic_rows_but_no_events(station, rig, store):
    poke.read_all(HOST, rig.proxy.port)
    assert store.get_event("evt_0001") is None
    rows = store.query_traffic(src_ip=HOST)
    assert rows and {r["fc"] for r in rows} >= {1, 2, 3, 4}


def test_subscriber_sees_the_event_as_it_happens(station, rig, store):
    got = []
    ready = threading.Event()

    def consume():
        async def go():
            q = store.subscribe()
            ready.set()
            got.append(await asyncio.wait_for(q.get(), 5))
        asyncio.run(go())

    t = threading.Thread(target=consume)
    t.start()
    assert ready.wait(2)
    poke.write("coil", PUMP, 1, HOST, rig.proxy.port)
    t.join(6)
    assert got and got[0]["id"] == "evt_0001" and got[0]["decision"] == "ALLOW"


class BrokenStore(EventStore):
    def save_event(self, *a, **k):
        raise sqlite_error()

    def save_traffic(self, *a, **k):
        raise sqlite_error()


def sqlite_error():
    import sqlite3
    return sqlite3.OperationalError("disk full")


def test_storage_failure_does_not_change_enforcement(station):
    policy.reset_context()
    r = Rig(station, BrokenStore("sqlite:///:memory:"))
    try:
        poke.write("coil", PUMP, 1, HOST, r.proxy.port)               # still allowed
        assert poke.read_all(HOST, station.port)[("coil", PUMP)] == 1
        assert send(r.proxy.port, fc6(RATE, 100)) == bytes([0x86, EXC_HOLD])   # still held
        assert on_station(station, RATE) == 10
    finally:
        r.close()


def test_malformed_write_frame_is_stored_as_traffic_and_a_deny_event(station, rig, store):
    """Review finding 10: a write frame that did not decode closed the connection and left
    nothing in the database."""
    import socket
    import struct
    from gateway.proxy import EXC_DENY
    from test_proxy import recv_frame
    bad = struct.pack(">BHHB", 16, RATE, 2, 4) + struct.pack(">H", 100)   # says 2 registers, carries 1
    with socket.create_connection((HOST, rig.proxy.port), timeout=5) as s:
        s.sendall(adu(bad, tid=9))
        assert recv_frame(s)[7:] == bytes([0x90, EXC_DENY])
        assert s.recv(16) == b""                                  # then the gateway closes
    (e,) = store.list_events()
    assert e["decision"] == "DENY" and e["src_ip"] == HOST and e["function"] == 16
    assert e["reasons"][0].startswith("malformed write frame: fc 16: byte count")
    rows = store.query_traffic()
    assert rows[0]["id"] == e["traffic_id"] and rows[0]["raw"] == adu(bad, tid=9).hex()
    assert on_station(station, RATE) == 10
