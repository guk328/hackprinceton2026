"""The twin uses the CompE Link protocol; no live network needed here."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from physics import PlantState, step
from twin.run import decode, encode, tick


def snapshot(pump=1, doser=1, demand=400):
    values = {("coil", a): v for a, v in enumerate((0, 0, 1, 0, 0))}
    values.update({("holding_register", a): v for a, v in enumerate((10, 850, 400, 100, 1))})
    values.update({("discrete_input", 4): pump, ("discrete_input", 5): doser,
                   ("input_register", 0): demand, ("holding_register", 100): 600,
                   ("holding_register", 101): 100})
    return values


class FakeLink:
    def __init__(self, values=None):
        self.values = snapshot() if values is None else values
        self.writes = []
        self.reads = 0

    def read_all(self):
        self.reads += 1
        return dict(self.values)

    def write(self, kind, address, value):
        self.writes.append((kind, address, value))
        self.values[("holding_register", address)] = value


class TwinTests(unittest.TestCase):
    def test_wire_scales_and_actual_relays_override_command_coils(self):
        values = snapshot(pump=0, doser=0, demand=805)
        values[("coil", 0)] = values[("coil", 1)] = 1
        values[("holding_register", 100)] = 612
        values[("holding_register", 101)] = 125
        state = decode(values)
        self.assertEqual((state.level_pct, state.chlorine_mgl, state.demand_pct), (61.2, 1.25, 80.5))
        self.assertFalse(state.pump_on)
        self.assertFalse(state.doser_on)
        self.assertFalse(state.auto_mode)
        self.assertFalse(state.safe_mode)
        self.assertEqual((state.level_high_sp, state.level_low_sp, state.chlorine_target_mgl), (85, 40, 1))

    def test_sixty_plant_seconds_use_six_ten_second_steps_and_only_two_writes(self):
        link = FakeLink()
        expected = decode(link.values)
        for _ in range(6):
            expected = step(expected, dt_s=10)
        with patch("twin.run.step", wraps=step) as integrator:
            state = tick(link)
        self.assertEqual(state, expected)
        self.assertEqual(integrator.call_count, 6)
        self.assertTrue(all(c.kwargs["dt_s"] == 10 for c in integrator.call_args_list))
        self.assertEqual(link.reads, 1)
        self.assertEqual(link.writes, [("holding", 100, 602), ("holding", 101, 100)])
        self.assertAlmostEqual(state.level_pct, 60.175)

    def test_fractional_tick_has_short_final_substep(self):
        with patch("twin.run.step", wraps=step) as integrator:
            state = tick(FakeLink(), speedup=12.5)
        self.assertEqual([c.kwargs["dt_s"] for c in integrator.call_args_list], [10, 2.5])
        self.assertEqual(state.elapsed_s, 12.5)

    def test_reset_publishes_exact_start_values_without_integrating(self):
        link = FakeLink()
        link.values[("holding_register", 100)] = 900
        with patch("twin.run.step", wraps=step) as integrator:
            state = tick(link, reset=True)
        integrator.assert_not_called()
        self.assertEqual((state.level_pct, state.chlorine_mgl, state.elapsed_s), (60, 1, 0))
        self.assertEqual(link.writes, [("holding", 100, 600), ("holding", 101, 100)])

    def test_retains_unrounded_water_state_and_rereads_controls(self):
        link = FakeLink()
        first = tick(link)
        link.values[("discrete_input", 4)] = link.values[("discrete_input", 5)] = 0
        link.values[("input_register", 0)] = 800
        second = tick(link, first)
        self.assertAlmostEqual(second.level_pct, first.level_pct - 0.15)
        self.assertEqual(second.elapsed_s, 120)
        self.assertEqual(link.reads, 2)
        self.assertFalse(second.pump_on)

    def test_station_safe_mode_and_interlock_remain_authoritative(self):
        link = FakeLink(snapshot(pump=0, doser=0))
        link.values[("coil", 3)] = 1
        link.values[("holding_register", 100)] = 100  # local model would start at 10%
        result = tick(link)
        self.assertFalse(result.pump_on)
        self.assertFalse(result.doser_on)
        self.assertLess(result.level_pct, 10)

    def test_missing_and_invalid_reads_cause_no_writes_even_for_reset(self):
        bad_values = (None, -1, 65536, float("nan"), "600")
        for key in snapshot():
            values = snapshot()
            del values[key]
            for reset in (False, True):
                link = FakeLink(values)
                with self.subTest(key=key, reset=reset), self.assertRaises(KeyError):
                    tick(link, reset=reset)
                self.assertEqual(link.writes, [])
        for value in bad_values:
            link = FakeLink()
            link.values[("holding_register", 100)] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                tick(link)
            self.assertEqual(link.writes, [])
        for key, value in ((('input_register', 0), 1001), (('holding_register', 0), 101),
                           (('discrete_input', 4), 2)):
            link = FakeLink()
            link.values[key] = value
            with self.assertRaises(ValueError):
                tick(link)
            self.assertEqual(link.writes, [])

    def test_inconsistent_relays_are_rejected(self):
        link = FakeLink(snapshot(pump=0, doser=1))
        with self.assertRaises(ValueError):
            tick(link)
        self.assertEqual(link.writes, [])

    def test_disconnected_read_does_not_write(self):
        link = FakeLink()
        with patch.object(link, "read_all", side_effect=ConnectionError("disconnected")):
            with self.assertRaises(ConnectionError):
                tick(link)
        self.assertEqual(link.writes, [])

    def test_failed_first_write_stops_before_second(self):
        link = FakeLink()
        with patch.object(link, "write", side_effect=ConnectionError("disconnected")) as writer:
            with self.assertRaises(ConnectionError):
                tick(link)
        self.assertEqual(writer.call_count, 1)

    def test_output_encoding_rejects_invalid_or_unrepresentable_values(self):
        self.assertEqual(encode(PlantState(level_pct=61.24, chlorine_mgl=1.234)), (612, 123))
        for state in (PlantState(level_pct=-1), PlantState(chlorine_mgl=float("nan")),
                      PlantState(level_pct=7000), PlantState(chlorine_mgl=700)):
            with self.subTest(state=state), self.assertRaises(ValueError):
                encode(state)

    def test_invalid_speedup_does_not_read_or_write(self):
        for speedup in (0, -1, float("nan"), float("inf"), True, 3601):
            link = FakeLink()
            with self.subTest(speedup=speedup), self.assertRaises(ValueError):
                tick(link, speedup=speedup)
            self.assertEqual(link.reads, 0)
            self.assertEqual(link.writes, [])

    def test_observed_pump_start_is_recorded_once(self):
        link = FakeLink(snapshot(pump=0, doser=0))
        state = tick(link)
        link.values[("discrete_input", 4)] = 1
        running = tick(link, state)
        self.assertEqual(running.pump_start_times_s, (60,))
        self.assertEqual(tick(link, running).pump_start_times_s, (60,))
