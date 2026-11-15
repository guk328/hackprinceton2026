"""Held write frames, kept so an approved one can be released later.

The store only keeps them. Releasing is a separate, operator-approved path that
must run decide() again first. In memory for now; oldest dropped past `limit`.
"""
import logging
import time
from dataclasses import dataclass

from gateway.frames import Frame, WriteRequest

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeldCommand:
    id: str
    ts: float
    src_ip: str
    frame: Frame                 # exactly as received; frame.raw is what a release would send
    writes: list[WriteRequest]
    decisions: list              # policy.Decision per write, same order
    event_ids: tuple = ()        # the events saved for these writes ("evt_0042", ...)


class HeldStore:
    def __init__(self, limit: int = 1000):
        self.limit = limit
        self._items: dict[str, HeldCommand] = {}
        self._n = 0

    def add(self, src_ip: str, frame: Frame, writes, decisions, event_ids=()) -> HeldCommand:
        self._n += 1
        cmd = HeldCommand(f"held_{self._n:04d}", time.time(), src_ip, frame,
                          list(writes), list(decisions), tuple(e for e in event_ids if e))
        self._items[cmd.id] = cmd
        while len(self._items) > self.limit:
            dropped = next(iter(self._items))
            del self._items[dropped]
            log.warning("held store full, dropped %s", dropped)
        return cmd

    def get(self, held_id: str) -> HeldCommand | None:
        return self._items.get(held_id)

    def by_event(self, event_id: str) -> HeldCommand | None:
        for cmd in self._items.values():
            if event_id in cmd.event_ids:
                return cmd
        return None

    def pop(self, held_id: str) -> HeldCommand | None:
        return self._items.pop(held_id, None)

    def all(self) -> list[HeldCommand]:
        return list(self._items.values())

    def clear(self) -> None:
        self._items.clear()
