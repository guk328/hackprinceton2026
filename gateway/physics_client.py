"""The gateway's door to the physics model.

Uses the real `physics` package; falls back to gateway/physics_stub.py if it will not import.

plant_state()  StationSnapshot -> PlantState in engineering units
check()        predict() with a hard timeout; raises on error or timeout, the caller holds
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

from gateway.station import StationSnapshot

try:
    from physics import Command, PlantState, Prediction, Violation, predict, step
    USING_REAL_PHYSICS = True
except ImportError:
    from gateway.physics_stub import Command, PlantState, Prediction, Violation, predict, step
    USING_REAL_PHYSICS = False

# Slowest scenarios: overflow ~200 plant-min, pressure loss ~240 (measured on physics/model.py).
# predict() costs ~0.12 ms per plant-minute, so 270 min is ~30 ms on a fast core. Measured on
# the demo laptop (i7-12700H): ~30 ms on a P-core, 100-135 ms when Windows puts the worker
# thread on an E-core, which it does for short bursts between idle gaps. The timeout has to
# clear the slow core, or clean writes get held at random.
HORIZON_MIN = 270
DEFAULT_TIMEOUT_S = 0.25


def timeout_from_env(raw: str | None) -> float:
    """PHYSICS_TIMEOUT_S in seconds. Missing, unparseable or not positive: the default."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return value if 0 < value < float("inf") else DEFAULT_TIMEOUT_S


load_dotenv()  # here, not only in the proxy's main(): this module is imported before main() runs
TIMEOUT_S = timeout_from_env(os.getenv("PHYSICS_TIMEOUT_S"))

# ---------------------------------------------------------------- gateway side

# A timed-out predict() cannot be stopped; it keeps its worker until it returns. A slot is
# held for exactly that long, so a call that is submitted always has a free worker and never
# waits behind a stale one. With no slot left the check fails at once instead of queueing.
MAX_IN_FLIGHT = 8
_pool = ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT, thread_name_prefix="physics")
_slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)


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
          timeout_s: float | None = None) -> Prediction:
    """predict() on a worker thread. Raises TimeoutError after timeout_s (default TIMEOUT_S),
    or at once if MAX_IN_FLIGHT earlier calls are still running, or whatever predict raised.
    Every one of those makes the caller hold."""
    if not _slots.acquire(blocking=False):
        raise TimeoutError(f"{MAX_IN_FLIGHT} physics calls are still running")
    try:
        future = _pool.submit(predict, state, cmd, horizon_min)
    except BaseException:
        _slots.release()
        raise
    future.add_done_callback(lambda _: _slots.release())  # when predict() really ends
    return future.result(timeout=TIMEOUT_S if timeout_s is None else timeout_s)
