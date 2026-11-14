"""Read/poke a Modbus station. Step 3: read only. Usage: python -m sim.poke [--port N] read all"""
import argparse

from pymodbus.client import ModbusTcpClient

from gateway.registers import load_registers

READERS = {
    "coil": "read_coils",
    "discrete_input": "read_discrete_inputs",
    "input_register": "read_input_registers",
    "holding_register": "read_holding_registers",
}


def read_all(host="127.0.0.1", port=5020, regs=None) -> dict[tuple[str, int], int]:
    regs = regs or load_registers()
    out = {}
    with ModbusTcpClient(host, port=port) as c:
        for (kind, addr), _ in sorted(regs._by_key.items()):
            r = getattr(c, READERS[kind])(addr, count=1, device_id=regs.unit_id)
            if r.isError():
                raise RuntimeError(f"read {kind} {addr}: {r}")
            val = r.bits[0] if "bits" in dir(r) and kind in ("coil", "discrete_input") else r.registers[0]
            out[(kind, addr)] = int(val)
    return out


def main():
    regs = load_registers()
    ap = argparse.ArgumentParser(prog="sim.poke")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=regs.ports["sim_station"])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("what", choices=["all"])
    a = ap.parse_args()
    for (kind, addr), v in read_all(a.host, a.port, regs).items():
        reg = regs.lookup(kind, addr)
        print(f"{kind:17} {addr:3} {reg.name:18} {v}")


if __name__ == "__main__":
    main()
