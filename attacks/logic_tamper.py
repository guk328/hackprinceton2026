"""Scenario 4: logic tamper. One setpoint is changed directly on OUR station, behind the gateway.

    python -m attacks.logic_tamper --station-host 127.0.0.1 --station-port 5020

This is the only script that talks to the station itself, so the target must be given
explicitly: our ESP32 or the software station, nothing else. The gateway should notice the
LOGIC_HASH mismatch within a few seconds and the settings should come back (the agent's
revert tool, or plain code when no model is configured). The script waits for that.
"""
import argparse
import time

from attacks._common import emit

TAMPER_ADDR, TAMPER_NAME, TAMPER_VALUE = 1, "LEVEL_HIGH_SP", 990   # 99 %: the pump would never stop in time


def logic(link):
    return [link.read("holding_register", a) for a in range(5)]


def run(link, wait_s=90.0, sleep=time.sleep, clock=time.monotonic):
    before = logic(link)
    hash_before = link.read("input_register", 10)
    if before[TAMPER_ADDR] == TAMPER_VALUE:
        raise ValueError("station already holds the tampered value; reset it first")
    emit("request", operation="direct write", name=TAMPER_NAME, address=TAMPER_ADDR,
         value=TAMPER_VALUE, old_value=before[TAMPER_ADDR])
    link.write("holding", TAMPER_ADDR, TAMPER_VALUE)
    for _ in range(20):   # the station recomputes the hash on its next control tick
        hash_tampered = link.read("input_register", 10)
        if hash_tampered != hash_before:
            break
        sleep(0.1)
    emit("response", operation="direct write", logic_hash_before=hash_before,
         logic_hash_after=hash_tampered)
    if hash_tampered == hash_before:
        raise RuntimeError("LOGIC_HASH did not change: the station is not hashing holding 0-4")
    start = clock()
    while clock() - start < wait_s:
        sleep(1.0)
        if logic(link) == before:
            emit("scenario_result", scenario=4, passed=True, reverted_after_s=round(clock() - start, 1),
                 logic_hash=link.read("input_register", 10),
                 note="Confirm the LOGIC_TAMPER event and the revert step in the gateway log.")
            return
    raise RuntimeError(f"settings were not reverted within {wait_s:g} s (is the gateway running?); "
                       f"station still holds {TAMPER_NAME} = {TAMPER_VALUE}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--station-host", required=True, help="our ESP32 or 127.0.0.1 (software station)")
    parser.add_argument("--station-port", type=int, required=True, help="502 (ESP32) or 5020 (software station)")
    parser.add_argument("--wait", type=float, default=90.0, help="seconds to wait for the revert")
    args = parser.parse_args(argv)
    if args.station_port not in (502, 5020):
        parser.error("this script writes to the station itself: port 502 or 5020")
    from sim.poke import Link
    try:
        with Link(args.station_host, args.station_port) as link:
            run(link, args.wait)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        emit("scenario_result", scenario=4, passed=False, error=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
