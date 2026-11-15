"""Alarm on hold: a HOLD backed by a predicted physics violation, and a logic tamper, set the
station's ALARM coil by plain code. A large-step-only hold does not, and neither does anything
when no alarm callable is wired. SAFE_MODE is never touched."""
import pytest

from gateway.station_write import StationWriter
from sim import poke
from test_block import on_station
from test_proxy import HOST, station  # noqa: F401 (fixture)
from test_runner import Gw

ALARM, SAFE = 4, 3


@pytest.fixture
def g(station):
    gw = Gw(station, script=None)
    gw.proxy.alarm = StationWriter(HOST, station.port).alarm
    yield gw
    gw.close()


def alarm_steps(g):
    return [s for s in g.store.list_agent_steps() if "alarm raised" in s["text"]]


def test_physics_hold_raises_alarm_only(station, g):
    eid = g.attack(100)                                    # scenario 1: CHLORINE_HIGH
    assert g.wait(lambda: on_station(station, "coil", ALARM) == 1)
    assert on_station(station, "coil", SAFE) == 0
    assert on_station(station, "holding_register", 0) == 10
    assert g.wait(lambda: alarm_steps(g))
    assert alarm_steps(g)[0]["event_id"] == eid and "CHLORINE_HIGH" in alarm_steps(g)[0]["text"]


def test_large_step_only_hold_stays_silent(station, g):
    g.attack(40)                                           # scenario 5: safe, but a large step
    assert g.wait(lambda: g.channel.inbox())               # the incident ran
    assert on_station(station, "coil", ALARM) == 0 and alarm_steps(g) == []


def test_tamper_raises_alarm(station, g):
    g.proxy.poller.interval, g.proxy.poller.tamper_confirm_s = 0.15, 0.4
    g.on(g.proxy.poller.start())
    assert g.wait(lambda: g.proxy.poller.last_good_logic)
    poke.write("holding", 1, 900, HOST, station.port)      # behind the gateway
    assert g.wait(lambda: on_station(station, "coil", ALARM) == 1, timeout=6)


def test_no_alarm_callable_and_failing_alarm_change_nothing(station):
    gw = Gw(station, script=None)                          # alarm=None: feature off
    try:
        gw.attack(100)
        assert gw.wait(lambda: gw.channel.inbox())
        assert on_station(station, "coil", ALARM) == 0

        def broken():
            raise ConnectionError("station gone")
        gw.proxy.alarm = broken
        assert gw.attack(100)                              # still held, still answered
        assert on_station(station, "coil", ALARM) == 0
    finally:
        gw.close()
