"""Demo reset: the same scenario twice gives the same clean run."""
from fastapi.testclient import TestClient

from gateway import policy
from gateway.api import create_app
from gateway.auth import Auth
from gateway.reset import reset
from gateway.station_write import StationWriter
from sim import poke
from test_block import RATE, on_station
from test_proxy import HOST, station  # noqa: F401 (fixture)
from test_runner import Gw


def run_once(g):
    eid = g.attack()                                           # scenario 0 then scenario 1
    assert g.wait(lambda: g.channel.inbox())
    assert g.channel.reply(eid, "BLOCK", g.code(eid))[0]
    assert g.wait(lambda: g.proxy.blocklist.is_blocked(HOST))
    assert g.wait(lambda: "Held command discarded" in [s["text"] for s in g.store.list_agent_steps()])
    return ([(e["id"], e["decision"], e["name"], e["reasons"]) for e in g.store.list_events()],
            [(s["event_id"], s["kind"], s["text"]) for s in g.store.list_agent_steps()],
            [m["text"] for m in g.channel.inbox()])


def test_two_runs_in_a_row_are_identical(station, tmp_path):
    policy.reset_context()      # other tests' writes from 127.0.0.1 must not count as a burst here
    g = Gw(station, script=None)
    writer = StationWriter(HOST, station.port)
    (tmp_path / "evt_0002.md").write_text("old report")

    def do_reset():
        return reset(g.store, blocklist=g.proxy.blocklist, held=g.proxy.held, channel=g.channel,
                     runner=g.runner, poller=g.proxy.poller, writer=writer, reports_dir=tmp_path)
    try:
        first = run_once(g)
        assert [e[:2] for e in first[0]] == [("evt_0002", "HOLD"), ("evt_0001", "ALLOW")]
        poke.write("holding", 1, 700, HOST, station.port)      # drift the station too
        g.store.add_spend("evt_0002", 10, 10, 0.25)
        assert g.on(_async(do_reset)) == {"reset": True, "station": "defaults restored"}
        assert g.store.list_events() == [] and g.store.list_agent_steps() == []
        assert g.channel.inbox() == [] and g.proxy.held.all() == []
        assert not g.proxy.blocklist.is_blocked(HOST) and not list(tmp_path.glob("*.md"))
        assert g.store.total_spend() == 0.25                   # money spent stays counted
        direct = poke.read_all(HOST, station.port)
        assert [direct[("holding_register", a)] for a in range(5)] == [10, 850, 400, 100, 1]
        assert [direct[("coil", a)] for a in range(5)] == [0, 1, 1, 0, 0]
        assert g.proxy.poller.last_good_logic == {0: 10, 1: 850, 2: 400, 3: 100, 4: 1}
        assert run_once(g) == first
        assert on_station(station, "holding_register", RATE) == 10
    finally:
        g.close()


async def _async(fn):
    import asyncio
    return await asyncio.to_thread(fn)


def test_reset_route_is_admin_only(station):
    g = Gw(station, script=None)
    try:
        calls = []
        app = create_app(g.store, channel=g.channel, auth=Auth("pw"),
                         reset=lambda: calls.append(1) or {"reset": True})
        c = TestClient(app)
        assert c.post("/reset").status_code == 401 and calls == []
        c.post("/login", json={"password": "pw"})
        assert c.post("/reset").json() == {"reset": True} and calls == [1]
    finally:
        g.close()
