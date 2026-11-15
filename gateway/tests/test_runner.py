"""Runner end to end on the software station, model mocked: attack -> hold -> agent ->
text -> verified reply. And the operator is still told when the model is unusable."""
import asyncio
import threading
import time

import pytest
from langchain_core.messages import AIMessage

from fake_model import FakeModel, call
from gateway.agent.budget import Budget
from gateway.agent.build import INTERRUPT_ON, build_agent
from gateway.agent.runner import Runner, template_text
from gateway.agent.tools_act import make_action_tools, release
from gateway.agent.tools_read import make_read_tools
from gateway.events import EventStore
from gateway.notify import OperatorChannel
from gateway.proxy import EXC_HOLD, Proxy
from gateway.station_write import StationWriter
from sim import poke
from test_block import PUMP, RATE, fc6, on_station, send
from test_proxy import HOST, station  # noqa: F401 (fixture)

TEXT = "Someone tried to set the chlorine doser to max. Held. Reply BLOCK or ALLOW"


class Gw:
    def __init__(self, station, script=None, budget_ceiling=None, time_limit_s=20, delay_s=0.0,
                 debounce_s=0.0):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.store = EventStore("sqlite:///")
        self.proxy = Proxy(HOST, station.port, host=HOST, port=0, connect_timeout=1.0, store=self.store)
        self.on(self.proxy.start())
        self.channel = OperatorChannel(self.store)
        self.model = FakeModel(script=list(script or []), prompts=[], tools_seen=[], delay_s=delay_s)
        self.budget = Budget(self.store, ceiling_usd=budget_ceiling)
        refresh = lambda: self.on(self.proxy.poller.refresh())  # noqa: E731
        writer = StationWriter(HOST, station.port)
        kit = dict(held=self.proxy.held, channel=self.channel, writer=writer, refresh=refresh,
                   store=self.store, poller=self.proxy.poller)
        tools = make_read_tools(self.store, self.proxy.poller) + make_action_tools(
            self.store, self.proxy.poller, self.proxy.blocklist, self.channel, writer,
            held=self.proxy.held, refresh=refresh)
        self.tools = tools
        self.runner = Runner(
            self.store, self.channel, self.proxy.blocklist, budget=self.budget, held=self.proxy.held,
            make_agent=(lambda: build_agent(tools, self.model, budget=self.budget,
                                            interrupt_on=INTERRUPT_ON)) if script is not None else None,
            release=lambda eid: release(eid, **kit), time_limit_s=time_limit_s, debounce_s=debounce_s)
        self.on(self.runner.start())

    def on(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def close(self):
        self.on(self.runner.close())
        self.on(self.proxy.close())
        self.loop.call_soon_threadsafe(self.loop.stop)

    def attack(self, value=100):
        poke.write("coil", PUMP, 1, HOST, self.proxy.port)
        assert send(self.proxy.port, fc6(RATE, value)) == bytes([0x86, EXC_HOLD])
        return self.store.list_events(1)[0]["id"]

    def wait(self, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def kinds(self):
        return [s["kind"] for s in self.store.list_agent_steps()]

    def code(self, eid):
        return next(m["code"] for m in self.channel.inbox() if m["event_id"] == eid)


@pytest.fixture
def make(station):
    made = []

    def _make(**kw):
        made.append(Gw(station, **kw))
        return made[-1]
    yield _make
    for g in made:
        g.close()


def test_scenario_1_end_to_end_with_block(station, make):
    g = make(script=[
        call("get_event", _id="a", event_id="evt_0002"),
        call("run_physics", _id="b", address=0, value=100),
        call("notify_operator", _id="c", event_id="evt_0002", text=TEXT),
        AIMessage(content="MALICIOUS, high confidence. Evidence: evt_0002, traffic #3."),
        AIMessage(content="Final: source blocked on the operator's order. evt_0002."),
    ])
    eid = g.attack()
    assert eid == "evt_0002"
    assert g.wait(lambda: "verdict" in g.kinds())
    (msg,) = g.channel.inbox()
    assert msg["text"] == TEXT and msg["open"]
    assert on_station(station, "holding_register", RATE) == 10
    assert g.channel.reply(eid, "BLOCK", g.code(eid))[0]
    assert g.wait(lambda: g.proxy.blocklist.is_blocked(HOST))
    assert g.wait(lambda: g.kinds().count("verdict") == 2)
    assert g.proxy.held.all() == []
    assert "operator replied BLOCK" in str(g.model.prompts[-1][-1].content)
    assert "cost" in g.kinds()
    with pytest.raises(Exception):                      # the source really is cut off
        poke.write("coil", PUMP, 0, HOST, g.proxy.port)


def test_scenario_5_allow_after_the_agent_finished_releases_through_the_interrupt(station, make):
    g = make(script=[
        call("notify_operator", _id="c", event_id="evt_0002", text=TEXT),
        AIMessage(content="Probably legitimate. evt_0002."),
        call("release_held_command", _id="d", event_id="evt_0002"),
        AIMessage(content="Released after the operator's ALLOW. evt_0002."),
    ])
    eid = g.attack(40)
    assert g.wait(lambda: "verdict" in g.kinds())
    assert g.channel.reply(eid, "ALLOW", g.code(eid))[0]
    assert g.wait(lambda: on_station(station, "holding_register", RATE) == 40)
    assert g.wait(lambda: "released" in g.kinds() and g.kinds().count("verdict") == 2)


def test_model_down_still_texts_and_allow_cannot_release_scenario_1(station, make):
    g = make(script=[ConnectionError("model API is down")])
    eid = g.attack()
    assert g.wait(lambda: g.channel.inbox())
    (msg,) = g.channel.inbox()
    assert "DOSER_RATE_PCT 10 -> 100" in msg["text"] and msg["text"].endswith("Reply BLOCK or ALLOW")
    assert len(msg["text"]) <= 320
    assert any("Agent not used" in s["text"] for s in g.store.list_agent_steps())
    assert g.channel.reply(eid, "ALLOW", g.code(eid))[0]
    assert g.wait(lambda: "refused" in g.kinds())
    assert on_station(station, "holding_register", RATE) == 10


def test_no_model_at_all_and_allow_releases_scenario_5_by_plain_code(station, make):
    g = make(script=None)
    eid = g.attack(40)
    assert g.wait(lambda: g.channel.inbox())
    assert g.model.prompts == []
    assert g.channel.reply(eid, "ALLOW", g.code(eid))[0]
    assert g.wait(lambda: on_station(station, "holding_register", RATE) == 40)


def test_over_the_spend_ceiling_no_model_call_is_made(station, make):
    g = make(script=[AIMessage(content="should never run")], budget_ceiling=0.0)
    g.attack()
    assert g.wait(lambda: g.channel.inbox())
    assert g.model.prompts == [] and g.store.total_spend() == 0
    assert any("ceiling" in s["text"] for s in g.store.list_agent_steps())


def test_time_limit_sends_the_standard_text(station, make):
    g = make(script=[AIMessage(content="too slow")], delay_s=1.5, time_limit_s=0.3)
    g.attack()
    assert g.wait(lambda: g.channel.inbox(), timeout=5)
    assert any("time limit" in s["text"] for s in g.store.list_agent_steps())


def test_burst_from_one_source_is_one_incident(station, make):
    g = make(script=None, debounce_s=30)
    g.attack()
    for v in (99, 98, 97):
        assert send(g.proxy.port, fc6(RATE, v)) == bytes([0x86, EXC_HOLD])
    assert g.wait(lambda: g.kinds().count("status") >= 4)
    time.sleep(0.3)
    assert len(g.channel.inbox()) == 1


def test_template_text_is_short_and_ends_with_the_question():
    t = template_text({"src_ip": "203.0.113.7", "name": "DOSER_RATE_PCT", "old_value": 10,
                       "new_value": 100, "decision": "HOLD", "reasons": ["x" * 500]})
    assert len(t) <= 320 and t.endswith("Reply BLOCK or ALLOW")
