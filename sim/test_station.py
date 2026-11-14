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
def inputs(tmp_path):
    return tmp_path / "inputs.json"


@pytest.fixture
def port(inputs):
    p = free_port()
    threading.Thread(target=lambda: asyncio.run(station.serve("127.0.0.1", p, inputs_path=inputs)),
                     daemon=True).start()
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
    # sim boots with 40 % demand; other inputs read as 0
    assert wait_for(lambda: get(port, "input_register", 0) == 400)
    assert got[("discrete_input", 0)] == 0


def wait_for(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def get(port, kind, addr):
    return poke.read_all("127.0.0.1", port)[(kind, addr)]


def test_crc16_modbus_reference():
    assert station.crc16(b"123456789") == 0x4B37


def test_poke_write_changes_logic_hash(port):
    h0 = wait_for(lambda: get(port, "input_register", 10))
    poke.write("holding", 1, 900, "127.0.0.1", port)
    assert get(port, "holding_register", 1) == 900
    assert wait_for(lambda: get(port, "input_register", 10) != h0)


def test_auto_pump_follows_setpoints(port):
    poke.write("holding", 100, 300, "127.0.0.1", port)  # below LEVEL_LOW_SP 400
    assert wait_for(lambda: get(port, "coil", 0) == 1)
    poke.write("holding", 100, 900, "127.0.0.1", port)  # above LEVEL_HIGH_SP 850
    assert wait_for(lambda: get(port, "coil", 0) == 0)


def test_safe_mode_ignores_remote_writes_and_stops_doser(port):
    poke.write("coil", 3, 1, "127.0.0.1", port)
    assert wait_for(lambda: get(port, "coil", 1) == 0)
    poke.write("holding", 0, 77, "127.0.0.1", port)
    poke.write("coil", 2, 0, "127.0.0.1", port)
    assert get(port, "holding_register", 0) == 10
    assert get(port, "coil", 2) == 1
    poke.write("coil", 3, 0, "127.0.0.1", port)  # SAFE_MODE itself stays writable
    assert get(port, "coil", 3) == 0
    poke.write("holding", 0, 77, "127.0.0.1", port)
    assert get(port, "holding_register", 0) == 77


def test_set_inputs_presence_and_demand(port, inputs):
    poke.set_input("presence", 1, inputs)
    poke.set_input("DEMAND_PCT", 1000, inputs)
    assert wait_for(lambda: get(port, "discrete_input", 0) == 1)
    assert wait_for(lambda: get(port, "input_register", 0) == 1000)


def test_set_input_rejects_writable_and_computed(inputs):
    for name in ("PUMP_CMD", "TANK_LEVEL_PCT", "LOGIC_HASH", "NOPE"):
        with pytest.raises(ValueError):
            poke.set_input(name, 1, inputs)


def test_high_level_switch_forces_pump_off(port, inputs):
    poke.write("coil", 0, 1, "127.0.0.1", port)  # level 60 % is between setpoints: stays on
    assert wait_for(lambda: get(port, "coil", 0) == 1)
    poke.set_input("HIGH_LEVEL_SWITCH", 1, inputs)
    assert wait_for(lambda: get(port, "coil", 0) == 0)


def test_running_inputs_show_actual_relays(port, inputs):
    assert get(port, "discrete_input", 4) == 0  # boots pump off: doser cannot run
    assert wait_for(lambda: get(port, "discrete_input", 5) == 0)
    poke.write("coil", 0, 1, "127.0.0.1", port)
    assert wait_for(lambda: get(port, "discrete_input", 4) == 1)
    assert wait_for(lambda: get(port, "discrete_input", 5) == 1)
    poke.write("holding", 0, 0, "127.0.0.1", port)  # rate 0: doser relay off
    assert wait_for(lambda: get(port, "discrete_input", 5) == 0)
    poke.write("holding", 0, 10, "127.0.0.1", port)
    poke.set_input("HIGH_LEVEL_SWITCH", 1, inputs)  # interlock: both off, PUMP_CMD shows it
    assert wait_for(lambda: get(port, "discrete_input", 4) == 0)
    assert get(port, "discrete_input", 5) == 0
    with pytest.raises(ValueError):
        poke.set_input("PUMP_RUNNING", 1, inputs)


def test_link_keeps_one_connection(port):
    with poke.Link("127.0.0.1", port) as st:
        st.write("holding", 100, 612)
        st.write("holding", 101, 105)
        snap = st.read_all()
        assert (snap[("holding_register", 100)], snap[("holding_register", 101)]) == (612, 105)
        assert st.read("coil", 1) == 1


def test_link_unreachable_raises():
    with pytest.raises(ConnectionError):
        with poke.Link("127.0.0.1", free_port(), timeout=0.5):
            pass


def test_uptime_counts_seconds(port):
    u0 = get(port, "input_register", 11)
    time.sleep(1.2)
    assert get(port, "input_register", 11) > u0
