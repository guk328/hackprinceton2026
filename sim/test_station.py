import asyncio
import socket
import threading
import time

import pytest

from gateway.registers import load_registers
from sim import poke, station


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def port():
    p = free_port()
    threading.Thread(target=lambda: asyncio.run(station.serve("127.0.0.1", p)), daemon=True).start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", p), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return p


def test_read_all_matches_yaml_defaults(port):
    regs = load_registers()
    got = poke.read_all("127.0.0.1", port)
    for kind in ("coil", "holding_register"):
        for addr, default in regs.defaults(kind).items():
            r = regs.lookup(kind, addr)
            assert got[(kind, addr)] == default, r.name
    # inputs without a default read as 0
    assert got[("input_register", 0)] == 0
    assert got[("discrete_input", 0)] == 0
