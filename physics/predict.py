"""Copied-state forecasts with all six demo guards.

Pump cycling uses the supplied history plus predicted starts.
Only standard-library code is used; no station or network access occurs here.
"""

from dataclasses import dataclass, field, replace
from math import isfinite
from numbers import Real
from typing import List, Optional, Tuple

from .limits import (
    CHECKED_LIMITS, CHLORINE_HIGH_MGL, CHLORINE_LOW_MGL,
    SETPOINT_BOUNDS, TANK_OVERFLOW_PCT, PRESSURE_LOSS_PCT,
    MAX_PUMP_STARTS_PER_HOUR, PUMP_START_WINDOW_S,
)
from .model import TANK_CAPACITY_M3, Command, PlantState, step


# Wire addresses/scales mirror shared/registers.yaml; tests check that contract.
COIL_FIELDS = {0: "pump_on", 1: "doser_on", 2: "auto_mode", 3: "safe_mode", 4: "alarm"}
REGISTER_FIELDS = {
    0: ("doser_rate_pct", 1),
    1: ("level_high_sp", 10),
    2: ("level_low_sp", 10),
    3: ("chlorine_target_mgl", 100),
    4: ("logic_version", 1),
}
MAX_HORIZON_MIN = 1440.0  # Bound work/memory to one plant-day, not unbounded input.


@dataclass(frozen=True)
class Violation:
    code: str
    minutes_until: float
    value: float
    limit: float
    message: str
    pre_existing: bool = False
    baseline_minutes_until: Optional[float] = None


@dataclass(frozen=True)
class Prediction:
    safe: bool = False
    violations: List[Violation] = field(default_factory=list)
    trajectory: List[Tuple[float, float, float]] = field(default_factory=list)
    baseline_trajectory: List[Tuple[float, float, float]] = field(default_factory=list)
    assessment_complete: bool = False
    checked_limits: Tuple[str, ...] = CHECKED_LIMITS


def _validate_state(state):
    if not isinstance(state, PlantState):
        raise ValueError("state must be a PlantState")
    for name in ("level_pct", "chlorine_mgl", "demand_pct", "doser_rate_pct",
                 "level_high_sp", "level_low_sp", "chlorine_target_mgl"):
        value = getattr(state, name)
        if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
            raise ValueError(f"{name} must be a finite number in engineering units")
    for name in COIL_FIELDS.values():
        value = getattr(state, name)
        if type(value) not in (bool, int) or value not in (0, 1):
            raise ValueError(f"{name} must be a boolean or 0/1")
    if type(state.logic_version) is not int or not 0 <= state.logic_version <= 65535:
        raise ValueError("logic_version must be an unsigned 16-bit integer")
    if state.chlorine_target_mgl < 0:
        raise ValueError("chlorine_target_mgl must be nonnegative [mg/L]")
    volume_m3 = state.level_pct / 100 * TANK_CAPACITY_M3
    if not isfinite(volume_m3) or not isfinite(volume_m3 * state.chlorine_mgl):
        raise ValueError("derived volume [m³] or chlorine mass [g] overflowed")
    step(state, dt_s=0)  # Reuse the physical state's finite/range checks.


def _apply_command(state, cmd):
    """Decode a single raw coil/register write onto a fresh state."""
    if cmd is None:
        return replace(state)
    if not isinstance(cmd, Command):
        raise ValueError("command must be Command or None")
    if type(cmd.address) is not int:
        raise ValueError("command address must be an integer")
    if cmd.kind == "coil":
        if cmd.address not in COIL_FIELDS:
            raise ValueError("unknown coil address")
        if type(cmd.value) not in (bool, int) or cmd.value not in (0, 1):
            raise ValueError("coil value must be 0 or 1")
        # Match station locking: mode/alarm remain available; control writes lock.
        if state.safe_mode and cmd.address in (0, 1, 2):
            return replace(state)
        updates = {COIL_FIELDS[cmd.address]: bool(cmd.value)}
        if cmd.address == 0 and cmd.value and not state.pump_on:
            updates["pump_start_times_s"] = state.pump_start_times_s + (state.elapsed_s,)
        return replace(state, **updates)
    if cmd.kind == "register":
        if cmd.address not in REGISTER_FIELDS:
            raise ValueError("unknown or twin-only register address")
        if type(cmd.value) is not int or not 0 <= cmd.value <= 65535:
            raise ValueError("register value must be an unsigned 16-bit integer")
        if state.safe_mode:
            return replace(state)
        name, scale = REGISTER_FIELDS[cmd.address]
        value = cmd.value if scale == 1 else cmd.value / scale
        return replace(state, **{name: value})
    raise ValueError("command kind must be 'coil' or 'register'")


def _check_limits(state, minutes_until, seen=()):
    """Report first sampled crossings; settings are constant within a rollout."""
    violations = []
    if "CHLORINE_HIGH" not in seen and state.chlorine_mgl > CHLORINE_HIGH_MGL:
        message = ("Chlorine is already above 4.0 mg/L" if minutes_until == 0 else
                   f"Chlorine exceeds 4.0 mg/L in about {minutes_until:.1f} plant-minutes")
        violations.append(Violation("CHLORINE_HIGH", minutes_until,
                                    state.chlorine_mgl, CHLORINE_HIGH_MGL, message))
    if "TANK_OVERFLOW" not in seen and state.level_pct > TANK_OVERFLOW_PCT:
        message = ("Tank is already above the 95% overflow guard" if minutes_until == 0 else
                   f"Tank exceeds the 95% overflow guard in about {minutes_until:.1f} plant-minutes")
        violations.append(Violation("TANK_OVERFLOW", minutes_until,
                                    state.level_pct, TANK_OVERFLOW_PCT, message))
    starts = sum(state.elapsed_s - PUMP_START_WINDOW_S < t <= state.elapsed_s
                 for t in state.pump_start_times_s)
    for code, value, limit, crossed, description in (
        ("CHLORINE_LOW", state.chlorine_mgl, CHLORINE_LOW_MGL,
         state.chlorine_mgl < CHLORINE_LOW_MGL, "Chlorine below 0.2 mg/L residual guard"),
        ("PRESSURE_LOSS", state.level_pct, PRESSURE_LOSS_PCT,
         state.level_pct < PRESSURE_LOSS_PCT, "Tank below 15% pressure proxy"),
        ("PUMP_SHORT_CYCLE", starts, MAX_PUMP_STARTS_PER_HOUR,
         starts > MAX_PUMP_STARTS_PER_HOUR, "Pump exceeds 6 starts in a rolling plant-hour"),
    ):
        if code not in seen and crossed:
            violations.append(Violation(
                code, minutes_until, value, limit,
                f"{description} at {minutes_until:.1f} plant-minutes",
            ))
    if minutes_until == 0:
        for name, (low, high, unit) in SETPOINT_BOUNDS.items():
            value = getattr(state, name)
            if not low <= value <= high:
                violations.append(Violation(
                    "SETPOINT_OUT_OF_RANGE", 0.0, value, low if value < low else high,
                    f"{name} is {value:g} {unit}, outside {low:g}–{high:g} {unit}",
                ))
                break
        else:
            if state.level_low_sp >= state.level_high_sp:
                violations.append(Violation(
                    "SETPOINT_OUT_OF_RANGE", 0.0, state.level_low_sp, state.level_high_sp,
                    "Low level setpoint must be below the high level setpoint [%]",
                ))
    return violations


def _rollout(state, horizon_min):
    elapsed_s = 0.0
    total_s = float(horizon_min) * 60.0
    trajectory = []
    first_crossings = {}
    while True:
        if not isfinite(state.level_pct) or not isfinite(state.chlorine_mgl):
            raise ValueError("nonfinite model result")
        t_min = elapsed_s / 60.0
        trajectory.append((t_min, state.level_pct, state.chlorine_mgl))
        for violation in _check_limits(state, t_min, first_crossings):
            first_crossings.setdefault(violation.code, violation)
        if elapsed_s >= total_s:
            break
        dt_s = min(10.0, total_s - elapsed_s)
        next_state = step(state, dt_s=dt_s)
        if next_state.pump_on and not state.pump_on:
            # Local control starts at the beginning of this interval. Check
            # before advancing so a start expiring during dt cannot hide #7.
            started = replace(
                state, pump_on=True,
                pump_start_times_s=state.pump_start_times_s + (state.elapsed_s,),
            )
            for violation in _check_limits(started, t_min, first_crossings):
                first_crossings.setdefault(violation.code, violation)
        state = next_state
        elapsed_s += dt_s
    return trajectory, list(first_crossings.values())


def _attribute_violations(candidate, baseline):
    by_code = {violation.code: violation for violation in baseline}
    return [replace(
        violation,
        pre_existing=violation.code in by_code,
        baseline_minutes_until=(by_code[violation.code].minutes_until
                                if violation.code in by_code else None),
    ) for violation in candidate]


def predict(state, cmd, horizon_min=120):
    """Forecast candidate and baseline, including t=0 and the exact horizon.

    Horizon is 0–1440 plant-min. Invalid inputs return unsafe, never an exception.
    safe=True means no CHECKED_LIMITS violation in the candidate trajectory.
    assessment_complete marks successful evaluation of all six demo guards.
    Short-cycle coverage assumes supplied pump history is complete; an omitted
    history means no known starts before the supplied state's plant time.
    """
    try:
        _validate_state(state)
        if (isinstance(horizon_min, bool) or not isinstance(horizon_min, Real)
                or not isfinite(horizon_min) or not 0 <= horizon_min <= MAX_HORIZON_MIN):
            raise ValueError("horizon_min must be finite and within 0–1440 [min]")
        candidate = _apply_command(state, cmd)
        _validate_state(candidate)
        baseline_trajectory, baseline_violations = _rollout(replace(state), horizon_min)
        trajectory, violations = _rollout(candidate, horizon_min)
        return Prediction(
            safe=not violations,
            violations=_attribute_violations(violations, baseline_violations),
            trajectory=trajectory,
            baseline_trajectory=baseline_trajectory,
            assessment_complete=True,
        )
    except Exception:
        # A bad request or model error must never become gateway permission.
        return Prediction(violations=[Violation(
            code="SETPOINT_OUT_OF_RANGE", minutes_until=0.0, value=0.0, limit=0.0,
            message="Cannot forecast: invalid state, command, horizon, or model result",
        )])
