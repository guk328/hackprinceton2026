"""Read/poke a Modbus station.

    python -m sim.poke [--host H] [--port N] read all
    python -m sim.poke [--host H] [--port N] write holding|coil ADDR VALUE
    python -m sim.poke set NAME RAW     (software station only: PRESENCE, DEMAND_PCT, ...)

For code that polls (the twin, the gateway), use one persistent `Link`:

    from sim.poke import Link
    with Link("127.0.0.1", 5020) as st:
        snap = st.read_all()                    # {(kind, addr): raw}
        st.write("holding", 100, 612)           # TANK_LEVEL_PCT = 61.2 %
"""
import argparse
import json

from pymodbus.client import ModbusTcpClient

from gateway.registers import load_registers
from sim.station import COMPUTED, INPUTS_PATH, SETTABLE, write_inputs

READERS = {
    "coil": "read_coils",
    "discrete_input": "read_discrete_inputs",
    "input_register": "read_input_registers",
    "holding_register": "read_holding_registers",
}


class Link:
    """One persistent Modbus TCP connection. Use it for anything that polls:
    the ESP32 accepts only a few sockets, so do not reconnect every read.
    Errors raise (ConnectionError / RuntimeError); never guess a value."""

    def __init__(self, host="127.0.0.1", port=5020, regs=None, timeout=2.0):
        self.host, self.port = host, port
        self.regs = regs or load_registers()
        self.client = ModbusTcpClient(host, port=port, timeout=timeout)

    def __enter__(self):
        if not self.client.connect():
            raise ConnectionError(f"cannot reach station {self.host}:{self.port}")
        return self

    def __exit__(self, *exc):
        self.client.close()

    def read(self, kind, addr) -> int:
        r = getattr(self.client, READERS[kind])(addr, count=1, device_id=self.regs.unit_id)
        if r.isError():
            raise RuntimeError(f"read {kind} {addr}: {r}")
        return int(r.bits[0] if kind in ("coil", "discrete_input") else r.registers[0])

    def read_all(self) -> dict[tuple[str, int], int]:
        return {key: self.read(*key) for key in sorted(self.regs._by_key)}

    def write(self, what, addr, value):
        """what: 'coil' or 'holding'; value is the raw wire value."""
        if what == "coil":
            r = self.client.write_coil(addr, bool(value), device_id=self.regs.unit_id)
        elif what == "holding":
            r = self.client.write_register(addr, int(value), device_id=self.regs.unit_id)
        else:
            raise ValueError(f"cannot write {what!r}")
        if r.isError():
            raise RuntimeError(f"write {what} {addr}: {r}")


def read_all(host="127.0.0.1", port=5020, regs=None) -> dict[tuple[str, int], int]:
    with Link(host, port, regs) as st:
        return st.read_all()


def write(what, addr, value, host="127.0.0.1", port=5020, regs=None):
    with Link(host, port, regs) as st:
        st.write(what, addr, value)


def set_input(name, raw, path=INPUTS_PATH, regs=None):
    """Software station only: set a read-only input (raw wire value) via its file."""
    regs = regs or load_registers()
    reg = regs.by_name(name.upper())
    if reg is None or reg.kind not in SETTABLE or reg.name in COMPUTED:
        ok = [r.name for r in regs._by_key.values() if r.kind in SETTABLE and r.name not in COMPUTED]
        raise ValueError(f"{name}: settable inputs are {', '.join(ok)}")
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        values = {}
    values[reg.name] = int(raw)
    write_inputs(values, path)


def main():
    regs = load_registers()
    ap = argparse.ArgumentParser(prog="sim.poke")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=regs.ports["sim_station"])
    # same options after the subcommand too ("read all --port 1502")
    conn = argparse.ArgumentParser(add_help=False)
    conn.add_argument("--host", default=argparse.SUPPRESS)
    conn.add_argument("--port", type=int, default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read", parents=[conn])
    r.add_argument("what", choices=["all"])
    w = sub.add_parser("write", parents=[conn])
    w.add_argument("what", choices=["holding", "coil"])
    w.add_argument("addr", type=int)
    w.add_argument("value", type=int)
    s = sub.add_parser("set", help="software station only: set a read-only input (raw value)")
    s.add_argument("name")
    s.add_argument("value", type=int)
    a = ap.parse_args()
    if a.cmd == "set":
        set_input(a.name, a.value, regs=regs)
        print(f"set {a.name.upper()} = {a.value} (sim picks it up within one tick)")
        return
    if a.cmd == "write":
        write(a.what, a.addr, a.value, a.host, a.port, regs)
        print(f"wrote {a.what} {a.addr} = {a.value}")
        return
    for (kind, addr), v in read_all(a.host, a.port, regs).items():
        reg = regs.lookup(kind, addr)
        print(f"{kind:17} {addr:3} {reg.name:18} {v}")


if __name__ == "__main__":
    main()
