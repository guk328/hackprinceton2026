# Physics assumptions and evidence log

## Initial model — provisional, not field-validated

All starting numbers below come from `docs/prompts/PROMPT_ME.md`, not a verified
real installation. Primary-source validation is scheduled in ME plan step 11.

| Quantity | Initial value | Evidence status |
|---|---|---|
| Full tank capacity | 400 m³ | Demo assumption |
| Pump flow while on | 60 m³/h | Constant-flow demo assumption |
| Maximum town demand | 45 m³/h | Demo assumption; demand scales linearly with knob [%] |
| Maximum chlorine dose | 600 g/h | Demo assumption; linear output with rate [%] |
| Chlorine decay coefficient | 0.05 h⁻¹ | First-order demo assumption |
| Initial level / chlorine / demand | 60% / 1 mg/L / 40% | Demo reset state |
| Initial dose / low SP / high SP | 10% / 40% / 85% | Shared defaults, expressed in engineering units |

## Balances and units

- Volume V [m³] = level [%] / 100 × 400 m³.
- Inflow Qp [m³/h] = 60 when pump on, otherwise 0; demand Qd [m³/h] = 45 × demand [%] / 100.
- Chlorine concentration C [mg/L] has the same numeric value in g/m³; dissolved mass M [g] = V × C.
- Dose D [g/h] = 600 × rate [%] / 100 only when both pump and doser run.
- Water: dV/dt = Qp − Qd [m³/h].
- Chlorine: dM/dt = D − Qd × C − k × M [g/h], with k = 0.05 h⁻¹.
- Euler update: Δt [h] = dt_s / 3600; update V and M, then C = M/V.

The prompt writes V × dC/dt with an outflow term. With changing volume,
d(VC)/dt = V × dC/dt + C × dV/dt, so that expression misses the volume term.
The implementation conserves mass instead. For V > 0, the equivalent concentration
equation is V × dC/dt = D − Qp × C − k × V × C.
Incoming pump water is assumed to contain zero chlorine before our doser.

## Control and numerical boundaries

Auto control turns the pump on strictly below the low setpoint and off strictly
above the high setpoint; between them (including equality) it keeps its prior state.
Manual mode keeps the requested pump state. No dose reaches the tank with the pump off.
Actual outflow is capped by stored volume plus inflow for the step; a dry tank
reports 0% level and 0 mg/L, not a division error or fabricated stored chlorine.
These dry-tank values remain low-level evidence for the later limit detector.
Levels above 100% are retained as extrapolated overflow evidence; this model does
not simulate a spillway, pipe pressure, or post-overflow water routing.
Use short Euler steps (1–10 s); this is a well-mixed approximation, not a water-quality certification.

## Step 1 hand calculation

At V = 240 m³, C = 1 mg/L, demand = 18 m³/h, pump = 60 m³/h,
and dose = 60 g/h, a one-second step gives:

- V_next = 240 + (60 − 18) / 3600 = 240.011666667 m³.
- M_next = 240 + (60 − 18 − 0.05 × 240) / 3600 = 240.008333333 g.
- Level_next = 60.002916667%; C_next ≈ 0.999986112 mg/L.

The starting dose does not exactly maintain 1 mg/L once decay is included.
Tests in `physics/tests/test_model.py` compare the implementation with these
independent balances, control boundaries, pump-off dosing, and empty-tank cases.

## Step 2 normal-day sanity check

Run from the repository root with the existing workspace virtual environment:

```sh
../.venv/bin/python -m physics.plot_normal_day
```

The optional plotting version is pinned in `physics/requirements-plot.txt`:
matplotlib 3.9.4 was already installed; no packages were installed or upgraded.
The public physics imports remain independent of matplotlib.

The script simulates 24 plant-hours in 8,640 steps of 10 s, using the initial
state above, fixed 40% demand, 10% dose and automatic pump control. Output:
[normal-day plot](plots/normal_day.png), with time [h], level [%], chlorine
[mg/L] and pump ON/OFF. This is a constant-demand sanity case, not a measured
daily demand profile or a field-validation result.

| Result | Observed value |
|---|---|
| Level range | 40.000000–85.025000% |
| Chlorine range | 0.518907–1.000000 mg/L |
| First pump stop | 2.383333 plant-h |
| Pump restart | 12.388889 plant-h |
| Second pump stop | 16.675000 plant-h |
| Final minus initial volume minus integrated net flow | −6.753e−11 m³ |
| Final minus initial chlorine mass minus integrated net mass input | −8.413e−12 g |

Independent checks passed: pump-on slope +10.5 percentage-points/h and off
slope −4.5 points/h throughout; first pump stop within 10 s of the analytical
(85−60)/10.5 = 2.380952 h; all states finite and away from an empty tank.
During the first pump-off interval the analytical concentration follows
C(t)=C_start·exp(−k·t), because well-mixed withdrawal removes water and chlorine
proportionally. Euler gives 0.551595624 mg/L at the interval's end versus
0.551672578 mg/L analytically (absolute difference <0.0001 mg/L).

The small level overshoot above 85% is due to discrete control sampling.
Chlorine falls while the pump/doser are off and rises during the second fill;
the fixed 10% dose does not hold the 1 mg/L starting concentration. Balance
residuals verify numerical bookkeeping, not the real-world parameter choices.

## Step 3 command and baseline forecasts

`from physics import PlantState, Command, predict, step` now works. Prediction
applies one raw command to a copied state and runs the same equations twice:
candidate versus unchanged baseline. Neither input is mutated; no network,
filesystem, wall clock, or external library is used in the forecast.

Coils 0–4 map to pump, doser, automatic mode, safe mode and alarm. Holding
registers 0–4 map to dose rate [%], high/low level setpoints [%], chlorine target
[mg/L] and logic version. Decode setpoints by dividing raw values by 10 and
chlorine target by 100. Target, version and alarm are stored configuration;
they do not invent a target-tracking dosing controller. Twin-only registers
100/101 and unknown addresses are rejected. Tests check names/addresses/scales
against `shared/registers.yaml` without adding a YAML dependency to physics.

SAFE_MODE uses local 40/85% setpoints and forces the doser off, matching the
software station. Pump/doser/auto and holding 0–4 writes are ignored while safe
mode is active; safe-mode/alarm coils remain available, as in the station.
New state fields have defaults, so the README's original constructor still works.

Each rollout includes t=0, advances in steps of at most 10 s and ends exactly
at the requested horizon (a shorter final step if needed). Default 120 min
means 720 steps and 721 points per trajectory. `baseline_trajectory` is an
additional result field; both trajectories use `(plant-min, level %, mg/L)`.
Horizon is bounded to 0–1440 plant-min to cap work/memory. Invalid inputs and
numerical overflow return `safe=False` with SETPOINT_OUT_OF_RANGE at t=0;
value/limit placeholders of 0 in that input-error result are not physical limits.

**Step 3 snapshot (superseded by step 4 below):** Before the physical detectors,
valid forecasts also returned `safe=False`,
`assessment_complete=False`, and an empty violations list. An empty list must
not be interpreted as approval. The limit-check hook retains the first crossing
per code. Baseline attribution is tested using a test-only detector: a same-code
baseline hazard sets `pre_existing=True`, while candidate crossing time and
`baseline_minutes_until` remain separate so accelerated harm is still visible.

Verification: 14 standard-library physics tests pass (6 model + 8 forecast tests),
including bad inputs, exact/partial horizons, pure repeatability, safe-mode
behavior, wire scaling and baseline attribution. No dependencies were added.

Printed example: start at 60%, manual pump on, demand 40%; request pump off.
At 1 plant-min the candidate is 59.925% versus 60.175% for the baseline;
the original state remains 60% with its pump on. A paired 120-minute dosing
forecast measured median 13.57 ms, slowest batch average 14.69 ms (5 batches
of 5 calls on the local Python 3.9 environment, without real detectors).
The 24-hour normal-day volume/mass residuals remain below 1e−8 m³/g and the
level envelope remains within the previously documented range.

## Step 4 Friday-night physics handoff

The public imports and function signatures are unchanged. The predictor now
implements CHLORINE_HIGH (>4.0 mg/L), TANK_OVERFLOW (>95%) and the shared
setpoint envelope (including low < high). Constants and source comments live
in `physics/limits.py`; no shared or teammate-owned files were changed.

Threshold evidence, checked November 13, 2026:

- [EPA primary regulations](https://www.epa.gov/ground-water-and-drinking-water/national-primary-drinking-water-regulations) list chlorine MRDL 4.0 mg/L as Cl2. This project's instantaneous tank guard is a demo design choice, not a complete compliance calculation.
- [Oregon Health Authority](https://www.oregon.gov/oha/ph/healthyenvironments/drinkingwater/monitoring/pages/mon-mrdl.aspx) explains the running annual average of monthly averages used for compliance. Do not claim one instantaneous model crossing itself proves a regulatory violation.
- The 95% guard reserves 5% tank headroom per PROMPT_ME.md; it is an unvalidated design assumption, not a universal overflow height. Setpoint ranges come from shared/registers.yaml and are contract-tested.

Valid forecasts set `assessment_complete=True`; `safe=True` means no candidate
violation among `checked_limits`: CHLORINE_HIGH, TANK_OVERFLOW and
SETPOINT_OUT_OF_RANGE. Pressure loss, low chlorine and short cycling are still
step 5 work. This metadata makes the milestone's limited coverage explicit.
Invalid inputs/numerical failures remain unsafe with assessment_complete=False.

Both rollouts check t=0 and every sample thereafter. Exact equality is not a
crossing; minutes_until is the first sample strictly above the threshold, with
at most 10 s sampling resolution relative to the simulated crossing. The value
is the actual sampled concentration [mg/L] or level [%], and limit is 4 or 95.
Only the first crossing per code is returned. Pre-existing hazards remain unsafe;
candidate and baseline times are both retained when a command accelerates harm.
Numeric out-of-envelope level setpoints still get physical forecasts, so a 100%
high setpoint can report both immediate misconfiguration and later overflow.

Reproducible 120 plant-min fixtures (demand 40%, pump 60 m³/h, tank 400 m³):

| Case | Initial conditions / command | Expected result |
|---|---|---|
| Overdose | Level 40%, C=1 mg/L, manual pump+doser on, dose 10→100% | CHLORINE_HIGH at 80.667 min; sampled 4.001457 mg/L; not pre-existing |
| Overflow | Level 90%, manual pump off, then pump coil 0→1 | TANK_OVERFLOW at 28.667 min; sampled 95.016667%; not pre-existing |
| Invalid high SP | Level 90%, auto mode/pump on, high SP raw 850→1000 | SETPOINT_OUT_OF_RANGE at 0 min, then TANK_OVERFLOW at 28.667 min |
| Safe change | Default state, high SP raw 850→840 | safe=True, no implemented-limit violations |

Overflow hand check: (95−90) / 10.5 percentage-points/h × 60 = 28.571 min;
the first sampled crossing differs by less than 10 s. The README's 60%-full
starting example reaches only 3.911214 mg/L by 120 min at maximum dose with
these parameters, so it does not exercise CHLORINE_HIGH within that horizon.
Use the 40%-full fixture for the handoff. Do not claim the illustrative 11.2 min
or the desired 10–20 min timing; defensible tuning remains plan step 14.

CompE: run from the repository root, using any Python 3.9+ interpreter (no new dependencies):

```sh
python3 -m unittest discover -s physics/tests -p test_milestone.py -q
```

```python
from physics import PlantState, Command, predict, step
state = PlantState(level_pct=40.0, auto_mode=False)
p = predict(state, Command(kind="register", address=0, value=100))
print(p.safe, p.checked_limits)
print([(v.code, v.minutes_until, v.value, v.limit, v.pre_existing) for v in p.violations])
```

All 21 standard-library physics tests pass, including real baseline attribution,
earlier candidate harm, strict boundaries, multiple hazards, purity, JSON-safe
results, malformed inputs and the shared register bounds. No dependencies added.
Forty individual paired 120-min forecasts across overdose/overflow/invalid-SP/safe
fixtures measured median 14.01 ms and maximum 14.79 ms locally. Timing is a local
measurement, not a hard real-time guarantee or a benchmark of future checks.


## Step 5 — remaining demo guards

All six codes now run on the candidate and baseline forecasts. Strict lower
bounds are chlorine <0.2 mg/L and tank level <15%; equality is allowed.
These are provisional demo guards. Tank level is a pressure proxy, not a
hydraulic pressure calculation; source applicability remains research step 11.
The shared register envelopes and low-setpoint < high-setpoint rule still apply.
Invalid doser rates fail closed without attempting a physical trajectory.

Pump history uses optional `elapsed_s` [plant-s] and immutable
`pump_start_times_s` [plant-s] fields. Starts are counted in (now−3600, now];
more than six triggers PUMP_SHORT_CYCLE. Manual OFF→ON writes and local
AUTO/SAFE_MODE OFF→ON transitions record a start. Already-running pumps do
not create one. Step advances plant time and removes expired history. Callers
must carry these fields forward: default () means no known previous starts,
not proof the real motor had none. Malformed, future or unsorted history fails
closed. The six-start limit is a motor-protection design assumption awaiting
verification for the selected motor.

SAFE_MODE retains doser-off behavior, local 40/85% pump hysteresis, and ignored
remote pump/doser/auto/register writes; mode/alarm writes remain available.
Auto hysteresis keeps the preceding pump state at exact equality and between
setpoints. Safe mode cannot prevent eventual low chlorine with the doser off;
the predictor still reports that hazard.

Verification: 21 existing physics tests passed; focused checks cover equality,
lower crossings, seventh start, rolling expiry, manual/automatic history,
malformed histories, missing default history, configuration envelopes and
SAFE_MODE. No dependencies added (installed pytest 7.4.4).
