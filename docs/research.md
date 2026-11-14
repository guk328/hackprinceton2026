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

**This is a forecast foundation, not the completed safety checker.** Until step 4
implements the physical detectors, valid forecasts also return `safe=False`,
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
