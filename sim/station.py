"""Software station: Modbus TCP server on 127.0.0.1:5020 with the yaml defaults.

Read-only inputs (DEMAND_PCT, PRESENCE, CABINET_OPEN, ...) cannot be written over
Modbus, so the sim takes them from a JSON file of raw values, re-read when it
changes: `python -m sim.poke set PRESENCE 1`. Sim only; the ESP32 has real sensors.
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import ModbusTcpServer

from gateway.registers import load_registers

SIZE = 200  # covers addr 0-11 and the twin registers at 100-101
INPUTS_PATH = Path(__file__).resolve().parent / "inputs.json"
BOOT_INPUTS = {"DEMAND_PCT": 400}  # 40 % (README 4.3 example) so the twin's tank moves
SETTABLE = ("discrete_input", "input_register")
COMPUTED = ("LOGIC_HASH", "UPTIME_S")  # the station writes these itself


def _block(regs, kind):
    values = [0] * SIZE
    for addr, v in regs.defaults(kind).items():
        values[addr] = v
    return ModbusSequentialDataBlock(1, values)  # 3.16 stores at address-1


def make_context(regs=None) -> ModbusServerContext:
    regs = regs or load_registers()
    dev = ModbusDeviceContext(
        co=_block(regs, "coil"),
        di=_block(regs, "discrete_input"),
        ir=_block(regs, "input_register"),
        hr=_block(regs, "holding_register"),
    )
    return ModbusServerContext(devices={regs.unit_id: dev}, single=False)


def crc16(data: bytes) -> int:
    """CRC16, Modbus polynomial (0xA001 reflected, init 0xFFFF)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class Station:
    """Control logic around the datastore: auto pump, SAFE_MODE, LOGIC_HASH, UPTIME_S."""

    FC = {"coil": (1, 5), "discrete_input": (2, 2), "input_register": (4, 4), "holding_register": (3, 6)}

    def __init__(self, regs, inputs_path=INPUTS_PATH):
        self.regs = regs
        self.rt = None  # SimRuntime, attached once the server exists
        self.t0 = time.monotonic()
        self.in_step = False  # internal writes bypass the SAFE_MODE lock
        self.inputs_path = Path(inputs_path)
        self._inputs_text = None

    async def apply_inputs(self):
        """Copy the inputs file into the datastore when it has changed."""
        try:  # compare contents: Windows mtimes are too coarse for quick successive sets
            text = self.inputs_path.read_text(encoding="utf-8")
            if text == self._inputs_text:
                return
            data = json.loads(text)
        except (OSError, ValueError):
            return
        self._inputs_text = text
        for name, raw in data.items():
            reg = self.regs.by_name(name)
            if reg and reg.kind in SETTABLE and name not in COMPUTED:
                await self.put(reg.kind, reg.addr, int(raw) & 0xFFFF)

    async def get(self, kind, addr):
        v = await self.rt.async_getValues(self.FC[kind][0], addr, 1)
        return int(v[0])

    async def put(self, kind, addr, val):
        self.in_step = True
        try:
            v = bool(val) if kind in ("coil", "discrete_input") else int(val)
            await self.rt.async_setValues(self.FC[kind][1], addr, [v])
        finally:
            self.in_step = False

    async def action(self, fc, start_address, address, count, registers, set_values):
        """SAFE_MODE: remote writes to holding 0-4 and coils 0-2 are silently ignored."""
        if set_values is None or self.in_step or fc not in (5, 6, 15, 16):
            return None
        if not await self.get("coil", 3):
            return None
        kind, locked = ("coil", range(0, 3)) if fc in (5, 15) else ("holding_register", range(0, 5))
        for i in range(len(set_values)):
            if address + i in locked:
                set_values[i] = bool(await self.get(kind, address + i)) if kind == "coil" else await self.get(kind, address + i)
        return None

    async def step(self):
        await self.apply_inputs()
        g, r = self.get, self.regs
        safe = await g("coil", 3)
        lo, hi = (r.lookup("holding_register", 2).default, r.lookup("holding_register", 1).default) if safe else (
            await g("holding_register", 2), await g("holding_register", 1))
        level = await g("holding_register", 100)
        pump = await g("coil", 0)
        if safe or await g("coil", 2):
            if level < lo:
                pump = 1
            elif level > hi:
                pump = 0
        if await g("discrete_input", 3):
            pump = 0  # hard interlock
        if pump != await g("coil", 0):
            await self.put("coil", 0, pump)
        if safe and await g("coil", 1):
            await self.put("coil", 1, 0)
        raw = b"".join([(await g("holding_register", a)).to_bytes(2, "big") for a in range(5)])
        await self.put("input_register", 10, crc16(raw))
        await self.put("input_register", 11, int(time.monotonic() - self.t0) & 0xFFFF)

    async def run(self, tick):
        while True:
            await self.step()
            await asyncio.sleep(tick)


def write_inputs(values: dict, path=INPUTS_PATH):
    """Atomically replace the inputs file (raw values by register name)."""
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(values, indent=1), encoding="utf-8")
    tmp.replace(path)


async def serve(host="127.0.0.1", port=5020, tick=0.1, inputs_path=INPUTS_PATH, inputs=None):
    regs = load_registers()
    write_inputs(dict(BOOT_INPUTS if inputs is None else inputs), inputs_path)  # no stale state
    st = Station(regs, inputs_path)
    ctx = make_context(regs)
    ctx.simdevices[0].action = st.action
    server = ModbusTcpServer(ctx, address=(host, port))
    st.rt = server.context.devices[regs.unit_id]
    task = asyncio.create_task(st.run(tick))
    try:
        await server.serve_forever()
    finally:
        task.cancel()


if __name__ == "__main__":
    regs = load_registers()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=regs.ports["sim_station"])
    ap.add_argument("--demand", type=float, default=BOOT_INPUTS["DEMAND_PCT"] / 10,
                    help="town demand at boot, %% (change live with: poke set DEMAND_PCT <raw>)")
    a = ap.parse_args()
    print(f"software station on {a.host}:{a.port}, demand {a.demand:g} %, inputs file {INPUTS_PATH}")
    asyncio.run(serve(a.host, a.port, inputs={"DEMAND_PCT": round(a.demand * 10)}))
