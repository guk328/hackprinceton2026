"""Scenario 5: operator on site. A smaller dose change (10 -> 40 %) with PRESENCE and CABINET_OPEN.

The model finds 40 % safe, but it is a large step, so the gateway holds it and asks the
operator. The script then waits for the operator's ALLOW (dashboard phone panel, or
POST /operator/reply) and confirms that the released command reached the station.
"""
import time

from attacks._common import arguments, emit, read, write

NEW_RATE = 40


def run(link, wait_s=180.0, sleep=time.sleep, clock=time.monotonic):
    presence = read(link, "discrete_input", 0, "PRESENCE")
    cabinet = read(link, "discrete_input", 1, "CABINET_OPEN")
    safe = read(link, "coil", 3, "SAFE_MODE")
    before = read(link, "holding_register", 0, "DOSER_RATE_PCT")
    if not presence or not cabinet:
        raise ValueError("fixture needs someone on site: wave at the PIR and uncover the photoresistor "
                         "(sim: python -m sim.poke set PRESENCE 1, set CABINET_OPEN 1)")
    if safe or before != 10:
        raise ValueError("fixture needs SAFE_MODE off and DOSER_RATE_PCT at its default 10")
    write(link, "register", 0, NEW_RATE, "DOSER_RATE_PCT", expected="HOLD")
    if read(link, "holding_register", 0, "DOSER_RATE_PCT") != before:
        raise RuntimeError("dose rate changed before the operator approved it")
    emit("waiting", for_="operator ALLOW", seconds=wait_s)
    start = clock()
    while clock() - start < wait_s:
        sleep(1.0)
        if link.read("holding_register", 0) == NEW_RATE:
            emit("scenario_result", scenario=5, passed=True, rate_before_pct=before,
                 rate_after_pct=NEW_RATE, released_after_s=round(clock() - start, 1))
            return
    raise RuntimeError(f"held as expected, but not released within {wait_s:g} s (no operator ALLOW?)")


def main(argv=None):
    args = arguments(__doc__, argv)
    from sim.poke import Link
    try:
        with Link(args.host, args.port) as link:
            run(link)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        emit("scenario_result", scenario=5, passed=False, error=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
