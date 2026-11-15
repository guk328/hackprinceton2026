"""Assert the two command sequences and distinguish expected holds from faults."""
import contextlib
import io
from types import SimpleNamespace
import unittest

from attacks.normal_day import run as normal
from attacks.chlorine_overdose import run as overdose


class Reply:
    def __init__(self, code=None):
        self.exception_code = code

    def isError(self):
        return self.exception_code is not None


class Link:
    def __init__(self, rate_reply=6, leak=False):
        self.regs = SimpleNamespace(unit_id=1)
        self.client = self
        self.calls = []
        self.rate_reply, self.leak = rate_reply, leak
        self.values = {('holding_register', 0): 10, ('holding_register', 1): 850,
                       ('holding_register', 2): 400, ('coil', 0): 0, ('coil', 1): 1,
                       ('coil', 3): 0, ('discrete_input', 4): 1, ('discrete_input', 5): 1}

    def read(self, kind, address):
        return self.values[(kind, address)]

    def write_coil(self, address, value, *, device_id):
        self.calls.append(('coil', address, int(value), device_id))
        self.values[('coil', address)] = int(value)
        return Reply()

    def write_register(self, address, value, *, device_id):
        self.calls.append(('register', address, value, device_id))
        code = self.rate_reply if address == 0 and value == 100 else None
        if code is None or self.leak:
            self.values[('holding_register', address)] = value
        return Reply(code)


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_normal_commands_are_small_and_use_unit_one(self):
        link = Link()
        normal(link)
        self.assertEqual(link.calls, [('coil', 0, 1, 1), ('register', 1, 840, 1)])

    def test_low_boundary_uses_upward_small_change(self):
        link = Link()
        link.values[('holding_register', 1)] = 500
        normal(link)
        self.assertEqual(link.calls[-1], ('register', 1, 510, 1))

    def test_overdose_hold_keeps_rate_and_continues_harmless_doser_command(self):
        link = Link()
        overdose(link)
        self.assertEqual(link.calls, [('register', 0, 100, 1), ('coil', 1, 1, 1)])
        self.assertEqual(link.values[('holding_register', 0)], 10)

    def test_deny_and_connection_exception_are_not_mistaken_for_hold(self):
        for code in (1, 11):
            link = Link(rate_reply=code)
            with self.subTest(code=code), self.assertRaises(RuntimeError):
                overdose(link)
            self.assertEqual(len(link.calls), 1)

    def test_unexpected_allow_fails_instead_of_claiming_protection(self):
        link = Link(rate_reply=None)
        with self.assertRaises(RuntimeError):
            overdose(link)
        self.assertEqual(len(link.calls), 1)

    def test_hold_with_changed_readback_fails(self):
        with self.assertRaises(RuntimeError):
            overdose(Link(leak=True))

    def test_missing_pump_or_safe_mode_stops_before_attack(self):
        for key in (('discrete_input', 4), ('discrete_input', 5), ('coil', 3)):
            link = Link()
            link.values[key] = 1 if key == ('coil', 3) else 0
            with self.subTest(key=key), self.assertRaises(ValueError):
                overdose(link)
            self.assertEqual(link.calls, [])

    def test_transport_error_propagates_instead_of_becoming_hold(self):
        link = Link()
        def disconnected(*args, **kwargs):
            raise ConnectionError('offline')
        link.client.write_register = disconnected
        with self.assertRaises(ConnectionError):
            overdose(link)
