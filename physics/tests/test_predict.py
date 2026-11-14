"""Command/rollout contract checks using the implemented physical detectors."""

from dataclasses import replace
from pathlib import Path
import re
import unittest

from physics import Command, PlantState, Violation, predict, step
from physics.predict import _apply_command, _attribute_violations


class ForecastTests(unittest.TestCase):
    def test_manual_pump_command_changes_candidate_not_baseline_or_input(self):
        state = PlantState(auto_mode=False)
        command = Command("coil", 0, 0)
        result = predict(state, command, horizon_min=1)
        self.assertAlmostEqual(result.trajectory[-1][1], 59.925)
        self.assertAlmostEqual(result.baseline_trajectory[-1][1], 60.175)
        self.assertEqual(state, PlantState(auto_mode=False))
        self.assertEqual(command, Command("coil", 0, 0))
        self.assertEqual(result, predict(state, command, horizon_min=1))

    def test_none_and_zero_or_partial_horizons(self):
        for horizon, count in ((0, 1), (0.05, 2), (0.25, 3), (120, 721)):
            with self.subTest(horizon=horizon):
                result = predict(PlantState(), None, horizon_min=horizon)
                self.assertEqual(len(result.trajectory), count)
                self.assertEqual(result.trajectory, result.baseline_trajectory)
                self.assertEqual(result.trajectory[0], (0, 60, 1))
                self.assertEqual(result.trajectory[-1][0], horizon)
                self.assertEqual(result.violations, [])
                self.assertTrue(result.safe)
                self.assertTrue(result.assessment_complete)

    def test_wire_mapping_and_units_match_shared_yaml(self):
        contract = (Path(__file__).resolve().parents[2] / "shared" / "registers.yaml").read_text()
        coils = contract.split("coils:", 1)[1].split("discrete_inputs:", 1)[0]
        registers = contract.split("holding_registers:", 1)[1]
        cases = (
            ("DOSER_RATE_PCT", "doser_rate_pct", 37, 37, 1),
            ("LEVEL_HIGH_SP", "level_high_sp", 850, 85, 10),
            ("LEVEL_LOW_SP", "level_low_sp", 400, 40, 10),
            ("CHLORINE_TARGET", "chlorine_target_mgl", 125, 1.25, 100),
            ("LOGIC_VERSION", "logic_version", 8, 8, 1),
        )
        for name, field, raw, expected, scale in cases:
            with self.subTest(name=name):
                match = re.search(rf"\b{name}:\s*\{{addr:\s*(\d+),\s*scale:\s*(\d+)", registers)
                self.assertIsNotNone(match)
                self.assertEqual(int(match[2]), scale)
                candidate = _apply_command(PlantState(), Command("register", int(match[1]), raw))
                self.assertEqual(getattr(candidate, field), expected)
        for name, field in (("PUMP_CMD", "pump_on"), ("DOSER_CMD", "doser_on"),
                            ("AUTO_MODE", "auto_mode"), ("SAFE_MODE", "safe_mode"),
                            ("ALARM", "alarm")):
            match = re.search(rf"\b{name}:\s*\{{addr:\s*(\d+)", coils)
            self.assertIsNotNone(match)
            for value in (0, 1):
                with self.subTest(name=name, value=value):
                    candidate = _apply_command(PlantState(), Command("coil", int(match[1]), value))
                    self.assertIs(getattr(candidate, field), bool(value))

    def test_auto_mode_and_safe_mode_have_physical_effects(self):
        state = PlantState(level_pct=39, auto_mode=False, pump_on=False)
        automatic = predict(state, Command("coil", 2, 1), horizon_min=1)
        self.assertAlmostEqual(automatic.trajectory[-1][1], 39.175)
        self.assertAlmostEqual(automatic.baseline_trajectory[-1][1], 38.925)
        tampered = replace(state, level_low_sp=5, level_high_sp=10)
        safe = step(_apply_command(tampered, Command("coil", 3, 1)))
        self.assertTrue(safe.pump_on)  # Local 40/85% setpoints, not remote 5/10%.
        self.assertFalse(safe.doser_on)
        locked = replace(state, safe_mode=True)
        for command in (Command("coil", 0, 1), Command("register", 0, 100)):
            result = predict(locked, command, horizon_min=1)
            self.assertEqual(result.trajectory, result.baseline_trajectory)

    def test_configuration_only_fields_do_not_invent_physics_feedback(self):
        for command in (Command("register", 3, 125), Command("register", 4, 2), Command("coil", 4, 1)):
            result = predict(PlantState(), command, horizon_min=1)
            self.assertEqual(result.trajectory, result.baseline_trajectory)

    def test_bad_inputs_fail_closed_without_nonfinite_payloads(self):
        for command in (object(), Command("bad", 0, 1), Command("coil", 0, 2),
                        Command("coil", True, 1), Command("register", 0, 1.2),
                        Command("register", 0, True), Command("register", 0, 65536),
                        Command("register", 100, 600), Command("register", 101, 100)):
            with self.subTest(command=command):
                self.assert_invalid(predict(PlantState(), command))
        for horizon in (-1, float("nan"), float("inf"), True, "120", 1441):
            with self.subTest(horizon=horizon):
                self.assert_invalid(predict(PlantState(), None, horizon_min=horizon))
        for state in (None, {}, replace(PlantState(), level_pct=float("nan")),
                      replace(PlantState(), chlorine_mgl=1e308),
                      replace(PlantState(), pump_on="false")):
            with self.subTest(state=state):
                self.assert_invalid(predict(state, None))

    def assert_invalid(self, result):
        self.assertFalse(result.safe)
        self.assertFalse(result.assessment_complete)
        self.assertEqual(result.trajectory, [])
        self.assertEqual(result.violations[0].code, "SETPOINT_OUT_OF_RANGE")
        self.assertEqual(result.violations[0].minutes_until, 0)

    def test_baseline_attribution_preserves_accelerated_crossing(self):
        candidate = Violation("CHLORINE_HIGH", 20, 4.1, 4, "Candidate crossing")
        baseline = replace(candidate, minutes_until=60)
        attributed = _attribute_violations([candidate], [baseline])[0]
        self.assertTrue(attributed.pre_existing)
        self.assertEqual(attributed.minutes_until, 20)
        self.assertEqual(attributed.baseline_minutes_until, 60)
        self.assertFalse(_attribute_violations([candidate], [])[0].pre_existing)

    def test_rollout_retains_first_crossing_and_baseline_attribution(self):
        existing = predict(PlantState(chlorine_mgl=5), None, horizon_min=1)
        self.assertEqual(len(existing.violations), 1)
        self.assertEqual(existing.violations[0].minutes_until, 0)
        self.assertTrue(existing.violations[0].pre_existing)
        state = PlantState(level_pct=40, auto_mode=False)
        caused = predict(state, Command("register", 0, 100))
        self.assertEqual(len(caused.violations), 1)
        self.assertGreater(caused.violations[0].minutes_until, 0)
        self.assertFalse(caused.violations[0].pre_existing)
