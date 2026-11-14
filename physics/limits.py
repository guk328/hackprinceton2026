"""Engineering-unit limits for the currently implemented demo checks."""

# EPA chlorine MRDL benchmark, 4.0 mg/L as Cl2:
# https://www.epa.gov/ground-water-and-drinking-water/national-primary-drinking-water-regulations
# Instantaneous tank threshold is our demo guard, not a regulatory compliance test.
# Compliance averaging context (running annual average of monthly averages):
# https://www.oregon.gov/oha/ph/healthyenvironments/drinkingwater/monitoring/pages/mon-mrdl.aspx
CHLORINE_HIGH_MGL = 4.0

# PROMPT_ME.md design assumption: reserve 5% tank headroom, not a universal limit.
TANK_OVERFLOW_PCT = 95.0

# Normal envelope from shared/registers.yaml, decoded into engineering units.
# Tests verify these bounds against the shared file; no external YAML dependency.
SETPOINT_BOUNDS = {
    "doser_rate_pct": (0.0, 100.0, "%"),
    "level_high_sp": (50.0, 95.0, "%"),
    "level_low_sp": (20.0, 70.0, "%"),
    "chlorine_target_mgl": (0.2, 4.0, "mg/L"),
}

CHECKED_LIMITS = ("CHLORINE_HIGH", "TANK_OVERFLOW", "SETPOINT_OUT_OF_RANGE")
