"""Engineering-unit limits for the currently implemented demo checks."""

# Source audit 2026-11-15: EPA chlorine MRDL benchmark, 4.0 mg/L as Cl2:
# https://www.ecfr.gov/current/title-40/chapter-I/subchapter-D/part-141/subpart-G/section-141.65
# Instantaneous tank threshold is our demo guard, not a regulatory compliance test.
# Compliance averaging context (running annual average of monthly averages):
# https://www.oregon.gov/oha/ph/healthyenvironments/drinkingwater/monitoring/pages/mon-mrdl.aspx
CHLORINE_HIGH_MGL = 4.0

# Demo assumption: 5% nominal headroom; actual spill elevation is site-specific.
# EPA storage guidance supports controls, not this 95% value (research.md step 11).
TANK_OVERFLOW_PCT = 95.0

# Normal envelope from shared/registers.yaml, decoded into engineering units.
# Tests verify these bounds against the shared file; no external YAML dependency.
SETPOINT_BOUNDS = {
    "doser_rate_pct": (0.0, 100.0, "%"),
    "level_high_sp": (50.0, 95.0, "%"),
    "level_low_sp": (20.0, 70.0, "%"),
    "chlorine_target_mgl": (0.2, 4.0, "mg/L"),
}

# Benchmark: 40 CFR 141.72(a)(3)/(b)(2), covered surface-water/GWUDI systems,
# distribution-entry residual with a four-hour provision. Our immediate tank
# guard does not implement those rules or certify disinfection adequacy.
# https://www.ecfr.gov/current/title-40/chapter-I/subchapter-D/part-141/subpart-H/section-141.72
CHLORINE_LOW_MGL = 0.2
# Demo low-storage proxy only: no pressure [psi] calculated or universal 15% rule.
# EPA sanitary-survey guide ch. 10: elevations/demand/pressure limits are site-specific.
# https://www.epa.gov/sites/default/files/2019-08/documents/sanitary_survey_learners_guide_508_8.27.19.pdf
PRESSURE_LOSS_PCT = 15.0
# Unvalidated demo assumption; require actual motor/starter duty limits.
# Example only: Franklin 12-inch rewindable motor specifies 5 starts/hour,
# so our 6 would NOT adequately protect that motor. See research.md step 11.
# https://www.franklinwater.com/products/submersible-motors-and-control-boxes/12-inch-submersible-motors/12-inch-rewindable-motor/
MAX_PUMP_STARTS_PER_HOUR = 6
PUMP_START_WINDOW_S = 3600.0

CHECKED_LIMITS = (
    "CHLORINE_HIGH", "CHLORINE_LOW", "TANK_OVERFLOW", "PRESSURE_LOSS",
    "PUMP_SHORT_CYCLE", "SETPOINT_OUT_OF_RANGE",
)
