"""Scenario 2: drain the town. AUTO_MODE -> 0, then PUMP_CMD -> 0, at high demand, via our gateway.

Both writes together leave the pump off with nothing to restart it. Which of the two the
gateway holds depends on the pump: running, AUTO_MODE -> 0 alone is harmless and the stop is
held (PRESSURE_LOSS); idle between the setpoints, AUTO_MODE -> 0 is already the drain.
"""
from attacks._common import arguments, emit, read, write

MIN_DEMAND_RAW = 700   # 70 %: below this the tank does not reach the pressure limit in the gateway's horizon


def run(link):
    demand = read(link, "input_register", 0, "DEMAND_PCT")
    auto = read(link, "coil", 2, "AUTO_MODE")
    safe = read(link, "coil", 3, "SAFE_MODE")
    if safe or not auto or demand < MIN_DEMAND_RAW:
        raise ValueError("fixture needs AUTO_MODE on, SAFE_MODE off and demand of at least 70 % "
                         "(turn the pot up, or on the sim: python -m sim.poke set DEMAND_PCT 1000)")
    first = write(link, "coil", 2, 0, "AUTO_MODE", expected=("ALLOW", "HOLD"))
    second = write(link, "coil", 0, 0, "PUMP_CMD",
                   expected="HOLD" if first == "ALLOW" else ("ALLOW", "HOLD"))
    auto_after = read(link, "coil", 2, "AUTO_MODE")
    pump_after = read(link, "coil", 0, "PUMP_CMD")
    if not auto_after and not pump_after:
        raise RuntimeError("station is in manual with the pump off: the drain got through")
    if not auto_after:   # leave the station as we found it; this is an ordinary safe write
        write(link, "coil", 2, 1, "AUTO_MODE", expected="ALLOW")
    emit("scenario_result", scenario=2, passed=True, demand_pct=demand / 10,
         auto_mode_write=first, pump_stop_write=second,
         note="Confirm the PRESSURE_LOSS reason and time in the gateway event; 0x06 alone is not a reason.")


def main(argv=None):
    args = arguments(__doc__, argv)
    from sim.poke import Link
    try:
        with Link(args.host, args.port) as link:
            run(link)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        emit("scenario_result", scenario=2, passed=False, error=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
