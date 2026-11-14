"""Modbus TCP framing in plain bytes: MBAP header + PDU.

    tid(2) proto(2) length(2) unit(1) | fc(1) data(n)      length = 1 + len(pdu)

FrameBuffer   bytes in, complete Frames out (partial reads, several frames per read)
decode_write  FC 5/6/15/16 request Frame -> list[WriteRequest]
exception_reply  Frame + exception code -> reply bytes

Anything malformed raises FrameError; the caller fails closed.
"""
import struct
from dataclasses import dataclass
from typing import Iterator

from gateway.registers import RegisterMap

MBAP_LEN = 7
MIN_LENGTH = 2        # unit id + function code
MAX_LENGTH = 254      # unit id + 253-byte PDU
MAX_COILS = 0x7B0     # FC 15 limit, 1968
MAX_REGISTERS = 0x7B  # FC 16 limit, 123
WRITE_FCS = (5, 6, 15, 16)
# WriteRequest.kind (README 4.4) -> gateway.registers kind
_REG_KIND = {"coil": "coil", "register": "holding_register"}


class FrameError(ValueError):
    """Bytes that are not a valid Modbus TCP frame or write request."""


@dataclass(frozen=True)
class Frame:
    tid: int
    unit: int
    fc: int
    data: bytes   # PDU after the function code
    raw: bytes    # whole ADU, exactly as received

    @property
    def pdu(self) -> bytes:
        return self.raw[MBAP_LEN:]

    @property
    def is_exception(self) -> bool:
        return bool(self.fc & 0x80)


@dataclass(frozen=True)
class WriteRequest:
    src_ip: str
    fc: int
    kind: str          # "coil" or "register"
    address: int
    value: int
    name: str | None   # from registers.yaml, None if the address is not in the map


class FrameBuffer:
    """Per-connection stream buffer. After a FrameError the stream cannot be
    re-synchronised, so every later feed() raises too: close the connection."""

    def __init__(self):
        self._buf = bytearray()
        self._error: FrameError | None = None

    @property
    def pending(self) -> int:
        """Bytes held that do not yet make a complete frame."""
        return len(self._buf)

    def feed(self, data: bytes) -> Iterator[Frame]:
        self._buf += data          # buffered now, even if the caller never iterates
        return self._drain()

    def _fail(self, msg: str):
        self._error = FrameError(msg)
        self._buf.clear()
        raise self._error

    def _drain(self) -> Iterator[Frame]:
        if self._error:
            raise self._error
        while True:
            buf = self._buf
            if len(buf) >= 4 and buf[2:4] != b"\x00\x00":
                self._fail(f"protocol id {bytes(buf[2:4]).hex()} is not 0000")
            if len(buf) < 6:
                return
            tid, _, length = struct.unpack_from(">HHH", buf)
            if not MIN_LENGTH <= length <= MAX_LENGTH:
                self._fail(f"length field {length} outside {MIN_LENGTH}..{MAX_LENGTH}")
            end = 6 + length
            if len(buf) < end:
                return
            raw = bytes(buf[:end])
            del buf[:end]
            yield Frame(tid=tid, unit=raw[6], fc=raw[7], data=raw[8:], raw=raw)


def decode_write(frame: Frame, src_ip: str, regs: RegisterMap) -> list[WriteRequest]:
    """One WriteRequest per coil/register written. Raises FrameError if the frame
    is not a well-formed FC 5/6/15/16 request."""
    fc, d = frame.fc, frame.data
    if fc not in WRITE_FCS:
        raise FrameError(f"function code {fc} is not a write")
    kind = "coil" if fc in (5, 15) else "register"

    if fc in (5, 6):
        if len(d) != 4:
            raise FrameError(f"fc {fc}: expected 4 data bytes, got {len(d)}")
        addr, value = struct.unpack(">HH", d)
        if fc == 5:
            if value not in (0xFF00, 0x0000):
                raise FrameError(f"fc 5: coil value {value:#06x} is not FF00 or 0000")
            value = 1 if value else 0
        values = [value]
    else:
        if len(d) < 5:
            raise FrameError(f"fc {fc}: header cut short ({len(d)} data bytes)")
        addr, qty, nbytes = struct.unpack_from(">HHB", d)
        body = d[5:]
        limit, want = ((MAX_COILS, (qty + 7) // 8) if fc == 15
                       else (MAX_REGISTERS, qty * 2))
        if not 1 <= qty <= limit:
            raise FrameError(f"fc {fc}: quantity {qty} outside 1..{limit}")
        if nbytes != want or len(body) != nbytes:
            raise FrameError(f"fc {fc}: byte count {nbytes}, {len(body)} bytes "
                             f"present, {want} needed for quantity {qty}")
        if addr + qty > 0x10000:
            raise FrameError(f"fc {fc}: {qty} from {addr} runs past address 65535")
        if fc == 15:
            values = [(body[i // 8] >> (i % 8)) & 1 for i in range(qty)]
        else:
            values = list(struct.unpack(f">{qty}H", body))

    out = []
    for i, value in enumerate(values):
        reg = regs.lookup(_REG_KIND[kind], addr + i)
        out.append(WriteRequest(src_ip, fc, kind, addr + i, value,
                                reg.name if reg else None))
    return out


def exception_reply(frame: Frame, code: int) -> bytes:
    """Modbus exception ADU answering `frame`: same tid and unit, fc | 0x80."""
    return struct.pack(">HHHBBB", frame.tid, 0, 3, frame.unit, frame.fc | 0x80, code)
