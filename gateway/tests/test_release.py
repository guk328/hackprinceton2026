"""Operator approval and release. Scenario 5 (safe, held for approval) can be released;
scenario 1 (unsafe) cannot, even with a valid one-time code. Asserts on the station."""
import asyncio

import pytest

from fake_model import FakeModel, call
from gateway.agent.build import INTERRUPT_ON, Session, build_agent
from gateway.agent.tools_act import make_action_tools
from gateway.events import EventStore
from gateway.notify import MAX_BAD_CODES, OperatorChannel
from gateway.proxy import EXC_HOLD, Proxy
from gateway.station_write import StationWriter
from sim import poke
from test_block import RATE, PUMP, fc6, on_station, send
from test_proxy import HOST, station  # noqa: F401 (fixture)
import threading


@pytest.fixture
def gw(station):
    """Proxy with an event store, plus the full action tool set."""
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    store = EventStore("sqlite:///")
    p = Proxy(HOST, station.port, host=HOST, port=0, connect_timeout=1.0, store=store)
    asyncio.run_coroutine_threadsafe(p.start(), loop).result(5)
    channel = OperatorChannel(store)
    refresh = lambda: asyncio.run_coroutine_threadsafe(p.poller.refresh(), loop).result(5)  # noqa: E731
    tools = make_action_tools(store, p.poller, p.blocklist, channel,
                              StationWriter(HOST, station.port), held=p.held, refresh=refresh)
    yield p, store, channel, tools
    asyncio.run_coroutine_threadsafe(p.close(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    store.close()


def hold(p, store, value):
    """Scenario 0 then a doser-rate write; returns the held event id."""
    poke.write("coil", PUMP, 1, HOST, p.port)
    assert send(p.port, fc6(RATE, value)) == bytes([0x86, EXC_HOLD])
    return store.list_events(1)[0]["id"]


def paused_session(store, tools, event_id):
    """Agent texts the operator, then asks to release: it must pause there."""
    model = FakeModel(prompts=[], tools_seen=[], script=[
        call("notify_operator", _id="n1", event_id=event_id,
             text="Doser rate change was held. Reply BLOCK or ALLOW"),
        call("release_held_command", _id="r1", event_id=event_id)])
    agent = build_agent(tools, model, interrupt_on=INTERRUPT_ON)
    s = Session(agent, event_id)
    assert s.start(store.get_event(event_id)) == "waiting"
    assert [a["name"] for a in s.pending] == ["release_held_command"]
    return s


def code_for(channel, event_id):
    return next(m["code"] for m in channel.inbox() if m["event_id"] == event_id)


def test_scenario_5_is_held_for_approval_then_released(station, gw):
    p, store, channel, tools = gw
    eid = hold(p, store, 40)
    e = store.get_event(eid)
    assert e["prediction"]["safe"] is True
    assert any("large step" in r for r in e["reasons"])
    s = paused_session(store, tools, eid)
    assert on_station(station, "holding_register", RATE) == 10          # paused, not sent
    assert channel.reply(eid, "ALLOW", code_for(channel, eid)) == (True, "accepted")
    assert s.resume(True) == "done"
    assert on_station(station, "holding_register", RATE) == 40
    assert p.held.all() == [] and p.poller.last_good_logic[0] == 40
    assert "released" in [x["kind"] for x in store.list_agent_steps()]


def test_scenario_1_is_never_released_even_with_a_valid_code(station, gw):
    p, store, channel, tools = gw
    eid = hold(p, store, 100)
    s = paused_session(store, tools, eid)
    assert channel.reply(eid, "ALLOW", code_for(channel, eid)) == (True, "accepted")
    assert s.resume(True) == "done"
    assert on_station(station, "holding_register", RATE) == 10
    assert p.held.all() == []                                           # and it is gone for good
    refused = [x for x in store.list_agent_steps() if x["kind"] == "refused"]
    assert refused and "CHLORINE_HIGH" in refused[0]["text"]


def test_block_reply_releases_nothing(station, gw):
    p, store, channel, tools = gw
    eid = hold(p, store, 40)
    s = paused_session(store, tools, eid)
    assert channel.reply(eid, "BLOCK", code_for(channel, eid))[0]
    assert s.resume(False, "operator replied BLOCK") == "done"
    assert on_station(station, "holding_register", RATE) == 10
    # and a later "approve" from the framework side has no operator ALLOW behind it
    release = {t.__name__: t for t in tools}["release_held_command"]
    assert release(eid)["released"] is False
    assert on_station(station, "holding_register", RATE) == 10


def test_framework_approval_without_an_operator_reply_releases_nothing(station, gw):
    p, store, channel, tools = gw
    eid = hold(p, store, 40)
    s = paused_session(store, tools, eid)
    assert s.resume(True) == "done"
    assert on_station(station, "holding_register", RATE) == 10


def test_wrong_reused_and_cross_event_codes(station, gw):
    p, store, channel, tools = gw
    a = hold(p, store, 40)
    assert send(p.port, fc6(RATE, 45)) == bytes([0x86, EXC_HOLD])
    b = store.list_events(1)[0]["id"]
    channel.send(a, "A? Reply BLOCK or ALLOW")
    channel.send(b, "B? Reply BLOCK or ALLOW")
    release = {t.__name__: t for t in tools}["release_held_command"]
    assert channel.reply(a, "ALLOW", "000000x")[0] is False
    assert channel.reply(a, "ALLOW", code_for(channel, b))[0] is False   # b's code, a's event
    assert channel.reply("evt_9999", "ALLOW", code_for(channel, a))[0] is False
    assert release(a)["released"] is False
    assert channel.reply(a, "ALLOW", code_for(channel, a))[0] is True
    assert channel.reply(a, "ALLOW", code_for(channel, a))[0] is False   # single use
    assert release(b)["released"] is False                               # a's ALLOW is not b's
    assert release(a)["released"] is True
    assert release(a)["released"] is False                               # nothing held any more
    assert on_station(station, "holding_register", RATE) == 40


def test_expired_code_and_too_many_wrong_codes():
    t = [0.0]
    ch = OperatorChannel(ttl_s=60, clock=lambda: t[0])
    ch.send("evt_0001", "q")
    code = ch.inbox()[0]["code"]
    t[0] = 61
    assert ch.reply("evt_0001", "ALLOW", code) == (False, "the code has expired")
    assert ch.take_approval("evt_0001") is False
    ch.send("evt_0002", "q")
    good = next(m["code"] for m in ch.inbox() if m["event_id"] == "evt_0002")
    for _ in range(MAX_BAD_CODES):
        assert ch.reply("evt_0002", "ALLOW", "x")[0] is False
    assert ch.reply("evt_0002", "ALLOW", good) == (False, "this question is void")
    assert ch.take_approval("evt_0002") is False
