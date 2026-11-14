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


class FullContractTests(unittest.TestCase):
    def violation(self, state, command, code, horizon=120):
        result = predict(state, command, horizon_min=horizon)
        self.assertTrue(result.assessment_complete)
        self.assertFalse(result.safe)
        matches = [v for v in result.violations if v.code == code]
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0].message)
        return matches[0]

    def test_chlorine_high(self):
        v = self.violation(PlantState(level_pct=40, auto_mode=False),
                           Command("register", 0, 100), "CHLORINE_HIGH")
        self.assertGreater(v.value, 4)
        self.assertAlmostEqual(v.minutes_until, 80.6666667, places=5)
        self.assertFalse(v.pre_existing)

    def test_tank_overflow(self):
        v = self.violation(PlantState(level_pct=90, auto_mode=False, pump_on=False),
                           Command("coil", 0, 1), "TANK_OVERFLOW")
        self.assertGreater(v.value, 95)
        self.assertLessEqual(abs(v.minutes_until - (95 - 90) / 10.5 * 60), 10 / 60)
        self.assertFalse(v.pre_existing)

    def test_chlorine_low_matches_analytical_decay(self):
        # No inflow/outflow/dose: C(t) = C0 exp(-0.05 t_h).
        from math import log
        state = PlantState(chlorine_mgl=0.21, pump_on=False, auto_mode=False,
                           doser_on=False, demand_pct=0)
        v = self.violation(state, None, "CHLORINE_LOW")
        self.assertLess(v.value, 0.2)
        self.assertLessEqual(abs(v.minutes_until - log(0.21 / 0.2) / 0.05 * 60), 10 / 60)
        self.assertTrue(v.pre_existing)

    def test_pressure_loss_matches_analytical_drain(self):
        state = PlantState(level_pct=16, demand_pct=100, auto_mode=False)
        v = self.violation(state, Command("coil", 0, 0), "PRESSURE_LOSS")
        self.assertLess(v.value, 15)
        self.assertLessEqual(abs(v.minutes_until - (16 - 15) / 11.25 * 60), 10 / 60)
        self.assertFalse(v.pre_existing)

    def test_seventh_manual_start_is_immediate_and_does_not_mutate_history(self):
        state = PlantState(auto_mode=False, pump_on=False, elapsed_s=3600,
                           pump_start_times_s=(1, 100, 200, 300, 400, 500))
        before = replace(state)
        command = Command("coil", 0, 1)
        result = predict(state, command)
        v = self.violation(state, command, "PUMP_SHORT_CYCLE")
        self.assertEqual((v.minutes_until, v.value, v.limit), (0, 7, 6))
        self.assertFalse(v.pre_existing)
        self.assertEqual(state, before)
        self.assertEqual(predict(state, command), result)

    def test_local_start_is_not_hidden_when_old_start_expires_during_step(self):
        state = PlantState(level_pct=39, pump_on=False, elapsed_s=3600,
                           pump_start_times_s=(1, 100, 200, 300, 400, 500))
        v = self.violation(state, None, "PUMP_SHORT_CYCLE", horizon=1)
        self.assertEqual((v.minutes_until, v.value), (0, 7))
        self.assertTrue(v.pre_existing)

    def test_setpoint_out_of_range_and_equal_pair(self):
        for command in (Command("register", 1, 499), Command("register", 2, 701),
                        Command("register", 3, 19), Command("register", 3, 401)):
            with self.subTest(command=command):
                v = self.violation(PlantState(), command, "SETPOINT_OUT_OF_RANGE", horizon=0)
                self.assertEqual(v.minutes_until, 0)
                self.assertFalse(v.pre_existing)
        self.violation(PlantState(level_low_sp=60, level_high_sp=60), None,
                       "SETPOINT_OUT_OF_RANGE", horizon=0)

    def test_safe_command_assesses_all_six_codes(self):
        result = predict(PlantState(), Command("register", 1, 840))
        self.assertTrue(result.safe)
        self.assertTrue(result.assessment_complete)
        self.assertEqual(result.violations, [])
        self.assertEqual(set(result.checked_limits), {
            "CHLORINE_HIGH", "CHLORINE_LOW", "TANK_OVERFLOW", "PRESSURE_LOSS",
            "PUMP_SHORT_CYCLE", "SETPOINT_OUT_OF_RANGE",
        })

    def test_strict_limits_allow_exact_equality(self):
        for level, chlorine in ((15, 0.2), (95, 4)):
            state = PlantState(level_pct=level, chlorine_mgl=chlorine,
                               elapsed_s=3600, pump_start_times_s=(1, 100, 200, 300, 400, 500))
            result = predict(state, None, horizon_min=0)
            self.assertTrue(result.assessment_complete)
            self.assertTrue(result.safe)

    def test_missing_cycle_history_preserves_old_constructor(self):
        state = PlantState(pump_on=False, auto_mode=False)
        result = predict(state, Command("coil", 0, 1), horizon_min=1)
        self.assertTrue(result.safe)
        self.assertEqual(state.pump_start_times_s, ())
        self.assertEqual(state.elapsed_s, 0)

    def test_garbage_and_nonfinite_values_fail_closed_with_json_safe_output(self):
        from dataclasses import asdict
        import json
        fields = ("level_pct", "chlorine_mgl", "demand_pct", "doser_rate_pct",
                  "level_high_sp", "level_low_sp", "chlorine_target_mgl", "elapsed_s")
        for field in fields:
            for value in (float("nan"), float("inf"), -float("inf"), "bad", None, True):
                with self.subTest(field=field, value=value):
                    result = predict(replace(PlantState(), **{field: value}), None)
                    self.assertFalse(result.safe)
                    self.assertFalse(result.assessment_complete)
                    self.assertEqual(result.trajectory, [])
                    json.dumps(asdict(result), allow_nan=False)
        for history in (None, [], (float("nan"),), (1,), (-1, -2)):
            with self.subTest(history=history):
                result = predict(replace(PlantState(), pump_start_times_s=history), None)
                self.assertFalse(result.safe)
                self.assertFalse(result.assessment_complete)

    def test_dry_tank_reports_hazards_without_nonfinite_predictions(self):
        from dataclasses import asdict
        import json
        state = PlantState(level_pct=0, pump_on=False, auto_mode=False)
        result = predict(state, None, horizon_min=1)
        self.assertTrue(result.assessment_complete)
        self.assertEqual({v.code for v in result.violations}, {"PRESSURE_LOSS", "CHLORINE_LOW"})
        self.assertTrue(all(point[1] == 0 for point in result.trajectory))
        json.dumps(asdict(result), allow_nan=False)

    def test_ten_and_five_second_crossings_agree_within_one_plant_minute(self):
        # Independent fine-step reference: uses public step(), direct strict
        # comparisons, and no predictor detector/rollout implementation.
        cases = (
            (PlantState(level_pct=40, auto_mode=False, doser_rate_pct=100),
             "CHLORINE_HIGH", "chlorine_mgl", 4, True),
            (PlantState(level_pct=90, auto_mode=False),
             "TANK_OVERFLOW", "level_pct", 95, True),
            (PlantState(chlorine_mgl=0.21, auto_mode=False, pump_on=False, demand_pct=0),
             "CHLORINE_LOW", "chlorine_mgl", 0.2, False),
            (PlantState(level_pct=16, auto_mode=False, pump_on=False, demand_pct=100),
             "PRESSURE_LOSS", "level_pct", 15, False),
        )
        for initial, code, field, limit, above in cases:
            with self.subTest(code=code):
                coarse = self.violation(initial, None, code)
                fine = initial
                for elapsed_s in range(0, 7201, 5):
                    value = getattr(fine, field)
                    if (value > limit) if above else (value < limit):
                        break
                    fine = step(fine, dt_s=5)
                else:
                    self.fail("Fine-step reference did not cross " + code)
                self.assertLessEqual(abs(coarse.minutes_until - elapsed_s / 60), 1)
