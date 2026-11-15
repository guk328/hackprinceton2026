"""Scenario 1: a fixed overdose attempt, only through our own demo gateway."""
from attacks._common import arguments, emit, read, write


def run(link):
    pump = read(link, "discrete_input", 4, "PUMP_RUNNING")
    doser = read(link, "discrete_input", 5, "DOSER_RUNNING")
    safe = read(link, "coil", 3, "SAFE_MODE")
    before = read(link, "holding_register", 0, "DOSER_RATE_PCT")
    if not pump or not doser or safe or before != 10:
        raise ValueError("run normal_day first; fixture needs pump+doser running, safe mode off, rate 10%")
    write(link, "register", 0, 100, "DOSER_RATE_PCT", expected="HOLD")
    if read(link, "holding_register", 0, "DOSER_RATE_PCT") != before:
        raise RuntimeError("dose rate changed despite the HOLD response")
    # The held rate write must not change station state. This second command
    # is harmless at the unchanged 10% rate and is judged independently.
    write(link, "coil", 1, 1, "DOSER_CMD", expected="ALLOW")
    after = read(link, "holding_register", 0, "DOSER_RATE_PCT")
    if after != before:
        raise RuntimeError("dose rate changed during the scenario")
    emit("scenario_result", scenario=1, passed=True, attempted_rate_pct=100,
         rate_before_pct=before, rate_after_pct=after,
         note="Confirm CHLORINE_HIGH reason and crossing time in gateway evidence; 0x06 alone is not a reason.")


def main(argv=None):
    args = arguments(__doc__, argv)
    from sim.poke import Link
    try:
        with Link(args.host, args.port) as link:
            run(link)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        emit("scenario_result", scenario=1, passed=False, error=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
