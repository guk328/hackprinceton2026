"""Station poller: a StationSnapshot every `interval` s, reconnecting if the station drops.

It also watches for logic tampering: holding 0-4 (and the station's LOGIC_HASH of them)
must equal the last known good copy, which moves only when the gateway itself lets a write
through and then reads it back from the station (accept_logic). A difference that lasts
TAMPER_CONFIRM_S means the settings were changed behind the gateway, and on_tamper(info) is
called once for that change. Both the values and the hash are checked, so a station that
reports a stale hash is caught as well.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field, replace

from pymodbus.client import AsyncModbusTcpClient

from gateway.registers import RegisterMap, load_registers

log = logging.getLogger(__name__)

# kind -> (AsyncModbusTcpClient method, attribute on the reply holding the values)
READERS = {
    "coil": ("read_coils", "bits"),
    "discrete_input": ("read_discrete_inputs", "bits"),
    "input_register": ("read_input_registers", "registers"),
    "holding_register": ("read_holding_registers", "registers"),
}
LOGIC_ADDRS = range(5)  # holding 0-4 feed LOGIC_HASH
TAMPER_CONFIRM_S = 1.5  # a difference must last this long (an allowed write is settling for less)


def crc16_modbus(data: bytes) -> int:
    """CRC16/MODBUS (poly 0xA001 reflected, init 0xFFFF), as the station computes LOGIC_HASH."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def logic_hash(values: dict[int, int]) -> int | None:
    """LOGIC_HASH of holding 0-4 (each register big-endian); None if any is missing."""
    if any(a not in values for a in LOGIC_ADDRS):
        return None
    return crc16_modbus(b"".join(int(values[a]).to_bytes(2, "big") for a in LOGIC_ADDRS))


@dataclass(frozen=True)
class StationSnapshot:
    """Raw (unscaled) values by register name. Stale values are kept when connected is False."""
    coils: dict = field(default_factory=dict)
    discrete_inputs: dict = field(default_factory=dict)
    input_registers: dict = field(default_factory=dict)
    holding: dict = field(default_factory=dict)
    connected: bool = False
    ts: float = 0.0  # time of the last good poll


def _runs(addrs):
    """Contiguous (start, count) runs from a set of addresses."""
    out = []
    for a in sorted(addrs):
        if out and a == out[-1][0] + out[-1][1]:
            out[-1][1] += 1
        else:
            out.append([a, 1])
    return out


class StationPoller:
    def __init__(self, host, port, regs: RegisterMap | None = None,
                 interval: float = 1.0, timeout: float = 1.0):
        self.regs = regs or load_registers()
        self.host, self.port, self.interval, self.timeout = host, port, interval, timeout
        self.snapshot = StationSnapshot()
        self.last_good_logic: dict[int, int] = {}  # holding 0-4, moves only via accept_logic
        self._client = AsyncModbusTcpClient(host, port=port, timeout=timeout, retries=0)
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.on_tamper = None                 # callable(info dict), set by the proxy
        self.tamper_confirm_s = TAMPER_CONFIRM_S
        self._tamper_since: float | None = None
        self._tamper_reported = None          # the tampered values already reported

    async def start(self):
        self._task = asyncio.create_task(self._loop())

    async def close(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._client.close()

    def accept_logic(self, changes: dict[int, int], snap: StationSnapshot | None = None):
        """Record allowed writes to holding 0-4 as the new last-known-good, but only those
        that `snap`, read from the station after the write, shows really landed. A write the
        station refused or ignored (SAFE_MODE) leaves the copy alone. Returns the addresses
        that were not confirmed. Without `snap` nothing is recorded."""
        names = {r.addr: r.name for (k, _), r in self.regs._by_key.items()
                 if k == "holding_register" and r.addr in LOGIC_ADDRS}
        landed = {a: v for a, v in changes.items() if a in names and snap is not None
                  and snap.connected and snap.holding.get(names[a]) == v}
        self.last_good_logic.update(landed)
        missed = sorted(a for a in changes if a in names and a not in landed)
        if missed:
            log.warning("allowed write to holding %s not seen on the station; last known good unchanged", missed)
        return missed

    async def refresh(self) -> StationSnapshot:
        """Poll now (one poll at a time). On failure the old values stay, connected=False."""
        async with self._lock:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # station down, timeout, bad reply
                if self.snapshot.connected:
                    log.warning("station poll failed: %r", e)
                self.snapshot = replace(self.snapshot, connected=False)
                self._client.close()
            return self.snapshot

    async def _loop(self):
        while True:
            await self.refresh()
            await asyncio.sleep(self.interval)

    async def poll_once(self):
        if not self._client.connected:
            await self._client.connect()
            if not self._client.connected:
                raise ConnectionError("cannot connect")
        by_kind: dict[str, dict[str, int]] = {}
        for kind, (method, attr) in READERS.items():
            regs = {r.addr: r.name for (k, _), r in self.regs._by_key.items() if k == kind}
            values = by_kind[kind] = {}
            for start, count in _runs(regs):
                r = await getattr(self._client, method)(
                    start, count=count, device_id=self.regs.unit_id)
                if r.isError():
                    raise IOError(f"{method}({start},{count}): {r}")
                for i, v in enumerate(getattr(r, attr)[:count]):
                    values[regs[start + i]] = int(v)
        snap = StationSnapshot(
            coils=by_kind["coil"], discrete_inputs=by_kind["discrete_input"],
            input_registers=by_kind["input_register"], holding=by_kind["holding_register"],
            connected=True, ts=time.time())
        if not self.last_good_logic:
            self.last_good_logic = {
                r.addr: snap.holding[r.name]
                for (k, _), r in self.regs._by_key.items()
                if k == "holding_register" and r.addr in LOGIC_ADDRS}
        self.snapshot = snap
        self._check_logic(snap)

    def _check_logic(self, snap: StationSnapshot):
        names = {r.addr: r.name for (k, _), r in self.regs._by_key.items()
                 if k == "holding_register" and r.addr in LOGIC_ADDRS}
        now = {a: snap.holding.get(n) for a, n in names.items()}
        good, station_hash = self.last_good_logic, snap.input_registers.get("LOGIC_HASH")
        changed = [a for a in LOGIC_ADDRS if a in good and now.get(a) != good[a]]
        hash_off = station_hash is not None and station_hash != logic_hash(good)
        if not changed and not hash_off:
            self._tamper_since = self._tamper_reported = None
            return
        key = (tuple(sorted(now.items())), station_hash)
        if self._tamper_since is None:
            self._tamper_since = snap.ts
        if snap.ts - self._tamper_since < self.tamper_confirm_s or key == self._tamper_reported:
            return
        self._tamper_reported = key
        info = {"changed": [{"address": a, "name": names[a], "last_known_good": good[a],
                             "current": now.get(a)} for a in changed],
                "station_hash": station_hash, "expected_hash": logic_hash(good)}
        log.warning("LOGIC_TAMPER: %s", info)
        if self.on_tamper:
            try:
                self.on_tamper(info)
            except Exception:
                log.exception("on_tamper failed")
