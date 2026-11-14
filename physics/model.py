"""Well-mixed tank with provisional parameters from PROMPT_ME.md."""

from dataclasses import dataclass, replace
from math import isfinite


TANK_CAPACITY_M3 = 400.0
PUMP_FLOW_M3_H = 60.0
MAX_DEMAND_M3_H = 45.0
MAX_DOSE_G_H = 600.0
DECAY_PER_H = 0.05


@dataclass(frozen=True)
class PlantState:
    """Unscaled values: percentages [%] and chlorine [mg/L]."""

    level_pct: float = 60.0
    chlorine_mgl: float = 1.0
    demand_pct: float = 40.0
    pump_on: bool = True
    doser_on: bool = True
    doser_rate_pct: float = 10.0
    level_high_sp: float = 85.0
    level_low_sp: float = 40.0
    auto_mode: bool = True
    safe_mode: bool = False
    alarm: bool = False
    chlorine_target_mgl: float = 1.0
    logic_version: int = 1


@dataclass(frozen=True)
class Command:
    """Wire command: kind ('coil'/'register'), address, and raw wire value."""

    kind: str
    address: int
    value: int


def step(state: PlantState, dt_s: float = 1.0) -> PlantState:
    """Advance one explicit Euler step; use short intervals (1–10 seconds).

    V is m³, M is g, C is mg/L (= g/m³), and time in the balances is h.
    Incoming carrier water is assumed to have zero chlorine; no dose reaches
    the tank without pump flow. Level above 100% is retained for forecasting:
    overflow routing and delivered pressure are outside this simple model.
    """
    if not isfinite(dt_s) or dt_s < 0:
        raise ValueError("dt_s must be finite and nonnegative [s]")
    values = (
        state.level_pct, state.chlorine_mgl, state.demand_pct,
        state.doser_rate_pct, state.level_high_sp, state.level_low_sp,
    )
    if not all(isfinite(value) for value in values):
        raise ValueError("state measurements and settings must be finite")
    if state.level_pct < 0 or state.chlorine_mgl < 0:
        raise ValueError("level [%] and chlorine [mg/L] must be nonnegative")
    if not 0 <= state.demand_pct <= 100 or not 0 <= state.doser_rate_pct <= 100:
        raise ValueError("demand and doser rate must be within 0–100 [%]")
    if dt_s == 0:
        return replace(state)

    pump_on = state.pump_on
    if state.auto_mode or state.safe_mode:
        # SAFE_MODE uses the local defaults, independent of remote setpoints.
        low_sp = 40.0 if state.safe_mode else state.level_low_sp
        high_sp = 85.0 if state.safe_mode else state.level_high_sp
        if state.level_pct < low_sp:
            pump_on = True
        elif state.level_pct > high_sp:
            pump_on = False

    doser_on = state.doser_on and not state.safe_mode

    dt_h = dt_s / 3600.0
    volume_m3 = state.level_pct / 100.0 * TANK_CAPACITY_M3
    inflow_m3_h = PUMP_FLOW_M3_H if pump_on else 0.0
    demand_m3_h = state.demand_pct / 100.0 * MAX_DEMAND_M3_H
    # Town delivery cannot exceed stored water plus this step's inflow.
    outflow_m3_h = min(demand_m3_h, inflow_m3_h + volume_m3 / dt_h)
    concentration_g_m3 = state.chlorine_mgl if volume_m3 > 0 else 0.0
    mass_g = volume_m3 * concentration_g_m3
    dose_g_h = (
        MAX_DOSE_G_H * state.doser_rate_pct / 100.0
        if pump_on and doser_on else 0.0
    )
    next_volume_m3 = volume_m3 + (inflow_m3_h - outflow_m3_h) * dt_h
    next_mass_g = mass_g + (
        dose_g_h - outflow_m3_h * concentration_g_m3 - DECAY_PER_H * mass_g
    ) * dt_h
    if not isfinite(next_volume_m3) or not isfinite(next_mass_g):
        raise ValueError("volume [m³] or chlorine mass [g] overflowed")
    next_volume_m3 = max(0.0, next_volume_m3)
    next_mass_g = max(0.0, next_mass_g)
    # A dry tank retains no dissolved mass and has no concentration to divide by.
    next_chlorine_mgl = next_mass_g / next_volume_m3 if next_volume_m3 > 0 else 0.0
    return replace(
        state,
        level_pct=next_volume_m3 / TANK_CAPACITY_M3 * 100.0,
        chlorine_mgl=next_chlorine_mgl,
        pump_on=pump_on,
        doser_on=doser_on,
    )
