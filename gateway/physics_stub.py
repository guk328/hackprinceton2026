"""Stand-in for the physics package: same names and signatures as README 4.3.
Used only if `import physics` fails."""
from dataclasses import dataclass, field, replace

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
    baseline_minutes_until: float | None = None   # when the same limit breaks with no command


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
        before = {v.code: v.minutes_until for v in _violations(state)}
        after = _apply(state, cmd)
        found = [replace(v, pre_existing=v.code in before, baseline_minutes_until=before.get(v.code))
                 for v in _violations(after) if v.minutes_until <= horizon_min]
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
