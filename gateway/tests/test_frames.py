"""Frame parser tests, all from hand-built byte strings.

Layout: tid(2) proto(2) length(2) unit(1) | fc(1) data(n).  length = 1 + len(pdu).
"""
import pytest

from gateway.frames import (FrameBuffer, FrameError, WriteRequest, decode_write,
                            exception_reply)
from gateway.registers import load_registers

REGS = load_registers()
IP = "203.0.113.7"


def h(s: str) -> bytes:
    return bytes.fromhex(s)


def one(raw: bytes):
    frames = list(FrameBuffer().feed(raw))
    assert len(frames) == 1
    return frames[0]


# (fc, request, response, request data, response data)
CASES = [
    (1, "0001 0000 0006 01 01 0000 0005", "0001 0000 0004 01 01 01 16",
     "0000 0005", "01 16"),
    (2, "0002 0000 0006 01 02 0000 0004", "0002 0000 0004 01 02 01 0A",
     "0000 0004", "01 0A"),
    (3, "0003 0000 0006 01 03 0000 0005",
     "0003 0000 000D 01 03 0A 000A 0352 0190 0064 0001",
     "0000 0005", "0A 000A 0352 0190 0064 0001"),
    (4, "0004 0000 0006 01 04 0000 0003", "0004 0000 0009 01 04 06 01F4 00E6 0190",
     "0000 0003", "06 01F4 00E6 0190"),
    (5, "0005 0000 0006 01 05 0003 FF00", "0005 0000 0006 01 05 0003 FF00",
     "0003 FF00", "0003 FF00"),
    (6, "0006 0000 0006 01 06 0000 0064", "0006 0000 0006 01 06 0000 0064",
     "0000 0064", "0000 0064"),
    (15, "0007 0000 0008 01 0F 0000 0005 01 15", "0007 0000 0006 01 0F 0000 0005",
     "0000 0005 01 15", "0000 0005"),
    (16, "0008 0000 000B 01 10 0001 0002 04 0384 012C",
     "0008 0000 0006 01 10 0001 0002",
     "0001 0002 04 0384 012C", "0001 0002"),
]
REQ = {c[0]: h(c[1]) for c in CASES}


# ---- framing ----------------------------------------------------------------

@pytest.mark.parametrize("fc,req,resp,req_data,resp_data", CASES)
def test_request_and_response_round_trip(fc, req, resp, req_data, resp_data):
    for raw, data in ((h(req), h(req_data)), (h(resp), h(resp_data))):
        f = one(raw)
        assert (f.fc, f.unit, f.data) == (fc, 1, data)
        assert f.tid == int.from_bytes(raw[:2], "big")
        assert f.raw == raw
        assert f.pdu == raw[7:]
        assert not f.is_exception


def test_exception_reply_frame_is_parsed():
    f = one(h("0006 0000 0003 01 86 02"))
    assert f.is_exception
    assert (f.tid, f.fc, f.data) == (6, 0x86, b"\x02")


def test_unit_id_and_tid_are_kept():
    f = one(h("BEEF 0000 0006 11 03 0000 0001"))
    assert (f.tid, f.unit) == (0xBEEF, 0x11)


@pytest.mark.parametrize("fc", [1, 2, 3, 4, 5, 6, 15, 16])
def test_partial_frame_one_byte_at_a_time(fc):
    raw, buf, out = REQ[fc], FrameBuffer(), []
    for i, b in enumerate(raw):
        got = list(buf.feed(bytes([b])))
        if i < len(raw) - 1:
            assert got == []
        out += got
    assert [f.raw for f in out] == [raw]
    assert buf.pending == 0


def test_split_inside_header_and_inside_pdu():
    raw = REQ[16]
    for cut in (1, 3, 6, 7, 8, len(raw) - 1):
        buf = FrameBuffer()
        assert list(buf.feed(raw[:cut])) == []
        assert buf.pending == cut
        assert [f.raw for f in buf.feed(raw[cut:])] == [raw]


def test_several_frames_in_one_read():
    stream = REQ[3] + REQ[6] + REQ[15] + REQ[16]
    frames = list(FrameBuffer().feed(stream))
    assert [f.fc for f in frames] == [3, 6, 15, 16]
    assert b"".join(f.raw for f in frames) == stream


def test_frames_plus_partial_tail_then_rest():
    buf = FrameBuffer()
    first = list(buf.feed(REQ[5] + REQ[6] + REQ[16][:9]))
    assert [f.fc for f in first] == [5, 6]
    assert buf.pending == 9
    assert [f.fc for f in buf.feed(REQ[16][9:] + REQ[1])] == [16, 1]
    assert buf.pending == 0


def test_feed_buffers_even_if_result_is_not_iterated():
    buf = FrameBuffer()
    buf.feed(REQ[6][:4])
    assert [f.fc for f in buf.feed(REQ[6][4:])] == [6]


def test_empty_feed():
    assert list(FrameBuffer().feed(b"")) == []


def test_truncated_frame_never_yields():
    buf = FrameBuffer()
    assert list(buf.feed(REQ[6][:-1])) == []     # last byte never arrives
    assert buf.pending == len(REQ[6]) - 1
    assert list(buf.feed(b"")) == []


# ---- bad framing ------------------------------------------------------------

def test_wrong_protocol_id():
    with pytest.raises(FrameError):
        list(FrameBuffer().feed(h("0001 0001 0006 01 03 0000 0001")))


def test_wrong_protocol_id_detected_before_frame_is_complete():
    with pytest.raises(FrameError):
        list(FrameBuffer().feed(h("0001 1234")))


@pytest.mark.parametrize("length", ["0000", "0001"])
def test_length_too_small(length):
    with pytest.raises(FrameError):
        list(FrameBuffer().feed(h(f"0001 0000 {length} 01 03 0000 0001")))


@pytest.mark.parametrize("length", ["00FF", "0100", "FFFF"])
def test_length_too_big(length):
    with pytest.raises(FrameError):
        list(FrameBuffer().feed(h(f"0001 0000 {length} 01 03")))


def test_max_length_is_accepted():
    raw = h("0001 0000 00FE 01 03") + bytes(252)     # 253-byte PDU, the ADU maximum
    assert one(raw).raw == raw


def test_good_frames_before_a_bad_one_are_still_delivered():
    buf = FrameBuffer()
    it = buf.feed(REQ[6] + h("0002 0009 0006 01 06 0000 0001"))
    assert next(it).fc == 6
    with pytest.raises(FrameError):
        next(it)


def test_buffer_stays_dead_after_an_error():
    buf = FrameBuffer()
    with pytest.raises(FrameError):
        list(buf.feed(h("0001 0001 0006 01 03 0000 0001")))
    with pytest.raises(FrameError):
        list(buf.feed(REQ[6]))


# ---- decode_write -----------------------------------------------------------

def test_fc5_coil_on_and_off():
    assert decode_write(one(REQ[5]), IP, REGS) == [
        WriteRequest(IP, 5, "coil", 3, 1, "SAFE_MODE")]
    off = one(h("0005 0000 0006 01 05 0000 0000"))
    assert decode_write(off, IP, REGS) == [WriteRequest(IP, 5, "coil", 0, 0, "PUMP_CMD")]


def test_fc6_register():
    assert decode_write(one(REQ[6]), IP, REGS) == [
        WriteRequest(IP, 6, "register", 0, 100, "DOSER_RATE_PCT")]


def test_fc15_coils_lsb_first():
    reqs = decode_write(one(REQ[15]), IP, REGS)      # 0x15 = 1,0,1,0,1
    assert [(r.address, r.value, r.name) for r in reqs] == [
        (0, 1, "PUMP_CMD"), (1, 0, "DOSER_CMD"), (2, 1, "AUTO_MODE"),
        (3, 0, "SAFE_MODE"), (4, 1, "ALARM")]
    assert all(r.fc == 15 and r.kind == "coil" and r.src_ip == IP for r in reqs)


def test_fc15_spanning_two_bytes():
    f = one(h("0001 0000 0009 01 0F 0000 000A 02 01 02"))   # 10 coils: bit 0 and bit 9
    reqs = decode_write(f, IP, REGS)
    assert [r.value for r in reqs] == [1, 0, 0, 0, 0, 0, 0, 0, 0, 1]
    assert [r.address for r in reqs] == list(range(10))


def test_fc16_registers():
    assert decode_write(one(REQ[16]), IP, REGS) == [
        WriteRequest(IP, 16, "register", 1, 900, "LEVEL_HIGH_SP"),
        WriteRequest(IP, 16, "register", 2, 300, "LEVEL_LOW_SP")]


def test_twin_only_and_unknown_addresses_still_decode():
    twin = one(h("0001 0000 0006 01 06 0064 0001"))
    assert decode_write(twin, IP, REGS)[0].name == "TANK_LEVEL_PCT"
    unknown = one(h("0001 0000 0006 01 06 0063 0001"))
    assert decode_write(unknown, IP, REGS) == [WriteRequest(IP, 6, "register", 99, 1, None)]
    coil = one(h("0001 0000 0006 01 05 0063 FF00"))
    assert decode_write(coil, IP, REGS)[0].name is None


@pytest.mark.parametrize("fc", [1, 2, 3, 4])
def test_reads_are_not_writes(fc):
    with pytest.raises(FrameError):
        decode_write(one(REQ[fc]), IP, REGS)


@pytest.mark.parametrize("raw", [
    "0001 0000 0006 01 2B 0E01 0000",            # unknown function code
    "0001 0000 0003 01 86 02",                   # exception reply
    "0001 0000 0002 01 06",                      # fc 6, no data
    "0001 0000 0005 01 06 0000 00",              # fc 6, value cut short
    "0001 0000 0007 01 06 0000 0064 00",         # fc 6, trailing byte
    "0001 0000 0005 01 05 0000 FF",              # fc 5, cut short
    "0001 0000 0006 01 05 0000 0001",            # fc 5, value not FF00/0000
    "0001 0000 0006 01 05 0000 FF01",
    "0001 0000 0006 01 0F 0000 0005",            # fc 15, header only (looks like a reply)
    "0001 0000 0007 01 0F 0000 0005 01",         # fc 15, bits missing
    "0001 0000 0008 01 0F 0000 0005 02 15",      # fc 15, byte count != ceil(qty/8)
    "0001 0000 0009 01 0F 0000 0005 01 15 00",   # fc 15, trailing byte
    "0001 0000 0008 01 0F 0000 0000 01 15",      # fc 15, quantity 0
    "0001 0000 0008 01 0F FFFE 0005 01 15",      # fc 15, runs past address 65535
    "0001 0000 0006 01 10 0001 0002",            # fc 16, header only
    "0001 0000 000A 01 10 0001 0002 04 0384 01",  # fc 16, last value cut short
    "0001 0000 000B 01 10 0001 0002 03 0384 012C",  # fc 16, byte count != 2*qty
    "0001 0000 000B 01 10 0001 0003 04 0384 012C",  # fc 16, quantity != values
    "0001 0000 0007 01 10 0001 0000 00",         # fc 16, quantity 0
    "0001 0000 000B 01 10 FFFF 0002 04 0384 012C",  # fc 16, runs past address 65535
])
def test_malformed_write_pdus_are_rejected(raw):
    with pytest.raises(FrameError):
        decode_write(one(h(raw)), IP, REGS)


def test_fc15_quantity_over_spec_limit():
    # 1969 coils = 247 bytes = a full 253-byte PDU; the limit is 1968.
    # (FC 16 over its limit of 123 cannot fit in a legal frame at all.)
    f = one(h("0001 0000 00FE 01 0F 0000 07B1 F7") + bytes(247))
    with pytest.raises(FrameError):
        decode_write(f, IP, REGS)


def test_fc16_at_spec_limit():
    f = one(h("0001 0000 00FD 01 10 0000 007B F6") + bytes(246))
    assert len(decode_write(f, IP, REGS)) == 123


# ---- exception_reply --------------------------------------------------------

@pytest.mark.parametrize("fc,code,expected", [
    (6, 0x06, "0006 0000 0003 01 86 06"),        # HOLD
    (16, 0x01, "0008 0000 0003 01 90 01"),       # DENY
    (5, 0x01, "0005 0000 0003 01 85 01"),
    (15, 0x06, "0007 0000 0003 01 8F 06"),
])
def test_exception_reply_bytes(fc, code, expected):
    assert exception_reply(one(REQ[fc]), code) == h(expected)


def test_exception_reply_keeps_tid_and_unit_and_parses_back():
    req = one(h("BEEF 0000 0006 11 06 0000 0064"))
    f = one(exception_reply(req, 0x06))
    assert (f.tid, f.unit, f.fc, f.data, f.is_exception) == (0xBEEF, 0x11, 0x86, b"\x06", True)


def test_exception_reply_for_unknown_function_code():
    req = one(h("0001 0000 0006 01 2B 0E01 0000"))
    assert exception_reply(req, 0x01) == h("0001 0000 0003 01 AB 01")
