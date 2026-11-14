"""Hand-derived balance and control checks; no third-party test dependency."""

from dataclasses import replace
from math import isfinite
import unittest

from physics import PlantState, step


class ModelTests(unittest.TestCase):
    def test_one_second_matches_volume_and_mass_balances(self):
        state = PlantState()
        result = step(state, dt_s=1.0)
        # 240 m³ initially, Qp=60, Qd=18 m³/h; dose=60 g/h.
        expected_volume_m3 = 240.0 + 42.0 / 3600.0
        expected_mass_g = 240.0 + (60.0 - 18.0 - 12.0) / 3600.0
        self.assertAlmostEqual(result.level_pct, expected_volume_m3 / 4.0)
        self.assertAlmostEqual(result.chlorine_mgl, expected_mass_g / expected_volume_m3)
        self.assertEqual(state, PlantState())
        self.assertIsNot(result, state)

    def test_no_dose_arrives_without_both_pump_and_doser(self):
        pump_off = PlantState(pump_on=False, auto_mode=False)
        self.assertEqual(step(replace(pump_off, doser_rate_pct=100)).chlorine_mgl,
                         step(replace(pump_off, doser_rate_pct=0)).chlorine_mgl)
        doser_off = PlantState(doser_on=False, auto_mode=False)
        self.assertEqual(step(replace(doser_off, doser_rate_pct=100)).chlorine_mgl,
                         step(replace(doser_off, doser_rate_pct=0)).chlorine_mgl)

    def test_auto_hysteresis_and_manual_override(self):
        for level_pct, previous_on, expected_on in (
            (39.0, False, True), (86.0, True, False),
            (60.0, False, False), (60.0, True, True),
            (40.0, False, False), (85.0, True, True),
        ):
            with self.subTest(level_pct=level_pct, previous_on=previous_on):
                state = PlantState(level_pct=level_pct, pump_on=previous_on)
                self.assertEqual(step(state).pump_on, expected_on)
        self.assertFalse(step(PlantState(level_pct=39, pump_on=False, auto_mode=False)).pump_on)

    def test_empty_and_nearly_empty_tank_remain_finite_and_low(self):
        for level_pct in (0.0, 0.000001):
            state = PlantState(level_pct=level_pct, pump_on=False, auto_mode=False)
            result = step(state)
            self.assertEqual(result.level_pct, 0.0)
            self.assertEqual(result.chlorine_mgl, 0.0)
            self.assertTrue(isfinite(result.chlorine_mgl))
        filling = step(PlantState(level_pct=0, demand_pct=0))
        self.assertGreater(filling.level_pct, 0)
        self.assertAlmostEqual(filling.chlorine_mgl, 1.0)

    def test_forecast_level_is_not_clipped_at_capacity(self):
        result = step(PlantState(level_pct=100, auto_mode=False, demand_pct=0))
        self.assertGreater(result.level_pct, 100)

    def test_zero_duration_and_invalid_duration(self):
        self.assertEqual(step(PlantState(), dt_s=0), PlantState())
        for dt_s in (-1, float("nan"), float("inf")):
            with self.subTest(dt_s=dt_s), self.assertRaises(ValueError):
                step(PlantState(), dt_s=dt_s)


    def test_safe_mode_uses_local_hysteresis_and_suppresses_dosing(self):
        for level, previous, expected in ((39, False, True), (86, True, False),
                                          (40, False, False), (85, True, True),
                                          (60, False, False), (60, True, True)):
            with self.subTest(level=level, previous=previous):
                state = PlantState(level_pct=level, pump_on=previous, safe_mode=True,
                                   auto_mode=False, level_low_sp=5, level_high_sp=10,
                                   doser_rate_pct=100)
                result = step(state)
                self.assertEqual(result.pump_on, expected)
                self.assertFalse(result.doser_on)
                self.assertEqual(result.chlorine_mgl,
                                 step(replace(state, doser_on=False)).chlorine_mgl)

    def test_plant_clock_and_history_follow_actual_local_transitions(self):
        initial = PlantState(level_pct=39, pump_on=False, elapsed_s=3600,
                             pump_start_times_s=(0, 1, 100))
        snapshot = replace(initial)
        started = step(initial, dt_s=5)
        self.assertEqual(started.elapsed_s, 3605)
        self.assertEqual(started.pump_start_times_s, (100, 3600))
        running = step(started, dt_s=5)
        self.assertEqual(running.pump_start_times_s, started.pump_start_times_s)
        stopped = step(replace(running, level_pct=86), dt_s=5)
        self.assertFalse(stopped.pump_on)
        self.assertEqual(stopped.pump_start_times_s, started.pump_start_times_s)
        self.assertEqual(initial, snapshot)
        self.assertEqual(step(initial, dt_s=0), initial)

    def test_nonfinite_physical_state_is_rejected_by_step(self):
        for field in ("level_pct", "chlorine_mgl", "demand_pct", "doser_rate_pct",
                      "level_high_sp", "level_low_sp", "elapsed_s"):
            for value in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    step(replace(PlantState(), **{field: value}))
