"""Held write frames, kept so an approved one can be released later.

The store only keeps them. Releasing is a separate, operator-approved path that must run
decide(release=True) again first (agent/tools_act.py). With an EventStore the frames are
also written to SQLite and loaded again at start-up, so a gateway restart does not lose
them (the verdicts are not stored: they are recomputed at release anyway). Oldest dropped
past `limit`.
"""
import logging
import time
from dataclasses import dataclass

from gateway.frames import Frame, FrameBuffer, WriteRequest, decode_write
from gateway.registers import load_registers

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeldCommand:
    id: str
    ts: float
    src_ip: str
    frame: Frame                 # exactly as received; frame.raw is what a release would send
    writes: list[WriteRequest]
    decisions: list              # policy.Decision per write, same order (empty after a restart)
    event_ids: tuple = ()        # the events saved for these writes ("evt_0042", ...)


class HeldStore:
    def __init__(self, limit: int = 1000, store=None, regs=None):
        self.limit, self.store = limit, store
        self._items: dict[str, HeldCommand] = {}
        self._n = 0
        if store is not None:
            self._load(regs)

    def _load(self, regs):
        regs = regs or load_registers()
        for row in self.store.held_rows():
            try:
                (frame,) = FrameBuffer().feed(row["raw"])
                writes = decode_write(frame, row["src_ip"], regs)
                self._items[row["id"]] = HeldCommand(row["id"], row["ts"], row["src_ip"], frame,
                                                     list(writes), [], tuple(row["event_ids"]))
                self._n = max(self._n, int(row["id"].rsplit("_", 1)[1]))
            except Exception:      # an unreadable row is dropped, never released
                log.exception("could not restore held frame %s, discarding it", row["id"])
                self.store.held_delete(row["id"])

    def _forget(self, held_id: str):
        if self.store is not None:
            try:
                self.store.held_delete(held_id)
            except Exception:
                log.exception("could not delete held frame %s from the database", held_id)

    def add(self, src_ip: str, frame: Frame, writes, decisions, event_ids=()) -> HeldCommand:
        self._n += 1
        cmd = HeldCommand(f"held_{self._n:04d}", time.time(), src_ip, frame,
                          list(writes), list(decisions), tuple(e for e in event_ids if e))
        self._items[cmd.id] = cmd
        if self.store is not None:
            try:
                self.store.held_save(cmd.id, cmd.ts, src_ip, frame.raw, cmd.event_ids)
            except Exception:
                log.exception("could not persist held frame %s", cmd.id)
        while len(self._items) > self.limit:
            dropped = next(iter(self._items))
            del self._items[dropped]
            self._forget(dropped)
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
        cmd = self._items.pop(held_id, None)
        if cmd is not None:
            self._forget(held_id)
        return cmd

    def all(self) -> list[HeldCommand]:
        return list(self._items.values())

    def clear(self) -> None:
        self._items.clear()
        self._n = 0
        if self.store is not None:
            self.store.held_delete()
