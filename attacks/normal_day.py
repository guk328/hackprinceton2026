"""Scenario 0: pump on plus a small valid high-setpoint change via our gateway."""
from attacks._common import arguments, emit, read, write


def run(link):
    high = read(link, "holding_register", 1, "LEVEL_HIGH_SP")
    low = read(link, "holding_register", 2, "LEVEL_LOW_SP")
    safe = read(link, "coil", 3, "SAFE_MODE")
    if safe or not 500 <= high <= 950 or not 200 <= low <= 700 or low >= high:
        raise ValueError("use the documented normal fixture: safe mode off and valid level setpoints")
    # One percentage-point change that remains inside the current envelope.
    target = high - 10 if high - 10 >= 500 and high - 10 > low else high + 10
    if target > 950:
        raise ValueError("no small valid high-setpoint change available")
    write(link, "coil", 0, 1, "PUMP_CMD")
    if read(link, "coil", 0, "PUMP_CMD") != 1:
        raise RuntimeError("pump command readback did not become 1; check local control/interlocks")
    write(link, "register", 1, target, "LEVEL_HIGH_SP")
    if read(link, "holding_register", 1, "LEVEL_HIGH_SP") != target:
        raise RuntimeError("high setpoint readback differs from the acknowledged command")
    emit("scenario_result", scenario=0, passed=True, pump_command=1,
         old_high_sp_pct=high / 10, new_high_sp_pct=target / 10)


def main(argv=None):
    args = arguments(__doc__, argv)
    from sim.poke import Link
    try:
        with Link(args.host, args.port) as link:
            run(link)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        emit("scenario_result", scenario=0, passed=False, error=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
