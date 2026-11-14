"""Modbus TCP proxy between clients and the station. Transparent mode: every
complete frame is forwarded unchanged and logged; nothing is blocked yet.

    client --> :GATEWAY_PORT [proxy] --> STATION_HOST:STATION_PORT

One upstream connection per client. Writes pass through decide() (the hook that
step 8+ fills in). Unparseable bytes close that client only. If the station is
down the client gets Modbus exception 0x0B and the next client retries.

Run: python -m gateway.proxy   (or: python tasks.py gateway)
"""
import asyncio
import logging
import os
import struct

from dotenv import load_dotenv

from gateway.frames import (WRITE_FCS, Frame, FrameBuffer, FrameError, WriteRequest,
                            decode_write, exception_reply)
from gateway.registers import load_registers

log = logging.getLogger("gateway.proxy")

ALLOW = "ALLOW"
EXC_TARGET_FAILED = 0x0B   # "gateway target device failed to respond"
READ_FCS = (1, 2, 3, 4)
C2S, S2C, G2C = "client->station", "station->client", "gateway->client"


def decide(req: WriteRequest) -> str:
    """THE HOOK. Step 8+ replaces this with the real policy (ALLOW/HOLD/DENY)."""
    return ALLOW


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


class Proxy:
    def __init__(self, station_host="127.0.0.1", station_port=5020,
                 host="0.0.0.0", port=1502, regs=None, connect_timeout=3.0):
        self.station = (station_host, station_port)
        self.host, self.port = host, port
        self.regs = regs or load_registers()
        self.connect_timeout = connect_timeout
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self):
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
                for frame in buf.feed(data):
                    writes = None
                    if frame.fc in WRITE_FCS:
                        writes = decode_write(frame, src, self.regs)
                        # --- enforcement hook: step 10 acts on anything not ALLOW ---
                        decisions = [decide(req) for req in writes]
                        assert all(d == ALLOW for d in decisions)  # transparent mode
                    log_frame(src, C2S, frame, writes=writes)
                    if relay is None or relay.done():      # station down
                        self._reply_failed(src, c_writer, frame)
                        await c_writer.drain()
                        return
                    pending[frame.tid] = frame
                    s_writer.write(frame.raw)
                    await s_writer.drain()
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
                    log_frame(src, S2C, frame, req=pending.pop(frame.tid, None))
                    c_writer.write(frame.raw)
                await c_writer.drain()
            log.warning("%s station closed the connection", src)
        except (FrameError, OSError) as e:
            log.warning("%s station connection lost: %r", src, e)
        for req in list(pending.values()):
            self._reply_failed(src, c_writer, req)
        pending.clear()

    @staticmethod
    def _reply_failed(src, c_writer, req: Frame):
        raw = exception_reply(req, EXC_TARGET_FAILED)
        log.info("%s %s tid=%d fc=%d addr=None count=None values=exception 0x0b",
                 src, G2C, req.tid, req.fc | 0x80)
        c_writer.write(raw)


def config() -> dict:
    """Settings from .env, defaults as in .env.example."""
    load_dotenv()
    return dict(station_host=os.getenv("STATION_HOST", "127.0.0.1"),
                station_port=int(os.getenv("STATION_PORT", "5020")),
                port=int(os.getenv("GATEWAY_PORT", "1502")))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(Proxy(**config()).serve_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
