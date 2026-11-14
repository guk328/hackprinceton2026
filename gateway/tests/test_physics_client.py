"""Physics stub (gateway/physics_stub.py, README 4.3 signature), timeout wrapper, and decide() failing closed."""
import dataclasses
import inspect
import threading
import time

import pytest

from gateway import physics_client as pc
from gateway.physics_stub import Command, PlantState, Prediction, Violation, predict, step
from gateway.policy import ALLOW, HOLD, decide
from gateway.station import StationSnapshot
from gateway.tests.test_policy import coil, reg, snap

README_FIELDS = ["level_pct", "chlorine_mgl", "demand_pct", "pump_on", "doser_on",
                 "doser_rate_pct", "level_high_sp", "level_low_sp", "auto_mode"]


def readme_state(**over):
    return dataclasses.replace(
        PlantState(level_pct=60.0, chlorine_mgl=1.0, demand_pct=40.0,
                   pump_on=True, doser_on=True, doser_rate_pct=10,
                   level_high_sp=85.0, level_low_sp=40.0, auto_mode=True), **over)


def high(pre_existing=False):
    return Violation(code="CHLORINE_HIGH", minutes_until=11.4, value=4.0, limit=4.0,
                     pre_existing=pre_existing, message="Chlorine exceeds 4.0 mg/L")


# --- the stub matches README 4.3 ---------------------------------------------------------

def test_readme_example_runs_verbatim():
    p = predict(readme_state(), Command(kind="register", address=0, value=100), horizon_min=120)
    assert p.safe is False
    v = p.violations[0]
    assert v.code == "CHLORINE_HIGH" and v.pre_existing is False
    assert v.limit == 4.0 and v.minutes_until > 0 and v.message
    assert all(len(pt) == 3 for pt in p.trajectory)
    assert isinstance(step(readme_state(), dt_s=1.0), PlantState)


def test_signatures():
    sig = inspect.signature(predict)
    assert list(sig.parameters) == ["state", "cmd", "horizon_min"]
    assert sig.parameters["horizon_min"].default == 120
    sig = inspect.signature(step)
    assert list(sig.parameters) == ["state", "dt_s"] and sig.parameters["dt_s"].default == 1.0
    assert [f.name for f in dataclasses.fields(PlantState)] == README_FIELDS
    assert [f.name for f in dataclasses.fields(Command)] == ["kind", "address", "value"]
    assert {f.name for f in dataclasses.fields(Violation)} == {
        "code", "minutes_until", "value", "limit", "pre_existing", "message"}
    assert {f.name for f in dataclasses.fields(Prediction)} == {"safe", "violations", "trajectory"}


def test_stub_is_a_drop_in_for_the_real_package():
    """Step 12 is an import swap: same call signatures, stub fields are a subset."""
    physics = pytest.importorskip("physics")
    names = lambda f: list(inspect.signature(f).parameters)
    defaults = lambda f: [p.default for p in inspect.signature(f).parameters.values()
                          if p.default is not p.empty]
    for fn in ("predict", "step"):
        assert names(getattr(pc, fn)) == names(getattr(physics, fn))
        assert defaults(getattr(pc, fn)) == defaults(getattr(physics, fn))
    fields = lambda c: {f.name for f in dataclasses.fields(c)}
    for cls in ("PlantState", "Command", "Violation", "Prediction"):
        assert fields(getattr(pc, cls)) <= fields(getattr(physics, cls)), cls
    assert fields(pc.Command) == fields(physics.Command)


def test_stub_safe_command_and_pre_existing():
    assert predict(readme_state(), Command("register", 1, 800)).safe is True
    p = predict(readme_state(chlorine_mgl=5.0), Command("register", 1, 800))
    assert p.violations and all(v.pre_existing for v in p.violations)


def test_stub_never_raises():
    p = predict(None, Command("register", 0, "x"), horizon_min=-1)
    assert p.safe is False and p.violations


# --- snapshot -> PlantState --------------------------------------------------------------

def test_plant_state_from_snapshot_uses_engineering_units():
    got = pc.plant_state(snap())
    assert {f: getattr(got, f) for f in README_FIELDS} == dict(
        level_pct=60.0, chlorine_mgl=1.0, demand_pct=40.0, pump_on=True, doser_on=True,
        doser_rate_pct=10, level_high_sp=85.0, level_low_sp=40.0, auto_mode=True)


# --- decide() with physics ---------------------------------------------------------------

def test_scenario_1_is_held_with_a_physics_reason():
    d = decide(reg(0, 100), snap())
    assert d.decision == HOLD
    assert any(r.startswith("CHLORINE_HIGH in ") and r.endswith(" min") for r in d.reasons)
    assert d.prediction is not None and d.prediction.safe is False


def test_safe_write_carries_its_prediction():
    d = decide(reg(1, 800), snap())
    assert d.decision == ALLOW and d.prediction.safe is True


def test_physics_raises_gives_hold(monkeypatch):
    def boom(state, cmd, horizon_min=120):
        raise RuntimeError("model blew up")
    monkeypatch.setattr(pc, "predict", boom)
    d = decide(reg(1, 800), snap())
    assert d.decision == HOLD
    assert any("physics" in r for r in d.reasons)


def wait_until_idle():
    """Let abandoned predict() calls finish so they do not use up slots in later tests."""
    end = time.time() + 5
    while pc._slots._value < pc.MAX_IN_FLIGHT and time.time() < end:
        time.sleep(0.01)
    assert pc._slots._value == pc.MAX_IN_FLIGHT


def test_physics_sleeps_gives_hold_at_the_timeout(monkeypatch):
    def slow(state, cmd, horizon_min=120):
        time.sleep(0.3)
        return Prediction(safe=True, violations=[], trajectory=[])
    monkeypatch.setattr(pc, "predict", slow)
    monkeypatch.setattr(pc, "TIMEOUT_S", 0.05)
    decide(reg(1, 800), snap())  # warm the worker pool; this call is held too
    t0 = time.perf_counter()
    d = decide(reg(1, 800), snap())
    elapsed = time.perf_counter() - t0
    assert d.decision == HOLD
    assert any("timed out" in r for r in d.reasons)
    assert 0.05 <= elapsed < 0.2, elapsed  # gave up at the timeout, did not wait out the 0.3 s
    wait_until_idle()


def test_stale_calls_do_not_delay_later_checks(monkeypatch):
    """Timed-out calls keep running; a later check must not queue behind them."""
    release = threading.Event()

    def stuck(state, cmd, horizon_min=120):
        release.wait(5)
        return Prediction(safe=True, violations=[], trajectory=[])
    state, cmd = pc.plant_state(snap()), pc.Command("register", 1, 800)
    try:
        monkeypatch.setattr(pc, "predict", stuck)
        for _ in range(pc.MAX_IN_FLIGHT - 1):
            with pytest.raises(TimeoutError):
                pc.check(state, cmd, timeout_s=0.01)
        monkeypatch.setattr(pc, "predict", real.predict)
        assert pc.check(state, cmd, timeout_s=5).safe is True   # one slot left: runs at once

        monkeypatch.setattr(pc, "predict", stuck)
        with pytest.raises(TimeoutError):
            pc.check(state, cmd, timeout_s=0.01)                # takes the last slot
        monkeypatch.setattr(pc, "predict", real.predict)
        t0 = time.perf_counter()
        d = decide(reg(1, 800), snap())                         # nothing free: held at once
        assert time.perf_counter() - t0 < 0.05
        assert d.decision == HOLD and any("timed out" in r for r in d.reasons)
    finally:
        release.set()
        wait_until_idle()
    assert decide(reg(1, 800), snap()).decision == ALLOW        # and it recovers


@pytest.mark.parametrize("raw, want", [
    (None, pc.DEFAULT_TIMEOUT_S), ("", pc.DEFAULT_TIMEOUT_S), ("fast", pc.DEFAULT_TIMEOUT_S),
    ("0", pc.DEFAULT_TIMEOUT_S), ("-1", pc.DEFAULT_TIMEOUT_S), ("inf", pc.DEFAULT_TIMEOUT_S),
    ("nan", pc.DEFAULT_TIMEOUT_S), ("0.4", 0.4), (" 1 ", 1.0)])
def test_timeout_setting(raw, want):
    assert pc.DEFAULT_TIMEOUT_S == 0.25
    assert pc.timeout_from_env(raw) == want


@pytest.mark.parametrize("bad", [None, "ok", Prediction(safe=False, violations=[], trajectory=[])])
def test_unusable_physics_result_gives_hold(monkeypatch, bad):
    monkeypatch.setattr(pc, "predict", lambda state, cmd, horizon_min=120: bad)
    assert decide(reg(1, 800), snap()).decision == HOLD


def test_pre_existing_violations_are_ignored(monkeypatch):
    p = Prediction(safe=False, violations=[high(pre_existing=True)], trajectory=[])
    monkeypatch.setattr(pc, "predict", lambda state, cmd, horizon_min=120: p)
    d = decide(reg(1, 800), snap())
    assert d.decision == ALLOW and d.reasons == []


def test_only_new_violations_become_reasons(monkeypatch):
    low = dataclasses.replace(high(), code="PRESSURE_LOSS", minutes_until=42.0)
    p = Prediction(safe=False, violations=[high(pre_existing=True), low], trajectory=[])
    monkeypatch.setattr(pc, "predict", lambda state, cmd, horizon_min=120: p)
    d = decide(reg(1, 800), snap())
    assert d.decision == HOLD and d.reasons[:1] == ["PRESSURE_LOSS in 42 min"]
    assert not any("CHLORINE_HIGH" in r for r in d.reasons)


def test_station_offline_gives_hold():
    for station in (StationSnapshot(), snap(connected=False)):
        d = decide(coil(0, 1), station)
        assert d.decision == HOLD
        assert any("station" in r for r in d.reasons)


def test_envelope_and_physics_reasons_are_both_kept():
    d = decide(reg(0, 101), snap())
    assert d.decision == HOLD
    assert any("above max" in r for r in d.reasons)
    assert any(r.startswith("SETPOINT_OUT_OF_RANGE") for r in d.reasons)  # real model's view


# ---- step 12: the real physics package ----------------------------------------------------

import physics as real  # noqa: E402


def test_client_uses_the_real_package():
    assert pc.USING_REAL_PHYSICS is True
    assert pc.predict is real.predict and pc.PlantState is real.PlantState


def test_horizon_covers_slow_scenarios():
    assert pc.HORIZON_MIN >= 270  # pressure loss takes ~240 plant-min in scenario 2


def test_plant_state_ignores_lagging_running_inputs():
    """PUMP_RUNNING trails a write by a station tick; the commanded pump must still count."""
    di = {"PRESENCE": 0, "CABINET_OPEN": 0, "HIGH_LEVEL_SWITCH": 0,
          "PUMP_RUNNING": 0, "DOSER_RUNNING": 0}
    s = pc.plant_state(snap(discrete_inputs=di))   # snap() has PUMP_CMD=1, DOSER_CMD=1
    assert s.pump_on is True and s.doser_on is True


def test_plant_state_applies_the_high_level_interlock():
    di = {"PRESENCE": 0, "CABINET_OPEN": 0, "HIGH_LEVEL_SWITCH": 1}
    s = pc.plant_state(snap(discrete_inputs=di))
    assert s.pump_on is False and s.doser_on is False


def test_plant_state_carries_safe_mode_and_alarm():
    coils = dict(snap().coils, SAFE_MODE=1, ALARM=1)
    s = pc.plant_state(snap(coils=coils))
    assert s.safe_mode is True and s.alarm is True


def reasons_for(req, **over):
    d = decide(req, snap(**over))
    return d, " | ".join(d.reasons)


def test_scenario_1_real_model_reason_in_minutes():
    d, text = reasons_for(reg(0, 100))
    assert d.decision == HOLD
    assert any(r.startswith("CHLORINE_HIGH in ") and r.endswith(" min") for r in d.reasons), text


def test_scenario_2_pressure_loss_is_caught():
    di = {"PRESENCE": 0, "CABINET_OPEN": 0, "PUMP_RUNNING": 1, "DOSER_RUNNING": 1}
    coils = dict(snap().coils, AUTO_MODE=0)
    d, text = reasons_for(coil(0, 0), coils=coils, discrete_inputs=di,
                          input_registers=dict(snap().input_registers, DEMAND_PCT=1000))
    assert d.decision == HOLD and "PRESSURE_LOSS in " in text, text


def test_scenario_3_overflow_is_caught():
    d, text = reasons_for(reg(1, 1000))
    assert d.decision == HOLD and "TANK_OVERFLOW in " in text, text


@pytest.mark.parametrize("req", [reg(0, 100), reg(1, 1000)])
def test_real_check_latency(req):
    """The fastest run is the model's own cost (~30 ms on a fast core, 55-135 ms on this
    laptop's slow cores): scheduling and load only ever add to it, so keep sampling until one
    run shows it. The median must still clear the default timeout wherever the thread lands."""
    state, cmd = pc.plant_state(snap()), pc.Command(req.kind, req.address, req.value)
    runs = []
    while len(runs) < 9 or (min(runs) >= 0.09 and len(runs) < 40):
        t0 = time.perf_counter()
        pc.check(state, cmd, timeout_s=5)
        runs.append(time.perf_counter() - t0)
    runs.sort()
    assert runs[0] < 0.09, runs
    assert runs[len(runs) // 2] < pc.DEFAULT_TIMEOUT_S, runs
