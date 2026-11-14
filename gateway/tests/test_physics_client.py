"""Physics stub (README 4.3 signature), timeout wrapper, and decide() failing closed."""
import dataclasses
import inspect
import time

import pytest

from gateway import physics_client as pc
from gateway.physics_client import Command, PlantState, Prediction, Violation, predict, step
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
    assert pc.plant_state(snap()) == PlantState(
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


def test_physics_sleeps_gives_hold_in_under_100_ms(monkeypatch):
    def slow(state, cmd, horizon_min=120):
        time.sleep(0.5)
        return Prediction(safe=True, violations=[], trajectory=[])
    monkeypatch.setattr(pc, "predict", slow)
    decide(reg(1, 800), snap())  # warm the worker pool; this call is held too
    t0 = time.perf_counter()
    d = decide(reg(1, 800), snap())
    elapsed = time.perf_counter() - t0
    assert d.decision == HOLD
    assert any("timed out" in r for r in d.reasons)
    assert elapsed < 0.1, elapsed


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
    assert d.decision == HOLD and d.reasons == ["PRESSURE_LOSS in 42 min"]


def test_station_offline_gives_hold():
    for station in (StationSnapshot(), snap(connected=False)):
        d = decide(coil(0, 1), station)
        assert d.decision == HOLD
        assert any("station" in r for r in d.reasons)


def test_envelope_and_physics_reasons_are_both_kept():
    d = decide(reg(0, 101), snap())
    assert d.decision == HOLD
    assert any("above max" in r for r in d.reasons)
    assert any(r.startswith("CHLORINE_HIGH") for r in d.reasons)
