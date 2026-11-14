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
    assert len(d.reasons) == 3 and d.reasons[-1] == "last check"
    assert "above max" in d.reasons[0] and d.reasons[1].startswith("CHLORINE_HIGH")


def test_deny_outranks_hold_and_keeps_both_reasons(monkeypatch):
    import gateway.policy as policy
    extra = lambda req, reg, station: (HOLD, ["held too"])
    monkeypatch.setattr(policy, "CHECKS", [*policy.CHECKS, extra])
    d = decide(reg(100, 500), snap())
    assert d.decision == DENY
    assert "held too" in d.reasons and len(d.reasons) == 2
