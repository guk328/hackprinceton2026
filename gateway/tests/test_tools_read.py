"""Agent read tools: small JSON-able output with ids, and provably no writes to the station."""
import asyncio
import dataclasses
import inspect
import json
import threading
import time
from pathlib import Path

import pytest
from pymodbus.client import AsyncModbusTcpClient, ModbusTcpClient

from gateway.agent import tools_read
from gateway.agent.tools_read import MAX_TRAFFIC_ROWS, logic_hash, make_read_tools
from gateway.events import EventStore
from gateway.station import StationPoller
from sim import poke
from test_events import EVENT
from test_policy import snap
from test_proxy import HOST, station  # noqa: F401 (fixture)

DEFAULT_LOGIC = {0: 10, 1: 850, 2: 400, 3: 100, 4: 1}


class FakePoller:
    def __init__(self, snapshot, last_good=None):
        self.snapshot = snapshot
        self.last_good_logic = dict(last_good or DEFAULT_LOGIC)


@pytest.fixture
def store():
    s = EventStore("sqlite:///:memory:")
    yield s
    s.close()


def tools_for(store, poller):
    return {f.__name__: f for f in make_read_tools(store, poller)}


@pytest.fixture
def tools(store):
    s = dataclasses.replace(snap(), ts=time.time())
    return tools_for(store, FakePoller(s))


def small(obj, kb=8):
    text = json.dumps(obj)          # JSON-able, and small
    assert len(text) < kb * 1024, len(text)
    return obj


def test_every_tool_has_hints_and_a_docstring(tools):
    assert set(tools) == {"get_event", "get_recent_traffic", "read_station", "run_physics",
                          "diff_logic_baseline"}
    for f in tools.values():
        assert inspect.getdoc(f) and len(inspect.getdoc(f)) > 40
        sig = inspect.signature(f)
        assert sig.return_annotation is dict
        assert all(p.annotation is not inspect.Parameter.empty for p in sig.parameters.values())


def test_get_event_summarises_prediction_without_trajectory(tools, store):
    traj = [[t, 60.0, 1.0 + t / 10] for t in range(3000)]           # ~88 KB worth
    pred = {"safe": False, "trajectory": traj, "violations": [
        {"code": "CHLORINE_HIGH", "minutes_until": 11.4, "value": 4.0, "limit": 4.0,
         "pre_existing": False, "message": "Chlorine exceeds 4.0 mg/L"}]}
    saved = store.save_event({**EVENT, "prediction": pred}, traffic_id=7)
    e = small(tools["get_event"](saved["id"]))
    assert e["id"] == saved["id"] and e["traffic_id"] == 7 and e["decision"] == "HOLD"
    assert "trajectory" not in e["prediction"]
    assert e["prediction"]["safe"] is False
    assert e["prediction"]["minutes_to_first_violation"] == 11.4
    assert e["prediction"]["violations"][0]["code"] == "CHLORINE_HIGH"
    ts = e["prediction"]["trajectory_summary"]
    assert ts["points"] == 3000 and ts["chlorine_mgl"]["max"] == pytest.approx(300.9)
    assert "error" in tools["get_event"]("evt_9999")
    assert "error" in tools["get_event"]("../etc/passwd")


def test_traffic_is_capped_and_carries_ids(tools, store):
    ids = [store.save_traffic("203.0.113.7", "in", i, 16, 0, 120, list(range(120)),
                              bytes(260)) for i in range(80)]
    store.save_traffic("198.51.100.1", "in", 1, 3, 0, 1, None, b"\x00")
    out = small(tools["get_recent_traffic"]("203.0.113.7", None, 1000), kb=32)
    assert out["count"] == MAX_TRAFFIC_ROWS and out["truncated"] is True
    assert [r["id"] for r in out["rows"]] == ids[-MAX_TRAFFIC_ROWS:]     # newest, oldest first
    assert out["first_id"] == ids[-MAX_TRAFFIC_ROWS] and out["last_id"] == ids[-1]
    assert all(r["src_ip"] == "203.0.113.7" for r in out["rows"])
    assert len(out["rows"][0]["values"]) == tools_read.MAX_VALUES
    assert out["rows"][0]["raw"].endswith("...")
    few = tools["get_recent_traffic"](limit=5)
    assert few["count"] == 5 and few["truncated"] is True
    assert "error" in tools["get_recent_traffic"](limit="lots")


def test_read_station_scales_units(tools):
    s = small(tools["read_station"]())
    assert s["connected"] is True and s["age_s"] < 5
    assert s["holding"]["TANK_LEVEL_PCT"] == 60.0 and s["holding"]["CHLORINE_MGL"] == 1.0
    assert s["holding"]["DOSER_RATE_PCT"] == 10 and s["input_registers"]["DEMAND_PCT"] == 40.0
    assert s["coils"]["PUMP_CMD"] is True and s["discrete_inputs"]["PRESENCE"] is False
    offline = tools_for(EventStore("sqlite:///:memory:"), FakePoller(None))
    assert offline["read_station"]()["connected"] is False


def test_run_physics_overdose_is_unsafe_and_summarised(tools):
    p = small(tools["run_physics"](0, 100))
    assert p["name"] == "DOSER_RATE_PCT" and p["safe"] is False
    assert "CHLORINE_HIGH" in [v["code"] for v in p["violations"]]
    assert p["minutes_to_first_violation"] > 0
    assert "trajectory" not in p and "trajectory_summary" in p
    ok = tools["run_physics"](1, 860)
    assert ok["safe"] is True and ok["minutes_to_first_violation"] is None
    assert "error" in tools["run_physics"]("x", 1)
    stale = dataclasses.replace(snap(), connected=False)
    assert "error" in tools_for(EventStore("sqlite:///:memory:"),
                                FakePoller(stale))["run_physics"](0, 100)


def test_diff_logic_baseline(store):
    s = snap()
    s = dataclasses.replace(s, ts=time.time(), holding={**s.holding, "DOSER_RATE_PCT": 80},
                            input_registers={**s.input_registers,
                                             "LOGIC_HASH": logic_hash({**DEFAULT_LOGIC, 0: 80})})
    d = small(tools_for(store, FakePoller(s))["diff_logic_baseline"]())
    assert d["changed"] == ["DOSER_RATE_PCT"]
    row = d["registers"][0]
    assert row["current"] == 80 and row["last_known_good"] == 10
    assert d["logic_hash"]["station"] == d["logic_hash"]["of_current"]
    assert d["logic_hash"]["matches_last_known_good"] is False


def test_module_has_no_direct_station_or_database_access():
    src = Path(tools_read.__file__).read_text()
    for banned in ("pymodbus", "sqlite3", "sim.poke", "Link(", ".write(", "write_coil",
                   "write_register", "accept_logic", "refresh(", "save_event", "save_traffic"):
        assert banned not in src, banned


def test_tools_never_write_to_the_real_station(station, store, monkeypatch):  # noqa: F811
    """Every tool against the real sim + a real StationPoller: no Modbus write is even
    attempted, and the station's writable registers are unchanged afterwards."""
    attempts = []

    def spy(name):
        def refuse(self, *a, **kw):
            attempts.append(name)
            raise AssertionError(f"{name} called by a read tool")
        return refuse

    for cls in (AsyncModbusTcpClient, ModbusTcpClient):
        for name in ("write_coil", "write_coils", "write_register", "write_registers",
                     "mask_write_register", "readwrite_registers"):
            monkeypatch.setattr(cls, name, spy(name))

    def writable(values):
        return {k: v for k, v in values.items()
                if k[0] == "coil" and k[1] != 0                         # PUMP_CMD: auto
                or k[0] == "holding_register" and k[1] in DEFAULT_LOGIC}

    before = writable(poke.read_all(HOST, station.port))
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    async def start():
        p = StationPoller(HOST, station.port, interval=0.2, timeout=0.5)
        await p.start()
        return p
    poller = asyncio.run_coroutine_threadsafe(start(), loop).result(5)
    try:
        end = time.time() + 10
        while not poller.snapshot.connected and time.time() < end:
            time.sleep(0.05)
        assert poller.snapshot.connected
        saved = store.save_event(EVENT)
        store.save_traffic("203.0.113.7", "in", 1, 6, 0, 1, [100], b"\x00\x01")
        t = tools_for(store, poller)
        results = [t["get_event"](saved["id"]), t["get_recent_traffic"](), t["read_station"](),
                   t["diff_logic_baseline"]()]
        for addr, value, kind in [(0, 100, "register"), (1, 1000, "register"),
                                  (3, 0, "coil"), (0, 0, "coil"), (2, 0, "coil")]:
            results.append(t["run_physics"](addr, value, kind))
        for r in results:
            small(r)
        d = t["diff_logic_baseline"]()
        assert d["changed"] == [] and d["logic_hash"]["matches_last_known_good"] is True
        time.sleep(0.6)                                   # a few station ticks
    finally:
        asyncio.run_coroutine_threadsafe(poller.close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
    monkeypatch.undo()                                    # let poke read normally
    assert attempts == []
    assert writable(poke.read_all(HOST, station.port)) == before
