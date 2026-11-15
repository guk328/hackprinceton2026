"""Modbus TCP proxy between clients and the station. Reads are forwarded
unchanged. A write is forwarded only if policy.decide() says ALLOW for every
coil/register in the frame; otherwise nothing is sent to the station.

    client --> :GATEWAY_PORT [proxy] --> STATION_HOST:STATION_PORT

    ALLOW   forward, relay the reply
    HOLD    exception 0x06 to the client, frame kept in the HeldStore
    DENY    exception 0x01 (also any function code that is not 1-6, 15, 16)

One write is judged at a time, against station state read just before, and the
next one waits for the station's answer to it. One upstream connection per
client. Unparseable bytes close that client only. If the station is down the
client gets Modbus exception 0x0B and the next client retries. A source on the
blocklist is disconnected before anything it sends is parsed.

Run: python -m gateway.proxy   (or: python tasks.py gateway)
"""
import asyncio
import logging
import os
import struct
from dataclasses import replace

from dotenv import load_dotenv

from gateway import events, policy
from gateway.blocklist import Blocklist
from gateway.frames import (WRITE_FCS, Frame, FrameBuffer, FrameError, WriteRequest,
                            decode_write, exception_reply)
from gateway.events import EventStore
from gateway.held import HeldStore
from gateway.policy import ALLOW, DENY, HOLD, Decision
from gateway.registers import load_registers
from gateway.station import StationPoller, StationSnapshot

log = logging.getLogger("gateway.proxy")

EXC_DENY = 0x01            # "illegal function"
EXC_HOLD = 0x06            # "server device busy": held for review
EXC_TARGET_FAILED = 0x0B   # "gateway target device failed to respond"
READ_FCS = (1, 2, 3, 4)
STRICTNESS = (ALLOW, HOLD, DENY)
C2S, S2C, G2C = "client->station", "station->client", "gateway->client"
TAMPER_SOURCE = "behind-gateway"   # src_ip of a LOGIC_TAMPER event: it did not come through us


def with_write(snap: StationSnapshot, req: WriteRequest) -> StationSnapshot:
    """The snapshot as it would be after `req`, for judging the rest of the frame."""
    if req.name is None:
        return snap
    if req.kind == "coil":
        return replace(snap, coils={**snap.coils, req.name: req.value})
    return replace(snap, holding={**snap.holding, req.name: req.value})


def summarize(frame: Frame, req: Frame | None = None, writes=None):
    """(address, count, values) for the log line. `req` is the matching request
    when `frame` is a reply; `writes` are the decoded WriteRequests of a write."""
    d = frame.data
    try:
        if writes:
            return writes[0].address, len(writes), [w.value for w in writes]
        addr = struct.unpack_from(">H", req.data)[0] if req else None
        if frame.is_exception:
            return addr, None, f"exception {d[0]:#04x}"
        if req is None:
            if frame.fc in READ_FCS:
                return (*struct.unpack(">HH", d), None)
            return None, None, d.hex()
        if frame.fc in (1, 2):
            qty = struct.unpack_from(">H", req.data, 2)[0]
            return addr, qty, [(d[1 + i // 8] >> (i % 8)) & 1 for i in range(qty)]
        if frame.fc in (3, 4):
            n = d[0] // 2
            return addr, n, list(struct.unpack_from(f">{n}H", d, 1))
        if frame.fc in (5, 6):
            value = struct.unpack_from(">H", d, 2)[0]
            return addr, 1, [int(bool(value)) if frame.fc == 5 else value]
        if frame.fc in (15, 16):
            return addr, struct.unpack_from(">H", d, 2)[0], None
    except (struct.error, IndexError):
        pass
    return None, None, d.hex()


def log_frame(src: str, direction: str, frame: Frame, req: Frame | None = None, writes=None):
    """One line per frame; the timestamp comes from the logging formatter."""
    addr, count, values = summarize(frame, req, writes)
    log.info("%s %s tid=%d fc=%d addr=%s count=%s values=%s",
             src, direction, frame.tid, frame.fc, addr, count, values)
    return addr, count, values


class Proxy:
    def __init__(self, station_host="127.0.0.1", station_port=5020,
                 host="0.0.0.0", port=1502, regs=None, connect_timeout=3.0,
                 poller: StationPoller | None = None, held: HeldStore | None = None,
                 store: EventStore | None = None, blocklist: Blocklist | None = None):
        self.station = (station_host, station_port)
        self.host, self.port = host, port
        self.regs = regs or load_registers()
        self.connect_timeout = connect_timeout
        self.poller = poller             # None: start() makes one, read on demand
        self._own_poller = poller is None
        self.held = held if held is not None else HeldStore()
        self.store = store               # None: nothing is persisted (log lines only)
        self.blocklist = blocklist or Blocklist()
        self._write_lock = asyncio.Lock()   # one write decided and settled at a time
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()

    def run_locked(self, fn, timeout: float = 20.0):
        """Run fn() in a worker thread while holding the write lock, and return its result.
        For the release path: no client write can be decided or forwarded between its safety
        re-check and its forward. Call it from a worker thread, never from the event loop.
        Raises if the proxy is not running, the lock is not free within `timeout`, or fn raises."""
        if self._loop is None:
            raise RuntimeError("proxy is not running")
        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            raise RuntimeError("run_locked must be called from a worker thread")

        async def locked():
            async with self._write_lock:
                return await asyncio.to_thread(fn)
        fut = asyncio.run_coroutine_threadsafe(locked(), self._loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise

    async def start(self):
        self._loop = asyncio.get_running_loop()
        if self.poller is None:
            self.poller = StationPoller(*self.station, regs=self.regs)
        if self.poller.on_tamper is None:
            self.poller.on_tamper = self.record_tamper
        self._server = await asyncio.start_server(self._on_client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]  # real port if 0 was asked
        log.info("proxy on %s:%d -> station %s:%d", self.host, self.port, *self.station)

    async def serve_forever(self):
        await self.start()
        await self._server.serve_forever()

    async def close(self):
        if self._server:
            self._server.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._own_poller and self.poller:
            await self.poller.close()

    def _record(self, src, direction, frame: Frame, req: Frame | None = None, writes=None):
        """Log one frame and store it. Returns its traffic row id (None without a store).
        Storage trouble is logged and never changes what is allowed or held."""
        addr, count, values = log_frame(src, direction, frame, req, writes)
        if self.store is None:
            return None
        try:
            return self.store.save_traffic(src, direction, frame.tid, frame.fc, addr, count,
                                           values, frame.raw)
        except Exception:
            log.exception("could not store a traffic row")
            return None

    def _station_view(self, snap: StationSnapshot) -> dict:
        """The README 4.4 `station` object, in engineering units."""
        def scaled(name):
            reg, raw = self.regs.by_name(name), snap.holding.get(name)
            return None if reg is None or raw is None else raw / reg.scale
        di = snap.discrete_inputs
        return {"presence": bool(di.get("PRESENCE")), "cabinet_open": bool(di.get("CABINET_OPEN")),
                "level_pct": scaled("TANK_LEVEL_PCT"), "chlorine_mgl": scaled("CHLORINE_MGL")}

    def _save_event(self, src, fc, req: WriteRequest | None, d: Decision, verdict: str,
                    before: StationSnapshot, frame_row):
        """One event for one write (or for a frame that carries none, like an unknown
        function code). `verdict` is what happened to the whole frame."""
        if self.store is None:
            return None
        reasons = list(d.reasons)
        if d.decision != verdict:
            reasons.append(f"frame {verdict.lower()} because another write in it was {verdict}")
        old = None
        if req and req.name:
            old = (before.coils if req.kind == "coil" else before.holding).get(req.name)
        try:
            return self.store.save_event({
                "src_ip": src, "function": fc,
                "kind": req.kind if req else None, "address": req.address if req else None,
                "name": req.name if req else None,
                "old_value": None if old is None else int(old),
                "new_value": req.value if req else None,
                "decision": verdict, "reasons": reasons,
                "prediction": events.prediction_json(d.prediction),
                "station": self._station_view(before)}, traffic_id=frame_row)["id"]
        except Exception:
            log.exception("could not store an event")
            return None

    def record_tamper(self, info: dict):
        """Settings changed behind the gateway (StationPoller found it). Stored as an event in
        the README 4.4 shape: decision DENY (the gateway does not accept the change), first
        reason starting "LOGIC_TAMPER", old_value = last known good, new_value = what is there."""
        if self.store is None:
            return None
        ch = info["changed"]
        first = ch[0] if ch else {"address": None, "name": "LOGIC_HASH",
                                  "last_known_good": info["expected_hash"],
                                  "current": info["station_hash"]}
        reasons = ["LOGIC_TAMPER: " + (", ".join(
            f"{c['name']} {c['last_known_good']} -> {c['current']}" for c in ch) or "hash differs")
            + " without a command through the gateway",
            f"station LOGIC_HASH {info['station_hash']}, last known good {info['expected_hash']}"]
        return self.store.save_event({
            "src_ip": TAMPER_SOURCE, "function": None, "kind": "register",
            "address": first["address"], "name": first["name"],
            "old_value": first["last_known_good"], "new_value": first["current"],
            "decision": DENY, "reasons": reasons, "prediction": None,
            "station": self._station_view(self.poller.snapshot)})

    async def _decide_frame(self, writes, src=None, frame_row=None):
        """Strictest verdict over every write in the frame. Anything that goes
        wrong while deciding is a HOLD. Returns (verdict, decisions, event ids)."""
        station = await self.poller.refresh()
        verdict, decisions, befores = ALLOW, [], []
        for req in writes:
            befores.append(station)
            try:
                d = policy.decide(req, station, regs=self.regs)
                rank = STRICTNESS.index(d.decision)
            except Exception as e:
                log.exception("decide() failed, holding")
                d = Decision(HOLD, [f"decide failed: {e!r}"])
                rank = STRICTNESS.index(HOLD)
            decisions.append(d)
            if rank > STRICTNESS.index(verdict):
                verdict = d.decision
            station = with_write(station, req)
        ids = [self._save_event(src or req.src_ip, req.fc, req, d, verdict, before, frame_row)
               for req, d, before in zip(writes, decisions, befores)]
        return verdict, decisions, ids

    def _refuse(self, src, c_writer, frame: Frame, writes, verdict, decisions, event_ids=()):
        """Answer a frame that is not forwarded. Only HOLD is kept for release."""
        held = (self.held.add(src, frame, writes, decisions, event_ids)
                if verdict == HOLD else None)
        log.warning("%s %s tid=%d fc=%d id=%s reasons=%s", src, verdict, frame.tid, frame.fc,
                    held.id if held else None, [r for d in decisions for r in d.reasons])
        self._reply_exception(src, c_writer, frame, EXC_HOLD if verdict == HOLD else EXC_DENY)

    async def _settled(self, pending, frame: Frame, relay):
        """Wait until the station has answered a forwarded write, so the next
        decision reads state that includes it."""
        end = asyncio.get_running_loop().time() + self.connect_timeout
        while pending.get(frame.tid) is frame and not relay.done():
            if asyncio.get_running_loop().time() > end:
                log.warning("no reply from the station to tid=%d", frame.tid)
                return
            await asyncio.sleep(0.005)

    async def _on_client(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            await self._handle(reader, writer)
        except Exception:
            log.exception("client handler crashed")   # never take the server down
        finally:
            self._tasks.discard(task)

    async def _handle(self, c_reader, c_writer):
        src = (c_writer.get_extra_info("peername") or ("?",))[0]
        if self.blocklist.is_blocked(src):
            log.warning("%s is blocked, connection dropped", src)
            c_writer.close()
            return
        pending: dict[int, Frame] = {}   # tid -> request awaiting its reply
        s_writer = relay = None
        try:
            try:
                s_reader, s_writer = await asyncio.wait_for(
                    asyncio.open_connection(*self.station), self.connect_timeout)
                relay = asyncio.create_task(self._relay(src, s_reader, c_writer, pending))
            except (OSError, asyncio.TimeoutError) as e:
                log.warning("%s station %s:%d unreachable: %r", src, *self.station, e)

            buf = FrameBuffer()
            while data := await c_reader.read(4096):
                if self.blocklist.is_blocked(src):     # blocked while connected
                    log.warning("%s is blocked, connection dropped", src)
                    return
                for frame in buf.feed(data):
                    writes = decode_write(frame, src, self.regs) if frame.fc in WRITE_FCS else None
                    frame_row = self._record(src, C2S, frame, writes=writes)
                    if relay is None or relay.done():      # station down
                        self._reply_exception(src, c_writer, frame, EXC_TARGET_FAILED)
                        await c_writer.drain()
                        return
                    if frame.fc in READ_FCS:
                        pending[frame.tid] = frame
                        s_writer.write(frame.raw)
                        await s_writer.drain()
                        continue
                    if writes:
                        async with self._write_lock:
                            verdict, decisions, ids = await self._decide_frame(writes, src, frame_row)
                            if verdict == ALLOW:   # the only line that sends a write
                                pending[frame.tid] = frame
                                s_writer.write(frame.raw)
                                await s_writer.drain()
                                await self._settled(pending, frame, relay)
                                settings = {w.address: w.value for w in writes if w.kind == "register"}
                                if settings:   # the new baseline is what the station now holds
                                    self.poller.accept_logic(settings, await self.poller.refresh())
                                continue
                    else:
                        verdict = DENY
                        decisions = [Decision(DENY, [f"function code {frame.fc} is not allowed"])]
                        ids = [self._save_event(src, frame.fc, None, decisions[0], DENY,
                                                self.poller.snapshot, frame_row)]
                    self._refuse(src, c_writer, frame, writes or [], verdict, decisions, ids)
                    await c_writer.drain()
        except FrameError as e:
            log.warning("%s unparseable frame, closing client: %s", src, e)
        except OSError as e:
            log.warning("%s connection error: %r", src, e)
        finally:
            if relay:
                relay.cancel()
            for w in (s_writer, c_writer):
                if w:
                    w.close()

    async def _relay(self, src, s_reader, c_writer, pending):
        """Station -> client. When the station drops, answer what is still
        pending with 0x0B; the client loop fails the next request and closes."""
        buf = FrameBuffer()
        try:
            while data := await s_reader.read(4096):
                for frame in buf.feed(data):
                    self._record(src, S2C, frame, req=pending.pop(frame.tid, None))
                    c_writer.write(frame.raw)
                await c_writer.drain()
            log.warning("%s station closed the connection", src)
        except (FrameError, OSError) as e:
            log.warning("%s station connection lost: %r", src, e)
        for req in list(pending.values()):
            self._reply_exception(src, c_writer, req, EXC_TARGET_FAILED)
        pending.clear()

    def _reply_exception(self, src, c_writer, req: Frame, code: int):
        reply = exception_reply(req, code)
        log.info("%s %s tid=%d fc=%d addr=None count=None values=exception %#04x",
                 src, G2C, req.tid, req.fc | 0x80, code)
        if self.store is not None:
            try:
                self.store.save_traffic(src, G2C, req.tid, req.fc | 0x80,
                                        values=f"exception {code:#04x}", raw=reply)
            except Exception:
                log.exception("could not store a traffic row")
        c_writer.write(reply)


def config() -> dict:
    """Settings from .env, defaults as in .env.example."""
    load_dotenv()
    return dict(station_host=os.getenv("STATION_HOST", "127.0.0.1"),
                station_port=int(os.getenv("STATION_PORT", "5020")),
                port=int(os.getenv("GATEWAY_PORT", "1502")))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = config()   # loads .env first, so DATABASE_URL is set below
        asyncio.run(Proxy(**cfg, store=events.open_store()).serve_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
