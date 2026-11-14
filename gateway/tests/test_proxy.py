"""Proxy in front of the real software station (subprocess), pymodbus as client."""
import asyncio
import logging
import socket
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gateway.proxy import EXC_TARGET_FAILED, Proxy
from sim import poke

ROOT = Path(__file__).resolve().parents[2]
HOST = "127.0.0.1"


def free_port():
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


class StationProc:
    """sim.station in its own process so it can be killed and restarted."""

    def __init__(self):
        self.port = free_port()
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "sim.station", "--port", str(self.port)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.time() + 15
        while time.time() < end:
            try:
                socket.create_connection((HOST, self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("station did not start")

    def stop(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None


@pytest.fixture
def station():
    s = StationProc()
    s.start()
    yield s
    s.stop()


@pytest.fixture
def proxy(station):
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    p = Proxy(HOST, station.port, host=HOST, port=0, connect_timeout=1.0)
    asyncio.run_coroutine_threadsafe(p.start(), loop).result(5)
    yield p
    asyncio.run_coroutine_threadsafe(p.close(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)


def stable(d):
    """Drop UPTIME_S (input register 11): it ticks between two reads."""
    return {k: v for k, v in d.items() if k != ("input_register", 11)}


def read_req(tid=1, addr=0, count=1):
    return struct.pack(">HHHBBHH", tid, 0, 6, 1, 3, addr, count)


def recv_frame(sock):
    """One reply ADU, or b'' if the peer closed."""
    try:
        head = sock.recv(6)
        if len(head) < 6:
            return b""
        return head + sock.recv(struct.unpack(">H", head[4:])[0])
    except ConnectionError:
        return b""


def test_reads_through_proxy_match_direct(station, proxy):
    assert stable(poke.read_all(HOST, proxy.port)) == stable(poke.read_all(HOST, station.port))


def test_write_through_proxy_lands_on_station(station, proxy, caplog):
    caplog.set_level(logging.INFO, logger="gateway.proxy")
    poke.write("holding", 1, 900, HOST, proxy.port)
    poke.write("coil", 4, 1, HOST, proxy.port)
    direct = poke.read_all(HOST, station.port)
    assert direct[("holding_register", 1)] == 900
    assert direct[("coil", 4)] == 1
    lines = [r.getMessage() for r in caplog.records]
    assert any("127.0.0.1 client->station" in m and "fc=6 addr=1 count=1 values=[900]" in m
               for m in lines)
    assert any("station->client" in m and "fc=6 addr=1" in m for m in lines)


def test_garbage_frame_closes_that_client_only(station, proxy):
    with socket.create_connection((HOST, proxy.port), timeout=3) as good, \
            socket.create_connection((HOST, proxy.port), timeout=3) as bad:
        bad.sendall(b"GET / HTTP/1.1\r\n\r\n")
        assert recv_frame(bad) == b""           # closed, no reply
        good.sendall(read_req(tid=7))           # the other client is unaffected
        assert recv_frame(good)[:2] == b"\x00\x07"
    assert stable(poke.read_all(HOST, proxy.port)) == stable(poke.read_all(HOST, station.port))


def test_malformed_write_closes_client(station, proxy):
    with socket.create_connection((HOST, proxy.port), timeout=3) as s:
        s.sendall(struct.pack(">HHHBBHH", 1, 0, 6, 1, 5, 0, 0x1234))  # FC5 value not FF00/0000
        assert recv_frame(s) == b""
    assert poke.read_all(HOST, station.port)[("coil", 0)] == 0
    poke.read_all(HOST, proxy.port)


def test_several_clients_at_once(station, proxy):
    direct = stable(poke.read_all(HOST, station.port))
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda _: poke.read_all(HOST, proxy.port), range(6)))
    assert all(stable(r) == direct for r in results)


def test_station_restart_recovers(station, proxy):
    with socket.create_connection((HOST, proxy.port), timeout=5) as held:
        held.sendall(read_req(tid=1))
        assert recv_frame(held)[7] == 3                       # normal reply
        station.stop()
        held.sendall(read_req(tid=2))                         # station dropped mid-session
        assert recv_frame(held)[7:9] == bytes([0x83, EXC_TARGET_FAILED])

    with socket.create_connection((HOST, proxy.port), timeout=5) as s:   # station unreachable
        s.sendall(read_req(tid=3))
        assert recv_frame(s)[7:9] == bytes([0x83, EXC_TARGET_FAILED])
    with pytest.raises(Exception):
        poke.read_all(HOST, proxy.port)

    station.start()
    assert stable(poke.read_all(HOST, proxy.port)) == stable(poke.read_all(HOST, station.port))
