"""The demo as it is run: software station at its boot state (pump off), scenario 0 then
scenario 1 with the real attack scripts through the gateway. The hold must give the physics
reason (CHLORINE_HIGH), not only the large-step rule."""
import subprocess
import sys
import time

import pytest

from gateway import physics_client
from sim import poke
from test_events import Rig, store  # noqa: F401 (fixture)
from test_proxy import HOST, ROOT, station  # noqa: F401 (fixture)


def script(module, port):
    return subprocess.run([sys.executable, "-m", module, "--host", HOST, "--port", str(port)],
                          cwd=ROOT, capture_output=True, text=True, timeout=30)


@pytest.mark.skipif(not physics_client.USING_REAL_PHYSICS, reason="needs the real physics package")
def test_boot_state_scenario_0_then_1_is_held_for_chlorine(station, store, monkeypatch):
    monkeypatch.setenv("OPERATOR_IPS", "127.0.0.1")
    monkeypatch.setenv("DEMO_CLOCK", "03:00")                    # the 3 am story
    rig = Rig(station, store)
    try:
        r = script("attacks.normal_day", rig.proxy.port)
        assert r.returncode == 0, r.stdout + r.stderr
        end = time.time() + 5                                    # the station's next control tick
        while time.time() < end and not poke.read_all(HOST, station.port)[("discrete_input", 5)]:
            time.sleep(0.1)
        r = script("attacks.chlorine_overdose", rig.proxy.port)
        assert r.returncode == 0, r.stdout + r.stderr
    finally:
        rig.close()
    events = store.list_events()
    assert [e["decision"] for e in events if e["name"] in ("PUMP_CMD", "LEVEL_HIGH_SP")] == ["ALLOW", "ALLOW"]
    (held,) = [e for e in events if e["decision"] == "HOLD"]
    assert (held["name"], held["new_value"]) == ("DOSER_RATE_PCT", 100)
    assert any(r.startswith("CHLORINE_HIGH in ") for r in held["reasons"]), held["reasons"]
    assert any(not v["pre_existing"] and v["code"] == "CHLORINE_HIGH" for v in held["prediction"]["violations"])
    assert poke.read_all(HOST, station.port)[("holding_register", 0)] == 10
    print(held["reasons"])
