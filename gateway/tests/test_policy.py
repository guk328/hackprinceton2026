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


def test_context_never_holds_an_operator_s_clean_write():
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
    assert decide(reg(101, 5), snap(), release=True).decision == DENY      # twin-only too


# ---- large step over a rolling window (review finding 1) ----------------------------------

def rate_snap(rate):
    return snap(holding=dict(snap().holding, DOSER_RATE_PCT=rate))


def test_slow_walk_is_measured_from_where_it_started(monkeypatch):
    """10 -> 35 -> 60 -> 85 in steps of 25 used to pass every time."""
    assert decide(reg(0, 35), rate_snap(10)).decision == ALLOW
    d = decide(reg(0, 60), rate_snap(35))
    assert d.decision == HOLD
    assert any("DOSER_RATE_PCT 10 -> 60 within 30 min is a large step" in r for r in d.reasons), d.reasons
    assert decide(reg(0, 45), rate_snap(35)).decision == HOLD       # 10 -> 45 is still 35
    assert decide(reg(1, 840), rate_snap(35)).decision == ALLOW     # other registers unaffected


def test_walk_window_expires_after_30_minutes(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(policy, "monotonic", lambda: t[0])
    # Rates stay at or below 40 %: above that the physics inlet-dose check holds by itself.
    assert decide(reg(0, 25), rate_snap(0)).decision == ALLOW
    t[0] += policy.STEP_WINDOW_S - 60
    assert decide(reg(0, 26), rate_snap(25)).decision == HOLD       # 0 was seen 29 min ago
    t[0] += policy.STEP_WINDOW_S + 1
    assert decide(reg(0, 40), rate_snap(25)).decision == ALLOW      # only 25 in the window now


def test_a_released_step_becomes_the_new_starting_point():
    assert decide(reg(0, 40), rate_snap(10)).decision == HOLD
    assert decide(reg(0, 40), rate_snap(10), release=True).decision == ALLOW   # operator said ALLOW
    assert decide(reg(0, 38), rate_snap(40)).decision == ALLOW      # from 10 it would be a step of 28


# ---- OPERATOR_IPS: who, when and on-site can hold (review finding 2) -----------------------

STRANGER = "198.51.100.9"
ON_SITE = {"PRESENCE": 1, "CABINET_OPEN": 1}


def small(src, **snap_over):
    """LEVEL_HIGH_SP 850 -> 840: clean by every other rule (scenario 0's setting change)."""
    return decide(WriteRequest(src, 6, "register", 1, 840, "LEVEL_HIGH_SP"), snap(**snap_over))


def test_stranger_is_held_at_night_or_with_nobody_on_site(monkeypatch):
    d = small(STRANGER)                                           # 14:00, nobody on site
    assert d.decision == HOLD
    assert d.reasons[0] == f"source {STRANGER} is not an operator address: needs operator approval"
    assert "nobody on site" in d.reasons
    assert small(STRANGER, discrete_inputs=ON_SITE).decision == ALLOW   # day and someone there
    monkeypatch.setenv("DEMO_CLOCK", "03:00")
    d = small(STRANGER, discrete_inputs=ON_SITE)
    assert d.decision == HOLD and any("03:00" in r for r in d.reasons)


def test_scenario_0_from_an_operator_address_stays_allowed(monkeypatch):
    monkeypatch.setenv("DEMO_CLOCK", "03:00")
    monkeypatch.setenv("OPERATOR_IPS", "10.0.0.0/24, 192.0.2.5")
    for src in ("192.0.2.5", "10.0.0.77"):
        assert decide(WriteRequest(src, 5, "coil", 0, 1, "PUMP_CMD"), snap()).decision == ALLOW
        d = small(src)
        assert d.decision == ALLOW and d.reasons == []
    assert small("10.0.1.1").decision == HOLD


def test_operator_list_unset_empty_and_garbage(monkeypatch):
    monkeypatch.delenv("OPERATOR_IPS")
    assert small("127.0.0.1").decision == ALLOW and small(SRC).decision == HOLD
    monkeypatch.setenv("OPERATOR_IPS", "")
    assert small("127.0.0.1").decision == HOLD                    # set but empty: nobody
    monkeypatch.setenv("OPERATOR_IPS", "everyone, 0.0.0.0/zero, 192.0.2.5")
    assert small("192.0.2.5").decision == ALLOW and small(SRC).decision == HOLD
    assert small("not-an-ip").decision == HOLD


def test_operator_allow_releases_a_stranger_s_safe_write():
    assert small(STRANGER).decision == HOLD
    req = WriteRequest(STRANGER, 6, "register", 1, 840, "LEVEL_HIGH_SP")
    assert decide(req, snap(), release=True).decision == ALLOW


# ---- safe mode, alarm and doser shut-off (review finding 3) -------------------------------

def from_stranger(kind, addr, value, **snap_over):
    name = REGS.lookup("coil" if kind == "coil" else "holding_register", addr).name
    return decide(WriteRequest(STRANGER, 5 if kind == "coil" else 6, kind, addr, value, name),
                  snap(discrete_inputs=ON_SITE, **snap_over))     # daytime, someone on site


def test_safe_mode_on_is_allowed_but_marked_for_an_incident():
    d = decide(coil(3, 1), snap())
    assert d.decision == ALLOW and d.reasons[0].startswith(policy.SAFE_MODE_SET + ":") and SRC in d.reasons[0]
    assert from_stranger("coil", 3, 1).decision == ALLOW
    assert decide(coil(3, 1), snap(coils=dict(snap().coils, SAFE_MODE=1))).reasons == []   # already on
    assert decide(coil(4, 1), snap()).reasons == []                                         # alarm on: silent


def test_clearing_safe_mode_is_releasable_with_operator_approval():
    safe = snap(coils=dict(snap().coils, SAFE_MODE=1, DOSER_CMD=0))
    assert decide(coil(3, 0), safe).decision == HOLD
    assert decide(coil(3, 0), safe, release=True).decision == ALLOW


@pytest.mark.parametrize("kind,addr", [("coil", 4), ("coil", 1), ("register", 0)])
def test_alarm_and_doser_off_from_a_stranger_is_held(kind, addr):
    d = from_stranger(kind, addr, 0, coils=dict(snap().coils, ALARM=1))
    assert d.decision == HOLD and "turning off the alarm or the chlorine dose" in d.reasons[0], d.reasons
    name = REGS.lookup("coil" if kind == "coil" else "holding_register", addr).name
    op = WriteRequest(SRC, 5, kind, addr, 0, name)                # the operator may
    assert decide(op, snap(discrete_inputs=ON_SITE)).decision == ALLOW
    assert from_stranger(kind, addr, 1 if kind == "coil" else 12).decision == ALLOW   # not a shut-off
