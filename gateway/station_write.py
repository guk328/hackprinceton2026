"""The gateway's own writes to the station. Three fixed-purpose calls, nothing general:

    restore_logic(values)   holding 0-4 back to the last known good copy
    safe_mode()             SAFE_MODE = 1 and ALARM = 1
    forward(raw)            one held frame, byte for byte, after the release checks

None of these is an agent tool. The agent's tools (agent/tools_act.py) call them with
values the gateway already holds; the agent never supplies an address or a value.
Synchronous (called from the agent's worker thread), one short connection per call.
"""
import socket
import struct

from pymodbus.client import ModbusTcpClient

from gateway.registers import RegisterMap, load_registers
from gateway.station import LOGIC_ADDRS


class StationWriter:
    def __init__(self, host: str, port: int, regs: RegisterMap | None = None, timeout: float = 2.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.regs = regs or load_registers()

    def _client(self) -> ModbusTcpClient:
        c = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout)
        if not c.connect():
            raise ConnectionError(f"cannot reach station {self.host}:{self.port}")
        return c

    def restore_logic(self, values: dict[int, int]) -> list[int]:
        missing = [a for a in LOGIC_ADDRS if a not in values]
        if missing:
            raise ValueError(f"no last known good value for holding {missing}")
        data = [int(values[a]) for a in LOGIC_ADDRS]
        c = self._client()
        try:
            r = c.write_registers(LOGIC_ADDRS[0], data, device_id=self.regs.unit_id)
            if r.isError():
                raise RuntimeError(f"station refused the restore: {r}")
        finally:
            c.close()
        return data

    def safe_mode(self) -> None:
        c = self._client()
        try:
            for name in ("SAFE_MODE", "ALARM"):
                r = c.write_coil(self.regs.by_name(name).addr, True, device_id=self.regs.unit_id)
                if r.isError():
                    raise RuntimeError(f"station refused {name}: {r}")
        finally:
            c.close()

    def forward(self, raw: bytes) -> bytes:
        """Send one Modbus TCP frame as received and return the station's reply frame."""
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.sendall(raw)
            head = b""
            while len(head) < 6:
                chunk = s.recv(6 - len(head))
                if not chunk:
                    raise ConnectionError("station closed the connection")
                head += chunk
            need, body = struct.unpack(">H", head[4:])[0], b""
            while len(body) < need:
                chunk = s.recv(need - len(body))
                if not chunk:
                    raise ConnectionError("station closed the connection")
                body += chunk
            return head + body
