"""The gateway's own writes to the station. Three fixed-purpose calls, nothing general:

    restore_logic(values)   holding 0-4 back to the last known good copy
    restore_defaults()      demo reset: coils and holding 0-4 to the register-map defaults
    safe_mode()             SAFE_MODE = 1 and ALARM = 1
    alarm()                 ALARM = 1 only (buzzer + red LED; control is not touched)
    forward(raw)            one held frame, byte for byte, after the release checks

None of these is an agent tool (restore_defaults is admin-only, behind POST /reset). The agent's tools (agent/tools_act.py) call them with
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

    def restore_defaults(self) -> dict[int, int]:
        """Coils and holding 0-4 to their defaults. SAFE_MODE goes first: while it is on the
        station ignores the rest. Returns the holding 0-4 values written."""
        by_key = self.regs._by_key
        coils = sorted((r.addr, r) for (k, _), r in by_key.items() if k == "coil" and r.default is not None)
        logic = {a: self.regs.lookup("holding_register", a).default for a in LOGIC_ADDRS}
        c = self._client()
        try:
            safe = self.regs.by_name("SAFE_MODE")
            ops = [lambda: c.write_coil(safe.addr, bool(safe.default), device_id=self.regs.unit_id),
                   lambda: c.write_registers(LOGIC_ADDRS[0], [int(logic[a]) for a in LOGIC_ADDRS],
                                             device_id=self.regs.unit_id)]
            ops += [lambda r=r: c.write_coil(r.addr, bool(r.default), device_id=self.regs.unit_id)
                    for _, r in coils]
            for op in ops:
                if op().isError():
                    raise RuntimeError("station refused a default")
        finally:
            c.close()
        return logic

    def safe_mode(self) -> None:
        c = self._client()
        try:
            for name in ("SAFE_MODE", "ALARM"):
                r = c.write_coil(self.regs.by_name(name).addr, True, device_id=self.regs.unit_id)
                if r.isError():
                    raise RuntimeError(f"station refused {name}: {r}")
        finally:
            c.close()

    def alarm(self) -> None:
        c = self._client()
        try:
            r = c.write_coil(self.regs.by_name("ALARM").addr, True, device_id=self.regs.unit_id)
            if r.isError():
                raise RuntimeError(f"station refused ALARM: {r}")
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
