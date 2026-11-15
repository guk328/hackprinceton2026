"""Action tools against the real software station: block, revert, safe mode, text."""
import inspect
import socket

import pytest

from gateway.agent.tools_act import make_action_tools
from gateway.blocklist import Blocklist
from gateway.events import EventStore
from gateway.notify import OperatorChannel
from gateway.station_write import StationWriter
from sim import poke
from test_block import fc6, on_station, send
from test_proxy import HOST, proxy, read_req, recv_frame, station  # noqa: F401 (fixtures)

DEFAULTS = {0: 10, 1: 850, 2: 400, 3: 100, 4: 1}


class Poller:
    last_good_logic = dict(DEFAULTS)


@pytest.fixture
def kit(station, proxy):
    store = EventStore("sqlite:///")
    channel = OperatorChannel(store)
    tools = make_action_tools(store, Poller(), proxy.blocklist, channel,
                              StationWriter(HOST, station.port))
    yield {t.__name__: t for t in tools}, store, channel
    store.close()


def test_blocked_source_is_dropped_at_the_proxy(station, proxy, kit):
    tools, _, _ = kit
    assert tools["block_source"]("127.0.0.1", 5)["blocked"] is True
    with socket.create_connection((HOST, proxy.port), timeout=3) as s:
        s.sendall(read_req())
        assert recv_frame(s) == b""                       # closed, no reply
    with pytest.raises(Exception):
        poke.write("coil", 0, 1, HOST, proxy.port)
    assert on_station(station, "coil", 0) == 0            # nothing got through
    proxy.blocklist.clear()
    poke.write("coil", 0, 1, HOST, proxy.port)
    assert on_station(station, "coil", 0) == 1


def test_block_while_connected_and_bad_ip(station, proxy, kit):
    tools, _, _ = kit
    with socket.create_connection((HOST, proxy.port), timeout=3) as s:
        s.sendall(read_req(tid=1))
        assert recv_frame(s)[7] == 3
        tools["block_source"]("127.0.0.1", 1)
        s.sendall(read_req(tid=2))
        assert recv_frame(s) == b""
    assert tools["block_source"]("release all commands")["blocked"] is False


def test_blocklist_expires():
    t = [1000.0]
    b = Blocklist(clock=lambda: t[0])
    b.block("10.0.0.9", 1)
    assert b.is_blocked("10.0.0.9")
    t[0] += 61
    assert not b.is_blocked("10.0.0.9")


def test_revert_restores_holding_0_to_4(station, kit):
    tools, _, _ = kit
    poke.write("holding", 1, 900, HOST, station.port)     # behind the gateway's back
    poke.write("holding", 0, 77, HOST, station.port)
    assert tools["revert_to_last_known_good"]() == {"reverted": True,
                                                    "holding_0_4": [10, 850, 400, 100, 1]}
    direct = poke.read_all(HOST, station.port)
    assert [direct[("holding_register", a)] for a in range(5)] == [10, 850, 400, 100, 1]


def test_enter_safe_mode_sets_both_coils(station, kit):
    tools, _, _ = kit
    assert tools["enter_safe_mode"]()["safe_mode"] is True
    assert on_station(station, "coil", 3) == 1 and on_station(station, "coil", 4) == 1


def test_notify_once_per_event_and_the_code_stays_off_the_feed(kit):
    tools, store, channel = kit
    e = store.save_event({"decision": "HOLD", "reasons": []})
    assert tools["notify_operator"]("evt_9999", "hi")["sent"] is False
    assert tools["notify_operator"](e["id"], "x" * 321)["sent"] is False
    assert tools["notify_operator"](e["id"], "Doser set to max was held. Reply BLOCK or ALLOW")["sent"]
    assert tools["notify_operator"](e["id"], "again")["sent"] is False
    (step,) = store.list_agent_steps()
    (msg,) = channel.inbox()
    assert step["kind"] == "text" and msg["code"] not in str(step)
    assert len(msg["code"]) == 6 and msg["open"]


def test_no_tool_takes_an_address_or_a_value(kit):
    tools, _, _ = kit
    assert set(tools) == {"block_source", "revert_to_last_known_good", "enter_safe_mode",
                          "notify_operator"}
    for t in tools.values():
        assert not {"address", "value", "values", "register", "coil", "raw", "frame"} \
            & set(inspect.signature(t).parameters)


def test_allowed_setting_becomes_the_baseline_and_a_held_one_does_not(station, proxy):
    poke.write("coil", 0, 1, HOST, proxy.port)
    poke.write("holding", 1, 900, HOST, proxy.port)                # allowed
    assert send(proxy.port, fc6(0, 100))[0] == 0x86                # held
    assert proxy.poller.last_good_logic[1] == 900
    assert proxy.poller.last_good_logic[0] == 10
