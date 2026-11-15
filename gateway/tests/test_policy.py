"""decide(): static envelope, twin-only deny, SAFE_MODE/ALARM rules, every reason collected."""
import pytest

from gateway.frames import WriteRequest
from gateway.policy import ALLOW, DENY, HOLD, decide
from gateway.registers import load_registers
from gateway.station import StationSnapshot

REGS = load_registers()
SRC = "203.0.113.7"


def snap(**over):
    """A connected snapshot at the yaml defaults, pump running."""
    coils = {r.name: r.default for r in map(REGS.by_name, (
        "PUMP_CMD", "DOSER_CMD", "AUTO_MODE", "SAFE_MODE", "ALARM"))}
    coils["PUMP_CMD"] = 1
    holding = {r.name: r.default for r in map(REGS.by_name, (
        "DOSER_RATE_PCT", "LEVEL_HIGH_SP", "LEVEL_LOW_SP", "CHLORINE_TARGET",
        "LOGIC_VERSION", "TANK_LEVEL_PCT", "CHLORINE_MGL"))}
    s = dict(coils=coils, holding=holding,
             discrete_inputs={"PRESENCE": 0, "CABINET_OPEN": 0, "LOCAL_BUTTON": 0,
                              "HIGH_LEVEL_SWITCH": 0, "PUMP_RUNNING": 1, "DOSER_RUNNING": 1},
             input_registers={"DEMAND_PCT": 400, "CABINET_TEMP_C": 220,
                              "CABINET_HUMIDITY": 400, "LOGIC_HASH": 0, "UPTIME_S": 5},
             connected=True, ts=1.0)
    s.update(over)
    return StationSnapshot(**s)


def reg(addr, value, fc=6):
    r = REGS.lookup("holding_register", addr)
    return WriteRequest(SRC, fc, "register", addr, value, r.name if r else None)


def coil(addr, value, fc=5):
    r = REGS.lookup("coil", addr)
    return WriteRequest(SRC, fc, "coil", addr, value, r.name if r else None)


def test_in_envelope_write_is_allowed():
    d = decide(reg(1, 800), snap())
    assert d.decision == ALLOW and d.reasons == []


@pytest.mark.parametrize("addr,value,word", [
    (0, 101, "above"), (1, 1000, "above"), (1, 499, "below"),
    (2, 199, "below"), (3, 401, "above"), (3, 19, "below")])
def test_outside_envelope_is_held(addr, value, word):
    d = decide(reg(addr, value), snap())
    assert d.decision == HOLD
    assert any(word in r and str(value) in r for r in d.reasons), d.reasons


@pytest.mark.parametrize("addr,value", [(0, 0), (0, 100), (1, 500), (1, 950), (3, 20), (3, 400)])
def test_envelope_bounds_are_inclusive(addr, value):
    reasons = decide(reg(addr, value), snap()).reasons
    assert not any("above max" in r or "below min" in r for r in reasons)


def test_register_without_envelope_is_allowed():
    assert decide(reg(4, 65535), snap()).decision == ALLOW


@pytest.mark.parametrize("addr", [100, 101])
def test_twin_only_register_is_denied(addr):
    d = decide(reg(addr, 500), snap())
    assert d.decision == DENY
    assert any("twin" in r for r in d.reasons)


@pytest.mark.parametrize("req", [reg(50, 1), coil(9, 1)])
def test_unmapped_address_is_denied(req):
    d = decide(req, snap())
    assert d.decision == DENY
    assert any("not in the register map" in r for r in d.reasons)


@pytest.mark.parametrize("addr", [3, 4])
def test_setting_safe_mode_or_alarm_is_always_allowed(addr):
    assert decide(coil(addr, 1), snap()).decision == ALLOW
    assert decide(coil(addr, 1), StationSnapshot()).decision == ALLOW  # station offline


def test_clearing_safe_mode_is_held():
    coils = dict(snap().coils, SAFE_MODE=1)
    d = decide(coil(3, 0), snap(coils=coils))
    assert d.decision == HOLD
    assert any("SAFE_MODE" in r for r in d.reasons)


def test_ordinary_coil_write_is_allowed():
    assert decide(coil(0, 1), snap()).decision == ALLOW


def test_every_reason_is_collected(monkeypatch):
    """Every failing check adds its reason: decide() does not stop at the first."""
    import gateway.policy as policy
    extra = lambda req, reg, station: (HOLD, ["last check"])
    monkeypatch.setattr(policy, "CHECKS", [*policy.CHECKS, extra])
    d = decide(reg(0, 500), snap())
    assert d.decision == HOLD
    assert "last check" in d.reasons and len(d.reasons) == 4 + 2  # + 2 context reasons
    assert "above max" in d.reasons[0] and d.reasons[1].startswith("SETPOINT_OUT_OF_RANGE")


def test_deny_outranks_hold_and_keeps_both_reasons(monkeypatch):
    import gateway.policy as policy
    extra = lambda req, reg, station: (HOLD, ["held too"])
    monkeypatch.setattr(policy, "CHECKS", [*policy.CHECKS, extra])
    d = decide(reg(100, 500), snap())
    assert d.decision == DENY
    assert "held too" in d.reasons and len(d.reasons) == 2 + 2  # + 2 context reasons


# ---- step 11: context checks -------------------------------------------------------------

import gateway.policy as policy  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_context(monkeypatch):
    """Each test starts with an empty source history and a daytime demo clock."""
    policy.reset_context()
    monkeypatch.setenv("DEMO_CLOCK", "14:00")


def held(src=SRC, value=500, **snap_over):
    r = REGS.lookup("holding_register", 0)
    return decide(WriteRequest(src, 6, "register", 0, value, r.name), snap(**snap_over))


def test_new_source_is_a_reason_then_seen():
    assert "source not seen before" in held().reasons
    assert "source not seen before" not in held().reasons


def test_context_never_holds_a_clean_write():
    d = decide(reg(1, 800), snap(discrete_inputs={"PRESENCE": 0, "CABINET_OPEN": 0}))
    assert d.decision == ALLOW and d.reasons == []


def test_nobody_on_site_vs_presence_or_cabinet():
    assert "nobody on site" in held().reasons
    di = {"PRESENCE": 1, "CABINET_OPEN": 0}
    assert "nobody on site" not in held(discrete_inputs=di).reasons
    di = {"PRESENCE": 0, "CABINET_OPEN": 1}
    assert "nobody on site" not in held(discrete_inputs=di).reasons


def test_demo_clock_controls_time_of_day(monkeypatch):
    monkeypatch.setenv("DEMO_CLOCK", "03:00")
    assert any("03:00" in r and "outside" in r for r in held().reasons)
    monkeypatch.setenv("DEMO_CLOCK", "14:00")
    assert not any("outside" in r for r in held().reasons)
    monkeypatch.setenv("DEMO_CLOCK", "2026-11-14T03:12:09")  # full timestamp also accepted
    assert any("outside" in r for r in held().reasons)


def test_bad_demo_clock_falls_back_to_real_time(monkeypatch):
    monkeypatch.setenv("DEMO_CLOCK", "banana")
    assert held().decision == HOLD  # no crash


def test_write_rate_per_source():
    for _ in range(policy.RATE_MAX):
        held()
    assert any("writes in" in r for r in held().reasons)
    assert not any("writes in" in r for r in held(src="198.51.100.9").reasons)


def test_large_step_is_held_for_approval_and_release_skips_only_that():
    d = decide(reg(0, 40), snap())                       # safe per physics, but a big jump
    assert d.decision == HOLD and any("large step" in r for r in d.reasons)
    assert decide(reg(0, 30), snap()).decision == ALLOW  # within the step limit
    assert decide(reg(0, 40), snap(), release=True).decision == ALLOW
    assert decide(reg(0, 500), snap(), release=True).decision == HOLD      # envelope still holds
    assert decide(coil(3, 0), snap(), release=True).decision == HOLD       # SAFE_MODE rule too
    assert decide(reg(101, 5), snap(), release=True).decision == DENY      # twin-only too
