"""The gateway's door to the physics model.

Uses the real `physics` package; falls back to gateway/physics_stub.py if it will not import.

plant_state()  StationSnapshot -> PlantState in engineering units
check()        predict() with a hard timeout; raises on error or timeout, the caller holds
"""
from concurrent.futures import ThreadPoolExecutor

from gateway.station import StationSnapshot

try:
    from physics import Command, PlantState, Prediction, Violation, predict, step
    USING_REAL_PHYSICS = True
except ImportError:
    from gateway.physics_stub import Command, PlantState, Prediction, Violation, predict, step
    USING_REAL_PHYSICS = False

# Slowest scenarios: overflow ~200 plant-min, pressure loss ~240 (measured on physics/model.py).
# predict() costs ~0.12 ms per plant-minute, so 270 min is ~35 ms; TIMEOUT_S stays under 100 ms.
HORIZON_MIN = 270
TIMEOUT_S = 0.09

# ---------------------------------------------------------------- gateway side

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="physics")


def plant_state(station: StationSnapshot) -> PlantState:
    """Raises if the snapshot is stale or incomplete; the caller holds."""
    if not station.connected:
        raise ValueError("station is offline")
    c, h, di = station.coils, station.holding, station.discrete_inputs
    fields = {
        # In auto the station writes its choice back into PUMP_CMD/DOSER_CMD; the one thing it
        # overrides is the high-level interlock. PUMP_RUNNING is not used: it trails a write
        # by a station tick, which would hide a dose that is about to happen.
        "pump_on": bool(c["PUMP_CMD"]) and not di.get("HIGH_LEVEL_SWITCH"),
        "doser_on": bool(c["DOSER_CMD"]) and not di.get("HIGH_LEVEL_SWITCH"),
        "safe_mode": bool(c["SAFE_MODE"]),
        "alarm": bool(c["ALARM"]),
    }
    extra = {k: v for k, v in fields.items() if k in PlantState.__dataclass_fields__}
    return PlantState(
        level_pct=h["TANK_LEVEL_PCT"] / 10,
        chlorine_mgl=h["CHLORINE_MGL"] / 100,
        demand_pct=station.input_registers["DEMAND_PCT"] / 10,
        doser_rate_pct=h["DOSER_RATE_PCT"],
        level_high_sp=h["LEVEL_HIGH_SP"] / 10,
        level_low_sp=h["LEVEL_LOW_SP"] / 10,
        auto_mode=bool(c["AUTO_MODE"]),
        **extra,
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
