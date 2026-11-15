# Physics assumptions and evidence log

## Initial model — provisional, not field-validated

All starting numbers below come from `docs/prompts/PROMPT_ME.md`, not a verified real installation. The Step 11 audit below records sources and remaining site-validation gaps; these initial values remain demo assumptions.

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

The prompt writes V × dC/dt with an outflow term. With changing volume, d(VC)/dt = V × dC/dt + C × dV/dt, so that expression misses the volume term.
The implementation conserves mass instead. For V > 0, the equivalent concentration
equation is V × dC/dt = D − Qp × C − k × V × C.
Incoming pump water is assumed to contain zero chlorine before our doser.
## Control and numerical boundaries

Auto control turns the pump on strictly below the low setpoint and off strictly above the high setpoint; between them (including equality) it keeps its prior state.
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
step 5 work in this historical milestone snapshot. Step 5 below supersedes
that coverage limitation.
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

## Step 11 — primary-source audit (checked 2026-11-15, ET)

This section supersedes earlier statements that source checking is pending.
The research audit is complete; the plant is still a demonstrator, not a
calibrated installation. Sources support the modelling approach and some
benchmarks, not the accuracy of every parameter or forecast. No runtime
constants were changed during this audit.

### Six guards: what the numbers actually mean

| Guard / current value | Evidence and applicability | Classification / deployment requirement |
|---|---|---|
| CHLORINE_HIGH: >4.0 mg/L | [40 CFR 141.65(a)](https://www.ecfr.gov/current/title-40/chapter-I/subchapter-D/part-141/subpart-G/section-141.65) lists chlorine MRDL at 4.0 mg/L as Cl2. [Oregon's regulator](https://www.oregon.gov/oha/ph/healthyenvironments/drinkingwater/monitoring/pages/mon-mrdl.aspx) explains compliance averaging. | Regulation-derived benchmark; our instantaneous simulated tank crossing is an engineering guard, not proof of a compliance violation or a toxicity determination. |
| CHLORINE_LOW: <0.2 mg/L | [40 CFR 141.72(a)(3), (b)(2)](https://www.ecfr.gov/current/title-40/chapter-I/subchapter-D/part-141/subpart-H/section-141.72) addresses residual entering distribution for covered surface-water / groundwater-under-direct-influence systems, with a four-hour provision. It also has separate distribution residual requirements. | Context-specific benchmark. An immediate tank threshold does not reproduce those provisions, establish universal adequacy of disinfection, or automatically apply to every groundwater system. Contact time, treatment and state requirements matter. |
| TANK_OVERFLOW: >95% | [EPA sanitary survey guide, chapter 10](https://www.epa.gov/sites/default/files/2019-08/documents/sanitary_survey_learners_guide_508_8.27.19.pdf) discusses overflow hardware and level controls; it does not establish our percentage. | Demo guard reserving 5% nominal headroom. Actual overflow depends on tank geometry and overflow elevation. Do not say physical spilling necessarily begins at 95%. |
| PRESSURE_LOSS: <15% | The same [EPA guide, chapter 10, pp. 10-10–10-12](https://www.epa.gov/sites/default/files/2019-08/documents/sanitary_survey_learners_guide_508_8.27.19.pdf) calls for evaluating elevation, demand and applicable pressure requirements. | Low-storage proxy, not calculated pressure. Need tank/customer elevations, level-depth geometry and network losses to establish a site-specific pressure limit. No universal 15% pressure rule verified. |
| PUMP_SHORT_CYCLE: >6 starts in (now−3600 s, now] | [Franklin Electric's 12-inch rewindable motor](https://www.franklinwater.com/products/submersible-motors-and-control-boxes/12-inch-submersible-motors/12-inch-rewindable-motor/) specifies five maximum starts/hour for that product. This is an example of equipment-specific requirements, not our selected pump. | Six is an unvalidated demo assumption, not a universal motor limit; it would not protect that five-start motor adequately. Replace with the actual motor/starter duty limits. The fan does not validate water-pump protection. |
| SETPOINT_OUT_OF_RANGE | `shared/registers.yaml` specifies rate 0–100%, high SP 50–95%, low SP 20–70%, chlorine target 0.20–4.00 mg/L. Low SP < high SP is our controller consistency rule. | Project operating envelope. These settings are not independently established regulatory operating ranges for a real station. |

All guards use strict comparisons where specified; equality alone does not
cross a concentration/level threshold. A setpoint pair with low >= high is
invalid. EPA's guide is engineering guidance, not itself a regulation. Its
support for level alarms and pump-cycle management does not validate our
chosen 95%, 15%, or six-start numbers.

For MRDL detail, the official published [40 CFR 141.133(c)(1), 2024 edition](https://www.govinfo.gov/content/pkg/CFR-2024-title40-vol25/pdf/CFR-2024-title40-vol25-part141.pdf)
uses a running annual arithmetic average, computed quarterly, of monthly
averages for chlorine/chloramines. The current eCFR 141.133 page was access-blocked
during this audit; current state-agency guidance above independently confirms
the averaging distinction. Do not treat the older published edition as a check
of every subsequent amendment.

### Plant parameters: assumptions and evidence needed

The [EPA EPANET 2.2 manual, sections 3.4 and 12.2](https://nepis.epa.gov/Exe/ZyPURL.cgi?Dockey=P10113EM.TXT)
supports completely mixed storage mass balances and first-order chlorine decay
as modelling choices. It describes estimating bulk reaction coefficients from
bottle tests and notes temperature dependence. It does not validate our
specific tank or rate constants. Its decay coefficient uses a negative sign
for loss; our positive k appears in the negative loss term. This is a sign
convention, not a disagreement in the decay model.

| Parameter | Current assumption | Status / how to replace it |
|---|---|---|
| Capacity | 400 m³ | Not sourced to a real installation. Obtain tank drawings and usable volume versus water elevation; our percent-to-volume relation assumes constant cross-section. |
| Pump delivery | 60 m³/h, constant whenever on | Not measured or tied to a pump curve. Obtain actual pump performance and operating head or flow measurements. |
| Maximum town demand | 45 m³/h, linearly scaled by knob | Not a population-derived design demand or measured peak. Obtain a metered demand profile and appropriate peak/fire requirements. |
| Maximum chlorine mass delivery | 600 g/h as chlorine; linear with command percentage | Not tied to a selected dosing unit. Need pump calibration, chemical strength/density and actual flow; device liquid delivery must be converted to chlorine mass. |
| Decay coefficient | k=0.05 h⁻¹ | Not calibrated. Fit representative water-quality data at relevant water temperatures. Our model omits separate pipe-wall demand and spatial mixing effects. Cabinet DHT11 readings do not establish water temperature. |
| Incoming chlorine | 0 mg/L before the doser | Simplification; obtain source-water residual to justify or replace it. |
| Reset | level 60%, chlorine 1 mg/L, demand 40%, dose 10% | Chosen initial conditions, not field observations. Step 9's 40%-full fixture is a separate documented starting condition. |

Independent consequences of the chosen numbers (derived here, not measured):

- Demand at 40% is 18 m³/h; a full 400 m³ would supply 22.22 h at that fixed
  withdrawal with no inflow, ignoring reserve requirements.
- Storage between 40% and 85% is 180 m³; it supplies 10 h at 18 m³/h.
- k=0.05 h⁻¹ implies an isolated no-flow chlorine half-life ln(2)/k=13.86 h.
  In EPANET's first-order convention the equivalent bulk coefficient is −1.2 day⁻¹.
- At full pump flow, max dosing is 600/60=10 g/m³ = 10 mg/L added to the
  incoming stream before tank dilution/decay. At 10% output it is 1 mg/L.
- At V=240 m³, raising concentration from 1 to 4 mg/L requires 720 g even if
  volume were fixed and there were no losses; 600 g/h needs 72 min for that
  mass alone. Actual changing-volume forecasts must use the model.

These calculations check internal consistency. They do not establish a
particular town's capacity, dosing equipment or regulatory compliance. Keep the
current values for reproducible demos; field calibration and any later tuning
remain separate tasks. Do not invent a real town or attach these parameters to
an unrelated vendor installation.

### Incidents and security guidance

**Verified primary incident source:** the [FBI/EPA PSA dated July 30, 2026](https://www.fbi.gov/investigate/cyber/alerts/2026/malicious-cyber-actors-targeting-water-and-wastewater-sector-internet--facing-programmable-logic-controllers-causing-operational-disruptions)
reports incidents since July 27 in at least seven states. Attackers changed
exposed PLC addresses/passwords; reported consequences included lost monitoring
or control, pressure loss and flooding. It recommends mediated remote access,
restricted communications, unique credentials, logging and tested manual
operations. This supports the operational-disruption motivation; it is not
evidence that a chlorine overdose occurred or that Backflow would have
prevented those incidents.

**Count caution:** the README's “over 100” July claim appears in secondary
reporting attributed to CISA's [Internet Exposure Reduction Guidance](https://www.cisa.gov/resources-tools/resources/exposure-reduction).
The original page returned HTTP 403 in this audit. Its numerical claim was
not independently verified from accessible primary text. Prefer the verified
seven-state wording above until the team obtains the original publication.
Do not convert targeted systems into confirmed contaminated supplies or assume
all affected utilities were small towns with one operator.

**Agency guidance:** the joint [CISA/EPA/FBI Top Cyber Actions fact sheet](https://www.cisa.gov/sites/default/files/2024-03/fact-sheet-top-cyber-actions-for-securing-water-systems.pdf)
is internally dated February 23, 2024. It prioritizes exposure reduction,
assessments, password changes, inventories, incident/recovery preparation,
backups, vulnerability reduction and training. It supports layered security;
it does not endorse our product or substitute our simulation for those controls.

**Integrator role:** [CISA/FBI's September 23, 2026 agency bulletin](https://content.govdelivery.com/accounts/USDHSCISA/bulletins/42c2c98)
describes third-party ICS integrator work including design, installation,
support and operational services. It emphasizes least privilege, monitored
access and manual-operation preparation. Selling through water-system controls
integrators is a plausible channel hypothesis, not a validated customer or
partner relationship. No contact was made during this research task.

### Market size and pricing: precise claims, not a revenue forecast

[EPA's November 2024 report to Congress, executive summary and section “System Size”](https://www.epa.gov/system/files/documents/2025-01/final_508_epa_reporttocongress_11-19-24_0.pdf)
uses FY2021 data and includes 49,738 community water systems; 91% serve 10,000
or fewer people. These are historical CWS counts, not a November 2026 inventory,
not all public-water-system types, and not necessarily buyers of our gateway.
A safe website sentence is: “EPA's 2024 report, using FY2021 data, found that
91% of community water systems served 10,000 or fewer people.” The README's
unqualified 81% of all water systems should not be used without specifying its
denominator, size cutoff and data vintage.

As of this audit, the manufacturer's [Sensaphone Sentinel cellular product page](https://sensaphone.com/products/sensaphone-sentinel-monitoring-system-with-cellular-modem)
advertises USD $24.95/month for hosting, cellular connection and alerts. This
is one published recurring-service example, not an average utility budget or
measured willingness to pay for command screening. Hardware, installation,
sensors, commissioning, taxes and site-specific integration are separate costs.

The seller's own [Process Control Services monthly-service listing](https://www.processcontrolservicesllc.com/store/p/1-month-cellular-cloud-service)
shows $31.19 for one month; its [Sentinel hardware listing](https://www.processcontrolservicesllc.com/store/p/sensaphone-sentinel-monitoring-system-with-cellular-modem)
shows $1,951.87. These are separate observed retail listings, not an installed
project quote. Differences in channel/package mean the advertised prices must
not be treated as a single universal tariff. Neither product listing establishes
authorization to insert our gateway or a comparable physics-enforcement feature.

Our product price, installation effort, annual support cost, accessible market,
buying authority and savings remain unknown. A hardware purchase plus a monthly
monitoring subscription is evidence of an existing product category, not proof
of demand for Backflow. Operator/integrator interviews are step 12.

### Website / judge wording and remaining validation

Use: “We demonstrate a simplified water-system model that forecasts commands
against explicit engineering guards. Two chlorine benchmarks are informed by
published requirements; storage and cycling guards are configurable demo
assumptions. Site-specific deployment needs calibrated parameters and verified
controls.”

Avoid claims of certified water safety, exact field crossing times, universal
15% pressure loss, universal six-start motor protection, or agency endorsement.
Our small local tests establish numerical/enforcement behavior under fixtures,
not production reliability. A gateway reachable directly around the proxy is
not a protected deployment; prevent bypass in a real network design.

Research deliverable complete: every model input/guard is classified, primary
references and access limitations are recorded, and price/count claims have
units, scope and dates. Outstanding **field validation**, not completed here:
obtain an actual installation's drawings and flow/dose curves, measure chlorine
response, agree on jurisdiction-specific operational limits, validate motor
history collection end-to-end, and get operator feedback. Step 11 completion
does not mean those tasks or hardware Step 8 have been completed.
