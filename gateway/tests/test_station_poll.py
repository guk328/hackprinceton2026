"""StationPoller against the real sim in a subprocess: snapshot, drop, recover."""
import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

from gateway.station import StationPoller
from sim import poke

ROOT = Path(__file__).resolve().parents[2]
HOST = "127.0.0.1"


def free_port():
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


class Sim:
    def __init__(self, port):
        self.port, self.proc = port, None

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
        self.proc.kill()
        self.proc.wait()


async def wait_for(pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.1)
    return False


def test_snapshot_then_recover_after_station_restart():
    sim = Sim(free_port())
    sim.start()
    try:
        poke.write("holding", 1, 900, HOST, sim.port)

        async def scenario():
            p = StationPoller(HOST, sim.port, interval=0.2, timeout=0.5)
            await p.start()
            try:
                assert await wait_for(lambda: p.snapshot.connected)
                s = p.snapshot
                assert s.holding["LEVEL_HIGH_SP"] == 900
                assert s.coils["AUTO_MODE"] == 1
                assert "LOGIC_HASH" in s.input_registers and "PRESENCE" in s.discrete_inputs
                assert p.last_good_logic == {0: 10, 1: 900, 2: 400, 3: 100, 4: 1}

                sim.stop()                                  # station drops
                assert await wait_for(lambda: not p.snapshot.connected)
                assert p.snapshot.holding["LEVEL_HIGH_SP"] == 900   # stale copy kept
                assert p.last_good_logic[1] == 900

                sim.start()                                 # back with defaults
                assert await wait_for(lambda: p.snapshot.connected)
                assert p.snapshot.holding["LEVEL_HIGH_SP"] == 850
                assert p.last_good_logic[1] == 900          # LKG only moves on allowed writes
            finally:
                await p.close()

        asyncio.run(scenario())
    finally:
        sim.stop()


def test_accept_logic_updates_last_good():
    sim = Sim(free_port())
    sim.start()
    try:
        async def scenario():
            p = StationPoller(HOST, sim.port, interval=0.2, timeout=0.5)
            await p.start()
            try:
                assert await wait_for(lambda: p.snapshot.connected)
                p.accept_logic({1: 800})
                assert p.last_good_logic[1] == 800
            finally:
                await p.close()

        asyncio.run(scenario())
    finally:
        sim.stop()
