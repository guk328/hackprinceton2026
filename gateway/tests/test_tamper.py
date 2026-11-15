"""Scenario 4: settings changed behind the gateway raise LOGIC_TAMPER; the same change
through the gateway does not. With no model, the runner reverts by plain code."""
import pytest

from gateway.agent.runner import is_tamper
from gateway.proxy import TAMPER_SOURCE
from gateway.station import logic_hash
from gateway.station_write import StationWriter
from sim import poke
from test_block import on_station
from test_proxy import HOST, station  # noqa: F401 (fixture)
from test_runner import Gw


@pytest.fixture
def g(station):
    gw = Gw(station, script=None)
    gw.proxy.poller.interval = 0.15
    gw.proxy.poller.tamper_confirm_s = 0.4
    gw.on(gw.proxy.poller.start())
    assert gw.wait(lambda: gw.proxy.poller.last_good_logic)
    yield gw
    gw.close()


def tampers(g):
    return [e for e in g.store.list_events() if is_tamper(e)]


def test_write_behind_the_gateway_raises_one_tamper_event(station, g):
    poke.write("holding", 1, 900, HOST, station.port)          # straight to the station
    assert g.wait(lambda: tampers(g), timeout=5)
    import time
    time.sleep(0.8)
    (e,) = tampers(g)                                          # reported once, not every poll
    assert e["src_ip"] == TAMPER_SOURCE and e["decision"] == "DENY"
    assert (e["name"], e["old_value"], e["new_value"]) == ("LEVEL_HIGH_SP", 850, 900)
    assert "LEVEL_HIGH_SP 850 -> 900" in e["reasons"][0]
    assert str(logic_hash({0: 10, 1: 850, 2: 400, 3: 100, 4: 1})) in e["reasons"][1]


def test_same_write_through_the_gateway_is_not_tamper(station, g):
    poke.write("holding", 1, 900, HOST, g.proxy.port)          # allowed, through :1502
    import time
    time.sleep(1.5)
    assert on_station(station, "holding_register", 1) == 900
    assert tampers(g) == [] and g.proxy.poller.last_good_logic[1] == 900


def test_no_model_runner_reverts_and_texts(station, g):
    g.runner.revert = lambda: StationWriter(HOST, station.port).restore_logic(g.proxy.poller.last_good_logic)
    poke.write("holding", 0, 90, HOST, station.port)           # doser rate, behind the gateway
    assert g.wait(lambda: on_station(station, "holding_register", 0) == 10, timeout=6)
    assert g.wait(lambda: g.channel.inbox())
    assert "changed behind the gateway" in g.channel.inbox()[0]["text"]
    assert len(g.channel.inbox()[0]["text"]) <= 320
    eid = tampers(g)[0]["id"]
    assert g.channel.reply(eid, "BLOCK", g.code(eid))[0]       # nothing to block: no crash
    assert g.wait(lambda: any("No network source" in s["text"] for s in g.store.list_agent_steps()))
    import time
    time.sleep(1.0)
    assert len(tampers(g)) == 1                                # the revert itself is not a tamper


def test_second_different_tamper_is_reported_again(station, g):
    poke.write("holding", 1, 900, HOST, station.port)
    assert g.wait(lambda: len(tampers(g)) == 1, timeout=5)
    poke.write("holding", 2, 300, HOST, station.port)
    assert g.wait(lambda: len(tampers(g)) == 2, timeout=5)
