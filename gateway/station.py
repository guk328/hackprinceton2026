"""Station poller: a StationSnapshot every `interval` s, reconnecting if the station drops."""
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

    async def start(self):
        self._task = asyncio.create_task(self._loop())

    async def close(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._client.close()

    def accept_logic(self, changes: dict[int, int]):
        """Record allowed writes to holding 0-4 as the new last-known-good."""
        self.last_good_logic.update({a: v for a, v in changes.items() if a in LOGIC_ADDRS})

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
