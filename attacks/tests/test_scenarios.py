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


# ---- scenarios 2-5 ----
from attacks.drain_town import run as drain            # noqa: E402
from attacks.overflow import run as overflow           # noqa: E402
from attacks.logic_tamper import run as tamper         # noqa: E402
from attacks.operator_on_site import run as on_site    # noqa: E402


class Gateway(Link):
    """Fake gateway: `holds` maps (kind, address, value) to an exception code."""

    def __init__(self, holds=(), values=()):
        super().__init__()
        self.holds = dict(holds)
        self.values.update({('input_register', 0): 1000, ('coil', 0): 1, ('coil', 2): 1,
                            ('discrete_input', 0): 1, ('discrete_input', 1): 1,
                            ('input_register', 10): 42108, **dict(values)})

    def _write(self, kind, store, address, value):
        self.calls.append((kind, address, value, 1))
        code = self.holds.get((kind, address, value))
        if code is None:
            self.values[(store, address)] = value
        return Reply(code)

    def write_coil(self, address, value, *, device_id):
        return self._write('coil', 'coil', address, int(value))

    def write_register(self, address, value, *, device_id):
        return self._write('register', 'holding_register', address, value)

    def write(self, what, address, value):        # sim.poke.Link.write, used by the tamper script
        self.values[('holding_register', address)] = value
        self.values[('input_register', 10)] += 1


class MoreScenarioTests(ScenarioTests):
    def test_drain_pump_stop_is_held_and_auto_mode_is_restored(self):
        link = Gateway({('coil', 0, 0): 6})
        drain(link)
        self.assertEqual(link.calls, [('coil', 2, 0, 1), ('coil', 0, 0, 1), ('coil', 2, 1, 1)])
        self.assertEqual((link.values[('coil', 0)], link.values[('coil', 2)]), (1, 1))

    def test_drain_that_gets_through_fails(self):
        with self.assertRaises(RuntimeError):
            drain(Gateway())

    def test_drain_needs_high_demand(self):
        link = Gateway(values={('input_register', 0): 400})
        with self.assertRaises(ValueError):
            drain(link)
        self.assertEqual(link.calls, [])

    def test_overflow_hold_keeps_setpoint(self):
        link = Gateway({('register', 1, 1000): 6})
        overflow(link)
        self.assertEqual(link.calls, [('register', 1, 1000, 1), ('coil', 0, 1, 1)])
        self.assertEqual(link.values[('holding_register', 1)], 850)

    def test_overflow_allowed_or_denied_fails(self):
        for holds in ({}, {('register', 1, 1000): 1}):
            with self.subTest(holds=holds), self.assertRaises(RuntimeError):
                overflow(Gateway(holds))

    def test_tamper_passes_only_when_reverted(self):
        link = Gateway(values={('holding_register', 3): 100, ('holding_register', 4): 1})
        ticks = iter(range(1000))

        def revert(_):
            link.values[('holding_register', 1)] = 850
        tamper(link, wait_s=5, sleep=revert, clock=lambda: next(ticks))
        link = Gateway(values={('holding_register', 3): 100, ('holding_register', 4): 1})
        ticks = iter(range(1000))
        with self.assertRaises(RuntimeError):
            tamper(link, wait_s=5, sleep=lambda _: None, clock=lambda: next(ticks))

    def test_on_site_waits_for_release(self):
        link = Gateway({('register', 0, 40): 6})
        ticks = iter(range(1000))

        def release(_):
            link.values[('holding_register', 0)] = 40
        on_site(link, wait_s=5, sleep=release, clock=lambda: next(ticks))
        self.assertEqual(link.calls, [('register', 0, 40, 1)])

    def test_on_site_needs_presence_and_fails_without_release(self):
        link = Gateway(values={('discrete_input', 0): 0})
        with self.assertRaises(ValueError):
            on_site(link)
        link, ticks = Gateway({('register', 0, 40): 6}), iter(range(1000))
        with self.assertRaises(RuntimeError):
            on_site(link, wait_s=3, sleep=lambda _: None, clock=lambda: next(ticks))
