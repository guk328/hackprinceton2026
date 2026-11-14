"""Friday handoff: real guards, first-crossing evidence and wire envelopes."""

from dataclasses import asdict, replace
import json
from pathlib import Path
import re
import unittest

from physics import PlantState, Command, predict, step
from physics.limits import SETPOINT_BOUNDS


class MilestoneTests(unittest.TestCase):
    def test_overdose_payload_matches_first_sample_over_threshold(self):
        state = PlantState(level_pct=40, auto_mode=False)
        command = Command(kind="register", address=0, value=100)
        result = predict(state, command)
        self.assertFalse(result.safe)
        self.assertTrue(result.assessment_complete)
        self.assertEqual(len(result.trajectory), 721)
        self.assertEqual(len(result.violations), 1)
        violation = result.violations[0]
        self.assertEqual(violation.code, "CHLORINE_HIGH")
        self.assertFalse(violation.pre_existing)
        self.assertIsNone(violation.baseline_minutes_until)
        self.assertEqual(violation.limit, 4.0)
        self.assertIn("mg/L", violation.message)
        self.assertGreater(violation.minutes_until, 60)
        self.assertLess(violation.minutes_until, 120)
        index = next(i for i, point in enumerate(result.trajectory) if point[2] > 4)
        self.assertEqual(violation.minutes_until, result.trajectory[index][0])
        self.assertEqual(violation.value, result.trajectory[index][2])
        self.assertLessEqual(result.trajectory[index - 1][2], 4)
        self.assertEqual(state, PlantState(level_pct=40, auto_mode=False))
        self.assertEqual(result, predict(state, command))
        json.dumps(asdict(result), allow_nan=False)

    def test_overflow_time_agrees_with_analytic_fill_time(self):
        state = PlantState(level_pct=90, auto_mode=False, pump_on=False)
        result = predict(state, Command("coil", 0, 1))
        self.assertFalse(result.safe)
        self.assertEqual(len(result.violations), 1)
        violation = result.violations[0]
        self.assertEqual(violation.code, "TANK_OVERFLOW")
        self.assertFalse(violation.pre_existing)
        self.assertEqual(violation.limit, 95)
        analytic_min = (95 - 90) / 10.5 * 60
        self.assertGreater(violation.minutes_until, analytic_min)
        self.assertLessEqual(violation.minutes_until - analytic_min, 10 / 60)
        index = next(i for i, point in enumerate(result.trajectory) if point[1] > 95)
        self.assertEqual(violation.minutes_until, result.trajectory[index][0])
        self.assertEqual(violation.value, result.trajectory[index][1])
        self.assertLessEqual(result.trajectory[index - 1][1], 95)
        self.assertTrue(all(point[1] <= 95 for point in result.baseline_trajectory))

    def test_invalid_high_setpoint_keeps_physical_overflow_evidence(self):
        state = PlantState(level_pct=90, level_high_sp=85, pump_on=True)
        result = predict(state, Command("register", 1, 1000))
        by_code = {v.code: v for v in result.violations}
        self.assertFalse(result.safe)
        self.assertTrue(result.assessment_complete)
        self.assertEqual(set(by_code), {"TANK_OVERFLOW", "SETPOINT_OUT_OF_RANGE"})
        self.assertEqual(by_code["SETPOINT_OUT_OF_RANGE"].minutes_until, 0)
        self.assertEqual(by_code["SETPOINT_OUT_OF_RANGE"].value, 100)
        self.assertEqual(by_code["SETPOINT_OUT_OF_RANGE"].limit, 95)
        self.assertFalse(by_code["TANK_OVERFLOW"].pre_existing)
        self.assertGreater(by_code["TANK_OVERFLOW"].minutes_until, 0)
        self.assertEqual(state.level_high_sp, 85)

    def test_strict_thresholds_and_multiple_existing_hazards(self):
        boundary = PlantState(level_pct=95, chlorine_mgl=4, auto_mode=False)
        self.assertTrue(predict(boundary, None, horizon_min=0).safe)
        above = replace(boundary, level_pct=95.01, chlorine_mgl=4.01)
        result = predict(above, None, horizon_min=1)
        self.assertFalse(result.safe)
        self.assertEqual({v.code for v in result.violations}, {"CHLORINE_HIGH", "TANK_OVERFLOW"})
        for violation in result.violations:
            self.assertEqual(violation.minutes_until, 0)
            self.assertTrue(violation.pre_existing)
            self.assertEqual(violation.baseline_minutes_until, 0)

    def test_accelerated_existing_chlorine_hazard_keeps_both_crossing_times(self):
        state = PlantState(level_pct=40, chlorine_mgl=3, doser_rate_pct=80, auto_mode=False)
        result = predict(state, Command("register", 0, 100))
        overdose = next(v for v in result.violations if v.code == "CHLORINE_HIGH")
        self.assertTrue(overdose.pre_existing)
        self.assertGreater(overdose.minutes_until, 0)
        self.assertLess(overdose.minutes_until, overdose.baseline_minutes_until)

    def test_safe_command_and_repair_of_invalid_setting(self):
        state = PlantState()
        result = predict(state, Command("register", 1, 840))
        self.assertTrue(result.safe)
        self.assertTrue(result.assessment_complete)
        self.assertEqual(result.violations, [])
        self.assertEqual(set(result.checked_limits),
                         {"CHLORINE_HIGH", "TANK_OVERFLOW", "SETPOINT_OUT_OF_RANGE"})
        invalid = replace(state, level_high_sp=100)
        self.assertTrue(predict(invalid, Command("register", 1, 850)).safe)
        self.assertEqual(step(state).pump_on, True)  # Public import smoke check.

    def test_setpoint_bounds_follow_yaml_and_detect_reversed_pair(self):
        contract = (Path(__file__).resolve().parents[2] / "shared" / "registers.yaml").read_text()
        for wire, field, address in (("DOSER_RATE_PCT", "doser_rate_pct", 0),
                                     ("LEVEL_HIGH_SP", "level_high_sp", 1),
                                     ("LEVEL_LOW_SP", "level_low_sp", 2),
                                     ("CHLORINE_TARGET", "chlorine_target_mgl", 3)):
            match = re.search(rf"\b{wire}:.*scale:\s*(\d+).*min:\s*(\d+).*max:\s*(\d+)", contract)
            self.assertIsNotNone(match)
            scale, low, high = map(int, match.groups())
            self.assertEqual(SETPOINT_BOUNDS[field][:2], (low / scale, high / scale))
            for value in (low, high):
                self.assertTrue(predict(PlantState(), Command("register", address, value), horizon_min=0).safe)
        for command in (Command("register", 1, 499), Command("register", 2, 701), Command("register", 3, 401)):
            result = predict(PlantState(), command, horizon_min=0)
            self.assertFalse(result.safe)
            self.assertEqual(result.violations[0].code, "SETPOINT_OUT_OF_RANGE")
        reversed_pair = PlantState(level_low_sp=60, level_high_sp=55)
        self.assertFalse(predict(reversed_pair, None, horizon_min=0).safe)
