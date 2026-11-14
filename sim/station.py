"""Software station: Modbus TCP server on 127.0.0.1:5020 with the yaml defaults."""
import argparse
import asyncio

from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import StartAsyncTcpServer

from gateway.registers import load_registers

SIZE = 200  # covers addr 0-11 and the twin registers at 100-101


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


async def serve(host="127.0.0.1", port=5020):
    await StartAsyncTcpServer(make_context(), address=(host, port))


if __name__ == "__main__":
    regs = load_registers()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=regs.ports["sim_station"])
    a = ap.parse_args()
    print(f"software station on {a.host}:{a.port}")
    asyncio.run(serve(a.host, a.port))
