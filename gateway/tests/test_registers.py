from gateway.registers import load_registers


def test_lookup_holding_with_scale_and_limits():
    r = load_registers().lookup("holding_register", 1)
    assert (r.name, r.scale, r.default, r.min, r.max) == ("LEVEL_HIGH_SP", 10, 850, 500, 950)
    assert not r.twin_only


def test_twin_only():
    regs = load_registers()
    assert regs.lookup("holding_register", 100).twin_only
    assert regs.lookup("holding_register", 101).name == "CHLORINE_MGL"
    assert not regs.lookup("holding_register", 0).twin_only


def test_kinds_are_separate_address_spaces():
    regs = load_registers()
    assert regs.lookup("coil", 0).name == "PUMP_CMD"
    assert regs.lookup("discrete_input", 0).name == "PRESENCE"
    assert regs.lookup("input_register", 0).name == "DEMAND_PCT"
    assert regs.lookup("holding_register", 0).name == "DOSER_RATE_PCT"


def test_unknown_returns_none():
    regs = load_registers()
    assert regs.lookup("holding_register", 99) is None
    assert regs.lookup("bogus", 0) is None


def test_by_name_and_defaults():
    regs = load_registers()
    assert regs.by_name("SAFE_MODE").addr == 3
    assert regs.by_name("NOPE") is None
    assert regs.defaults("holding_register")[0] == 10
    assert regs.defaults("coil") == {0: 0, 1: 1, 2: 1, 3: 0, 4: 0}


def test_read_only_kinds_and_ports():
    regs = load_registers()
    assert regs.lookup("input_register", 10).writable is False
    assert regs.lookup("coil", 3).writable is True
    assert regs.unit_id == 1 and regs.ports["sim_station"] == 5020
