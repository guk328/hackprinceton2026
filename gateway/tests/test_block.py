"""Enforcement: a write that is not ALLOW never reaches the station.
Every test asserts on the station's own register value, read directly."""
import socket
import struct

import pytest

from gateway.proxy import EXC_DENY, EXC_HOLD
from sim import poke
from test_proxy import HOST, proxy, read_req, recv_frame, station  # noqa: F401 (fixtures)

RATE, HIGH_SP, LOW_SP = 0, 1, 2          # holding registers
PUMP, DOSER = 0, 1                       # coils


def adu(pdu: bytes, tid=1) -> bytes:
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, 1) + pdu


def send(port, pdu: bytes) -> bytes:
    """One request through the proxy; returns the reply PDU."""
    with socket.create_connection((HOST, port), timeout=5) as s:
        s.sendall(adu(pdu))
        return recv_frame(s)[7:]


def fc6(addr, value):
    return struct.pack(">BHH", 6, addr, value)


def fc16(addr, values):
    return struct.pack(f">BHHB{len(values)}H", 16, addr, len(values), 2 * len(values), *values)


def on_station(station, kind, addr):
    return poke.read_all(HOST, station.port)[(kind, addr)]


def test_scenario_1_is_held_and_never_reaches_the_station(station, proxy):
    poke.write("coil", PUMP, 1, HOST, proxy.port)             # scenario 0: allowed
    assert on_station(station, "coil", PUMP) == 1

    assert send(proxy.port, fc6(RATE, 100)) == bytes([0x86, EXC_HOLD])
    poke.write("coil", DOSER, 1, HOST, proxy.port)            # harmless at rate 10
    assert on_station(station, "holding_register", RATE) == 10

    (held,) = proxy.held.all()
    assert held.frame.raw == adu(fc6(RATE, 100))
    assert held.src_ip == HOST
    assert [w.name for w in held.writes] == ["DOSER_RATE_PCT"]
    assert any("CHLORINE_HIGH" in r for d in held.decisions for r in d.reasons)
    assert proxy.held.get(held.id) is held


def test_client_library_sees_the_hold_as_an_error(station, proxy):
    poke.write("coil", PUMP, 1, HOST, proxy.port)
    with pytest.raises(RuntimeError):
        poke.write("holding", RATE, 100, HOST, proxy.port)
    assert on_station(station, "holding_register", RATE) == 10


def test_fc16_with_one_unsafe_register_holds_the_whole_frame(station, proxy):
    reply = send(proxy.port, fc16(RATE, [20, 990, 450]))      # 990 is above LEVEL_HIGH_SP max
    assert reply == bytes([0x90, EXC_HOLD])
    direct = poke.read_all(HOST, station.port)
    assert [direct[("holding_register", a)] for a in (RATE, HIGH_SP, LOW_SP)] == [10, 850, 400]
    (held,) = proxy.held.all()
    assert len(held.writes) == 3 and len(held.decisions) == 3


def test_fc16_all_safe_goes_through(station, proxy):
    assert send(proxy.port, fc16(RATE, [20, 900, 450]))[0] == 16
    direct = poke.read_all(HOST, station.port)
    assert [direct[("holding_register", a)] for a in (RATE, HIGH_SP, LOW_SP)] == [20, 900, 450]
    assert proxy.held.all() == []


def test_later_write_is_judged_against_the_earlier_one(station, proxy):
    """Split attack: rate up while the doser is off, then doser on."""
    poke.write("coil", PUMP, 1, HOST, proxy.port)
    poke.write("coil", DOSER, 0, HOST, proxy.port)
    for rate in (35, 60, 85, 100):                            # allowed: doser is off, small steps
        poke.write("holding", RATE, rate, HOST, proxy.port)
    assert send(proxy.port, struct.pack(">BHH", 5, DOSER, 0xFF00)) == bytes([0x85, EXC_HOLD])
    assert on_station(station, "coil", DOSER) == 0


def test_split_attack_pipelined_in_one_tcp_segment(station, proxy):
    poke.write("coil", PUMP, 1, HOST, proxy.port)
    poke.write("coil", DOSER, 0, HOST, proxy.port)
    for rate in (35, 60, 85):                                 # under the large-step rule
        poke.write("holding", RATE, rate, HOST, proxy.port)
    with socket.create_connection((HOST, proxy.port), timeout=5) as s:
        s.sendall(adu(fc6(RATE, 100), tid=1) + adu(struct.pack(">BHH", 5, DOSER, 0xFF00), tid=2))
        replies = {r[1]: r[7:] for r in (recv_frame(s), recv_frame(s))}
    assert replies[1][0] == 6
    assert replies[2] == bytes([0x85, EXC_HOLD])
    assert on_station(station, "coil", DOSER) == 0


def test_twin_only_register_is_denied(station, proxy):
    assert send(proxy.port, fc6(101, 5)) == bytes([0x86, EXC_DENY])
    assert on_station(station, "holding_register", 101) == 100
    assert proxy.held.all() == []                             # denied frames are not releasable


def test_unknown_function_code_is_denied(station, proxy):
    mask_write = struct.pack(">BHHH", 22, RATE, 0x0000, 100)  # would set DOSER_RATE_PCT = 100
    with socket.create_connection((HOST, proxy.port), timeout=5) as s:
        s.sendall(adu(mask_write, tid=1))
        assert recv_frame(s)[7:] == bytes([0x96, EXC_DENY])
        s.sendall(read_req(tid=2))                            # connection still usable
        assert recv_frame(s)[7] == 3
    assert on_station(station, "holding_register", RATE) == 10


def test_unknown_station_state_holds(station, proxy, monkeypatch):
    async def boom():
        raise ConnectionError("no poll")
    monkeypatch.setattr(proxy.poller, "poll_once", boom)
    assert send(proxy.port, fc6(RATE, 20)) == bytes([0x86, EXC_HOLD])
    assert on_station(station, "holding_register", RATE) == 10


def test_broken_decide_holds(station, proxy, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("policy bug")
    monkeypatch.setattr("gateway.policy.decide", boom)
    assert send(proxy.port, fc6(RATE, 20)) == bytes([0x86, EXC_HOLD])
    assert on_station(station, "holding_register", RATE) == 10
