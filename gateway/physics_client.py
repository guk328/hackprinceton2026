"""The gateway's door to the physics model.

Stub section: same names and signatures as README 4.3, so step 12 replaces it with
`from physics import PlantState, Command, Violation, Prediction, predict, step`
(keeping this stub as the ImportError fallback).

plant_state()  StationSnapshot -> PlantState in engineering units
check()        predict() with a hard timeout; raises on error or timeout, the caller holds
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from gateway.station import StationSnapshot

HORIZON_MIN = 120
TIMEOUT_S = 0.05  # README 4.3: predict takes < 20 ms

# ---------------------------------------------------------------- stub (README 4.3)

CHLORINE_LIMIT_MGL = 4.0
_STUB_UNSAFE_RATE_PCT = 50   # stub only: a doser above this overdoses the tank
_COILS = {0: "pump_on", 1: "doser_on", 2: "auto_mode"}
_REGISTERS = {0: ("doser_rate_pct", 1), 1: ("level_high_sp", 10), 2: ("level_low_sp", 10)}


@dataclass(frozen=True)
class PlantState:
    level_pct: float
    chlorine_mgl: float
    demand_pct: float
    pump_on: bool
    doser_on: bool
    doser_rate_pct: float
    level_high_sp: float
    level_low_sp: float
    auto_mode: bool


@dataclass(frozen=True)
class Command:
    kind: str      # "coil" or "register"
    address: int
    value: int


@dataclass(frozen=True)
class Violation:
    code: str
    minutes_until: float
    value: float
    limit: float
    message: str
    pre_existing: bool = False


@dataclass(frozen=True)
class Prediction:
    safe: bool = False
    violations: list = field(default_factory=list)
    trajectory: list = field(default_factory=list)   # [(t_min, level_pct, chlorine_mgl), ...]


def _apply(state: PlantState, cmd: Command) -> PlantState:
    if cmd.kind == "coil" and cmd.address in _COILS:
        return replace(state, **{_COILS[cmd.address]: bool(cmd.value)})
    if cmd.kind == "register" and cmd.address in _REGISTERS:
        name, scale = _REGISTERS[cmd.address]
        return replace(state, **{name: cmd.value / scale if scale != 1 else cmd.value})
    return state


def _violations(state: PlantState) -> list:
    if state.chlorine_mgl > CHLORINE_LIMIT_MGL:
        minutes = 0.0
    elif state.pump_on and state.doser_on and state.doser_rate_pct > _STUB_UNSAFE_RATE_PCT:
        # Made-up rate: 100 % dosing adds 0.25 mg/L per plant-minute. The real model replaces it.
        minutes = (CHLORINE_LIMIT_MGL - state.chlorine_mgl) / (state.doser_rate_pct / 400)
    else:
        return []
    return [Violation(
        code="CHLORINE_HIGH", minutes_until=minutes, value=CHLORINE_LIMIT_MGL,
        limit=CHLORINE_LIMIT_MGL,
        message=f"Chlorine exceeds {CHLORINE_LIMIT_MGL} mg/L in about {minutes:.0f} plant-minutes")]


def predict(state, cmd, horizon_min=120):
    """Stub forecast: pure, fast, never raises. Unusable input comes back unsafe."""
    try:
        before = {v.code for v in _violations(state)}
        after = _apply(state, cmd)
        found = [replace(v, pre_existing=v.code in before) for v in _violations(after)
                 if v.minutes_until <= horizon_min]
        return Prediction(safe=not found, violations=found, trajectory=[
            (0.0, after.level_pct, after.chlorine_mgl),
            (float(horizon_min), after.level_pct, after.chlorine_mgl)])
    except Exception:
        return Prediction(violations=[Violation(
            code="SETPOINT_OUT_OF_RANGE", minutes_until=0.0, value=0.0, limit=0.0,
            message="Cannot forecast: invalid state, command or horizon")])


def step(state, dt_s=1.0):
    """Stub: the plant does not move. Only the twin uses step()."""
    return replace(state)


# ---------------------------------------------------------------- gateway side

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="physics")


def plant_state(station: StationSnapshot) -> PlantState:
    """Raises if the snapshot is stale or incomplete; the caller holds."""
    if not station.connected:
        raise ValueError("station is offline")
    c, h = station.coils, station.holding
    return PlantState(
        level_pct=h["TANK_LEVEL_PCT"] / 10,
        chlorine_mgl=h["CHLORINE_MGL"] / 100,
        demand_pct=station.input_registers["DEMAND_PCT"] / 10,
        pump_on=bool(c["PUMP_CMD"]),
        doser_on=bool(c["DOSER_CMD"]),
        doser_rate_pct=h["DOSER_RATE_PCT"],
        level_high_sp=h["LEVEL_HIGH_SP"] / 10,
        level_low_sp=h["LEVEL_LOW_SP"] / 10,
        auto_mode=bool(c["AUTO_MODE"]),
    )


def check(state: PlantState, cmd: Command, horizon_min: int = HORIZON_MIN,
          timeout_s: float = TIMEOUT_S) -> Prediction:
    """predict() on a worker thread. Raises TimeoutError after timeout_s, or whatever
    predict raised. A stuck call keeps its worker; with all workers stuck every later
    check times out, which still holds."""
    future = _pool.submit(predict, state, cmd, horizon_min)
    try:
        return future.result(timeout=timeout_s)
    finally:
        future.cancel()  # drops it if it never started
