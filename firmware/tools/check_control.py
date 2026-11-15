"""Step 5 acceptance check: does a station behave like sim/station.py (the reference)?

    python -m firmware.tools.check_control --host 172.20.10.9 --port 502     (the ESP32)
    python -m firmware.tools.check_control --host 127.0.0.1 --port 5020      (software station)

Talks to the station directly. Stop the twin and the gateway first: the script writes
TANK_LEVEL_PCT itself to walk the level across the setpoints, and it clicks the relays.
Everything is put back to the register-map defaults at the end.
"""
import argparse
import sys
import time

from gateway.station import logic_hash
from sim.poke import Link

PUMP, DOSER, AUTO, SAFE, ALARM = 0, 1, 2, 3, 4
RATE, HIGH, LOW, LEVEL = 0, 1, 2, 100
DI_HIGH_SWITCH, DI_PUMP, DI_DOSER = 3, 4, 5
fails = []


def settle(link, want: dict, timeout=1.5):
    """Wait until every (kind, addr) in `want` reads its value; returns (ok, seconds, got)."""
    start = time.monotonic()
    while True:
        got = {k: link.read(*k) for k in want}
        if got == want or time.monotonic() - start > timeout:
            return got == want, time.monotonic() - start, got
        time.sleep(0.03)


def check(link, label, want: dict, timeout=1.5):
    ok, took, got = settle(link, want, timeout)
    print(f"{'ok  ' if ok else 'FAIL'} {label} ({took * 1000:.0f} ms)" + ("" if ok else f"  wanted {want}, got {got}"))
    if not ok:
        fails.append(label)
    return took


def restore(link):
    link.write("coil", SAFE, 0)
    for addr, v in ((RATE, 10), (HIGH, 850), (LOW, 400), (3, 100), (4, 1), (LEVEL, 600), (101, 100)):
        link.write("holding", addr, v)
    for addr, v in ((AUTO, 1), (PUMP, 0), (DOSER, 1), (ALARM, 0)):
        link.write("coil", addr, v)


def run(link):
    co, di, hr, ir = "coil", "discrete_input", "holding_register", "input_register"
    if link.read(di, DI_HIGH_SWITCH):
        raise RuntimeError("HIGH_LEVEL_SWITCH is active (something in front of the IR module?): "
                           "the interlock holds the pump off, clear it and run again")
    restore(link)
    check(link, "defaults: pump and doser off at 60 %", {(di, DI_PUMP): 0, (di, DI_DOSER): 0, (co, PUMP): 0})

    link.write("holding", LEVEL, 300)
    check(link, "auto: level 30 % < low SP starts pump and doser, PUMP_CMD written back",
          {(co, PUMP): 1, (di, DI_PUMP): 1, (di, DI_DOSER): 1})
    link.write("holding", LEVEL, 600)
    time.sleep(0.4)
    check(link, "auto: back in the band the pump keeps running (coil is the memory)", {(di, DI_PUMP): 1})
    link.write("holding", RATE, 0)
    check(link, "rate 0 stops the doser, pump unaffected", {(di, DI_DOSER): 0, (di, DI_PUMP): 1})
    link.write("holding", RATE, 10)
    link.write("holding", LEVEL, 900)
    check(link, "auto: level 90 % > high SP stops pump and doser, PUMP_CMD written back",
          {(co, PUMP): 0, (di, DI_PUMP): 0, (di, DI_DOSER): 0})
    link.write("holding", LEVEL, 600)

    link.write("coil", AUTO, 0)
    link.write("coil", PUMP, 1)
    took = check(link, "manual: PUMP_CMD = 1 runs the pump", {(di, DI_PUMP): 1})
    if took > 0.3:
        fails.append("relay latency over 300 ms")
        print(f"FAIL relay followed the coil after {took * 1000:.0f} ms (limit 300)")
    link.write("holding", LEVEL, 900)
    time.sleep(0.4)
    check(link, "manual: level above high SP does not stop the pump", {(di, DI_PUMP): 1})
    link.write("coil", PUMP, 0)
    check(link, "manual: PUMP_CMD = 0 stops it", {(di, DI_PUMP): 0})
    link.write("holding", LEVEL, 600)
    link.write("coil", AUTO, 1)

    link.write("holding", HIGH, 700)          # a non-default setpoint that SAFE_MODE must ignore
    link.write("coil", PUMP, 1)
    check(link, "pump on before safe mode", {(di, DI_PUMP): 1, (di, DI_DOSER): 1})
    values = {a: link.read(hr, a) for a in range(5)}
    check(link, "LOGIC_HASH is the CRC16 of holding 0-4", {(ir, 10): logic_hash(values)})
    link.write("coil", SAFE, 1)
    check(link, "safe mode: DOSER_CMD forced to 0, doser off, pump keeps running",
          {(co, DOSER): 0, (di, DI_DOSER): 0, (di, DI_PUMP): 1})
    link.write("holding", RATE, 55)
    link.write("holding", HIGH, 990)
    link.write("coil", AUTO, 0)
    link.write("coil", DOSER, 1)
    time.sleep(0.4)
    check(link, "safe mode: remote writes to holding 0-4 and coils 0-2 are ignored",
          {(hr, RATE): 10, (hr, HIGH): 700, (co, AUTO): 1, (co, DOSER): 0, (ir, 10): logic_hash(values)})
    link.write("holding", LEVEL, 800)
    time.sleep(0.4)
    check(link, "safe mode: default 85 % setpoint is used, not the station's 70 %", {(di, DI_PUMP): 1})
    link.write("holding", LEVEL, 900)
    check(link, "safe mode: pump stops above the default high SP", {(di, DI_PUMP): 0, (co, PUMP): 0})
    link.write("holding", LEVEL, 300)
    check(link, "safe mode: pump starts below the default low SP, doser stays off",
          {(di, DI_PUMP): 1, (di, DI_DOSER): 0})
    link.write("coil", ALARM, 1)
    check(link, "ALARM can still be set in safe mode", {(co, ALARM): 1})
    link.write("coil", SAFE, 0)
    check(link, "safe mode can be cleared by a direct write", {(co, SAFE): 0})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    a = ap.parse_args(argv)
    with Link(a.host, a.port) as link:
        try:
            run(link)
        finally:
            restore(link)
            print("station restored to defaults")
    print(f"\n{len(fails)} failed: {fails}" if fails else "\nall checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
