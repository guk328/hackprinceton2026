"""Scenario 3: overflow. LEVEL_HIGH_SP -> 1000 (100 %), then PUMP_CMD -> 1, via our gateway.

The setpoint write is always held (above the 95 % envelope). The "tank overflows in N min"
forecast is added only when the tank can fill inside the gateway's horizon: demand at or
below about 45 % and level at or above about 50 %.
"""
from attacks._common import arguments, emit, read, write

MAX_DEMAND_RAW = 450


def run(link):
    demand = read(link, "input_register", 0, "DEMAND_PCT")
    safe = read(link, "coil", 3, "SAFE_MODE")
    before = read(link, "holding_register", 1, "LEVEL_HIGH_SP")
    if safe or not 500 <= before <= 950:
        raise ValueError("fixture needs SAFE_MODE off and a valid LEVEL_HIGH_SP")
    write(link, "register", 1, 1000, "LEVEL_HIGH_SP", expected="HOLD")
    after = read(link, "holding_register", 1, "LEVEL_HIGH_SP")
    if after != before:
        raise RuntimeError("high setpoint changed despite the HOLD response")
    # With the setpoint unchanged this is an ordinary command, judged by itself.
    pump = write(link, "coil", 0, 1, "PUMP_CMD", expected=("ALLOW", "HOLD"))
    emit("scenario_result", scenario=3, passed=True, demand_pct=demand / 10,
         high_sp_before_pct=before / 10, high_sp_after_pct=after / 10, pump_write=pump,
         overflow_forecast_expected=demand <= MAX_DEMAND_RAW,
         note="Confirm TANK_OVERFLOW and its time in the gateway event. At high demand only "
              "the envelope / SETPOINT_OUT_OF_RANGE reasons appear.")


def main(argv=None):
    args = arguments(__doc__, argv)
    from sim.poke import Link
    try:
        with Link(args.host, args.port) as link:
            run(link)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        emit("scenario_result", scenario=3, passed=False, error=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
