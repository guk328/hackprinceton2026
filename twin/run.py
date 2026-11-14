"""Software water plant driven by actual station relays.

Run from the repo root: python -m twin.run --speedup 60 --reset
The station chooses controls; this process writes only holding 100 and 101.
"""

import argparse
from dataclasses import replace
from math import isfinite
import sys
import time

from physics import PlantState, step


def raw(snapshot, kind, address, maximum=65535):
    """Missing/malformed reads are errors, never substituted defaults."""
    value = snapshot[(kind, address)]
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"invalid {kind} {address}: {value!r}")
    return value


def decode(snapshot, previous=None):
    """Decode wire scales; retain unrounded water state between ticks."""
    coils = [raw(snapshot, "coil", a, 1) for a in range(5)]
    pump = bool(raw(snapshot, "discrete_input", 4, 1))
    doser = bool(raw(snapshot, "discrete_input", 5, 1))
    if doser and (not pump or coils[3]):
        raise ValueError("inconsistent actual relay states; retry with a fresh poll")
    settings = [raw(snapshot, "holding_register", a) for a in range(5)]
    demand = raw(snapshot, "input_register", 0, 1000) / 10.0
    level = raw(snapshot, "holding_register", 100) / 10.0
    chlorine = raw(snapshot, "holding_register", 101) / 100.0
    if settings[0] > 100:
        raise ValueError("doser rate is outside the physical 0–100% envelope")
    base = previous if previous is not None else PlantState(level_pct=level, chlorine_mgl=chlorine)
    history = base.pump_start_times_s
    if previous is not None and pump and not previous.pump_on:
        history += (previous.elapsed_s,)
    return replace(
        base, pump_on=pump, doser_on=doser, demand_pct=demand,
        doser_rate_pct=settings[0], level_high_sp=settings[1] / 10.0,
        level_low_sp=settings[2] / 10.0, chlorine_target_mgl=settings[3] / 100.0,
        logic_version=settings[4], alarm=bool(coils[4]), pump_start_times_s=history,
        # Trust actual relay readback, including interlocks and SAFE_MODE.
        # Running local control here would override the station's decision.
        auto_mode=False, safe_mode=False,
    )


def encode(state):
    """Quantize only at the wire; do not silently wrap or clip overflowing data."""
    values = (state.level_pct * 10, state.chlorine_mgl * 100)
    if any(not isfinite(v) or v < 0 for v in values):
        raise ValueError("invalid physical output")
    wire = tuple(round(v) for v in values)
    if any(v > 65535 for v in wire):
        raise ValueError("physical output exceeds unsigned 16-bit representation")
    return wire


def tick(link, previous=None, speedup=60.0, reset=False):
    """One wall-second's plant update; failures raise and stop the caller.

    Both writes are validated before either is sent. The helper uses separate
    writes, so a disconnect between them can leave a partial update; no retry
    or invented recovery values are sent. Restart with --reset if needed.
    """
    if (isinstance(speedup, bool) or not isinstance(speedup, (int, float))
            or not isfinite(speedup) or not 0 < speedup <= 3600):
        raise ValueError("speedup must be finite, positive and at most 3600")
    state = decode(link.read_all(), previous)
    if reset:
        state = replace(state, level_pct=60.0, chlorine_mgl=1.0,
                        elapsed_s=0.0, pump_start_times_s=())
    else:
        remaining_s = float(speedup)
        while remaining_s > 0:
            dt_s = min(10.0, remaining_s)
            state = step(state, dt_s=dt_s)
            remaining_s -= dt_s
    level, chlorine = encode(state)
    link.write("holding", 100, level)
    link.write("holding", 101, chlorine)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5020)
    parser.add_argument("--speedup", type=float, default=60.0)
    parser.add_argument("--reset", action="store_true", help="reset water to 60%% / 1 mg/L first")
    parser.add_argument("--ticks", type=int, help="stop after this many updates (otherwise run continuously)")
    args = parser.parse_args(argv)
    if not 0 < args.speedup <= 3600 or not isfinite(args.speedup):
        parser.error("--speedup must be finite and in (0, 3600]")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be in 1–65535")
    if args.ticks is not None and args.ticks < 1:
        parser.error("--ticks must be positive")
    # Lazy import permits fake-helper tests without networking dependencies.
    from sim.poke import Link
    state = None
    count = 0
    try:
        with Link(args.host, args.port) as link:
            while args.ticks is None or count < args.ticks:
                start = time.monotonic()
                state = tick(link, state, args.speedup, reset=args.reset and count == 0)
                expected = encode(state)
                actual = (link.read("holding_register", 100), link.read("holding_register", 101))
                if actual != expected:
                    raise RuntimeError(f"readback mismatch: expected {expected}, got {actual}")
                print(f"plant_s={state.elapsed_s:g} level_pct={state.level_pct:.6f} "
                      f"chlorine_mgl={state.chlorine_mgl:.6f} "
                      f"pump={int(state.pump_on)} doser={int(state.doser_on)} "
                      f"demand_pct={state.demand_pct:g} readback={actual}", flush=True)
                count += 1
                if args.ticks is None or count < args.ticks:
                    time.sleep(max(0.0, 1.0 - (time.monotonic() - start)))
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"Twin stopped: {exc}. No further writes; check station and restart.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
