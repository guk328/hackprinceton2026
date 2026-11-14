# ME build plan
Work one step at a time; completed steps are checked. Keep README/shared contracts unchanged.
Follow the prompt's build order; reserve Saturday morning for outreach regardless of coding progress.
Use CompE's Modbus helper; do not edit teammates' folders or add dependencies without approval.
Starting plant parameters and unsourced limits are provisional until the research step verifies them.

1. [x] **State and one physical step — 90 min.** Goal: expose `PlantState`, `Command`, and `step()` with the README fields.
   - Files: `physics/__init__.py`, `physics/model.py`, `physics/tests/test_model.py`, `docs/research.md` (equation/assumption note).
   - Equations/numbers: V=(level_pct/100)·400 m³; Qd=(demand_pct/100)·45 m³/h; Qp=60 m³/h when pumping; Δt=dt_s/3600 h; dV/dt=Qp−Qd.
   - Equations/numbers: M=VC [g], C [mg/L]=[g/m³]; dM/dt=D−Qd·C−0.05 h⁻¹·M; D=600 g/h·rate/100 only when pump AND doser run. Use mass conservation: the prompt's V·dC/dt omits changing-volume effects.
   - Proof: printed hand-check of a 1 s Euler step, zero dose when pump is off, hysteresis below 40%/above 85%, and defined behavior near V=0 without hiding low-level violations.
   - Verified: 6 standard-library tests passed; printed one-second level 60.002916667% and chlorine 0.999986112 mg/L match hand calculations. pytest is not installed; no dependencies added.

2. [x] **Normal-day sanity plot — 45 min.** Goal: check level, chlorine and pump behavior before prediction.
   - Files: `physics/plot_normal_day.py`, `physics/requirements-plot.txt`, `docs/plots/normal_day.png`, `docs/research.md`.
   - Equations/numbers: start 60%, 1.0 mg/L, 40% demand (=18 m³/h), 10% dose; 24 plant-h in 10 s steps; manual pump-on slope +10.5 percentage-points/h, off slope −4.5 points/h.
   - Proof: labeled time [h], level [%], chlorine [mg/L] and pump-state plot plus printed mass-balance error; do not assume 10% dose holds 1 mg/L. Check installed matplotlib; request approval if missing.
   - Verified: existing workspace matplotlib 3.9.4 used, no installation; plot inspected; level 40–85.025%, chlorine 0.518907–1 mg/L; water/mass residuals <1e−8 m³/g; analytical slopes, first stop and pump-off decay checks passed.

3. [x] **Command and baseline rollouts — 60 min.** Goal: implement pure `predict(state, cmd, horizon_min=120)` with copied state.
   - Files: `physics/__init__.py`, `physics/predict.py`, `physics/model.py`, `physics/tests/test_predict.py`, `docs/research.md`.
   - Equations/numbers: 120 min / 10 s =720 integration steps per rollout; run candidate and cmd=None baseline; map coils 0–4/holding 0–4, setpoints ÷10 [%], target ÷100 [mg/L], rate ÷1 [%].
   - Proof: print `safe`, `violations`, `(t_min, level_pct, chlorine_mgl)` trajectory; unchanged input, deterministic output and cmd=None support; mark baseline hazards `pre_existing=True`, retaining earlier/worse command crossings.
   - Verified: 14 physics tests pass; wire mappings checked against YAML; 721 points/rollout at 120 min, median 13.57 ms locally. Baseline attribution is tested with a test-only detector; real detectors remain step 4, so `assessment_complete=False` and `safe=False` until wired in.

4. [ ] **FRIDAY-NIGHT MILESTONE — 90 min.** Goal: CompE imports `PlantState, Command, predict, step` and gets CHLORINE_HIGH and TANK_OVERFLOW.
   - Files: `physics/limits.py`, `physics/predict.py`, `physics/tests/test_milestone.py`, `docs/research.md` (fixtures).
   - Equations/numbers: chlorine >4.0 mg/L; level >95%; overdose fixture: level 40%, C=1 mg/L, manual pump+doser on, demand 40%, rate 10→100%; overflow fixture: manual pump on, level 90%, demand 40%, crossing ≈(5/10.5) h=28.6 min.
   - Proof: passing public-import smoke test, both codes with first-crossing `minutes_until`, value, limit, message, trajectory and safe flag; safe case, baseline flags, malformed input returns unsafe without raising; measure paired forecasts <20 ms locally and give CompE the import/test command.
   - Proof: flag out-of-range numeric settings while preserving their physical forecast; malformed values fail closed. README's 11.2 min is illustrative; at 60% level, overflow needs ≈200 min, outside the default horizon.

5. [ ] **Remaining limits — 60 min.** Goal: complete all six codes and auto/safe control semantics.
   - Files: `physics/limits.py`, `physics/model.py`, `physics/predict.py`, `docs/research.md`.
   - Equations/numbers: chlorine <0.2 mg/L; level <15%; >6 starts/rolling plant-hour; high SP 50–95%, low SP 20–70%, target 0.20–4.00 mg/L, rate 0–100%; low SP < high SP.
   - Proof: printed boundary cases and SETPOINT_OUT_OF_RANGE results; optional/defaulted timestamped pump-start history preserves existing constructors; test history handling and SAFE_MODE (doser off, local pump control). Put source/assumption comments in the single limits file.

6. [ ] **Focused physics tests — 60 min.** Goal: verify the full physics contract before networking.
   - Files: `physics/tests/test_predict.py`, `physics/tests/test_model.py`.
   - Equations/numbers: one test per 6 violation codes, one safe-command test and one garbage-input test; compare 10 s/5 s integration with crossing times within 1 plant-min; equality alone does not cross strict limits.
   - Proof: relevant `pytest -x -q` passes, including nonmutation, baseline attribution, zero volume, nonfinite values, missing cycle history, hysteresis, safe mode and register scaling; check installed pytest before use, request approval if missing.

7. [ ] **Twin on software station — 90 min.** Goal: run the same model through CompE's read/write helper.
   - Files: `twin/run.py`, `twin/tests/test_run.py`, `docs/demo.md`.
   - Equations/numbers: direct 127.0.0.1:5020, unit 1, reads every 1 wall-s; demand input 0 ÷10 [%]; --speedup 60 gives 60 plant-s/tick, integrated in ≤10 s substeps; write only holding 100=round(level·10), 101=round(C·100), unsigned 16-bit.
   - Proof: fake-helper test and printed live readback; --reset restores 60%/1 mg/L (raw 600/100); invalid reads/disconnects produce no guessed writes. Request helper from CompE and inspect installed pymodbus API rather than writing networking from scratch.

8. [ ] **ESP32 twin and rig with EE — 60 min.** Goal: repeat the loop on hardware and make outputs visible.
   - Files: `twin/run.py` (configuration if needed), `docs/demo.md` (rig/reset checklist).
   - Equations/numbers: direct STATION_HOST:502, unit 1; 1 wall-s updates, 60× plant time; label PUMP, DOSER, LEVEL [%], DEMAND [%], cabinet sensor; optional 5 V pump loop.
   - Proof: OLED matches readback, knob changes demand, relay outputs follow control; photograph labeled rig with tray and raised/dry electronics or labeled fan; demonstrate software fallback if hardware unavailable.

9. [ ] **Normal day and headline attack — 60 min.** Goal: reproduce scenarios 0 and 1 through our gateway.
   - Files: `attacks/normal_day.py`, `attacks/chlorine_overdose.py`, `docs/demo.md`.
   - Equations/numbers: gateway :1502, unit 1; #0 pump coil 0→1 plus a small valid SP change; #1 holding 0→100 [%], doser coil 1→1; record predicted crossing of 4 mg/L.
   - Proof: print each request and response; normal ALLOW/relay click, overdose HOLD/Modbus exception with no forwarded change; save curve/event evidence. Test the full command sequence only on our own ESP32/software station.

10. [ ] **Scenarios 2–5 — 90 min.** Goal: prepare drain, overflow, tamper and operator-on-site judge demonstrations.
    - Files: `attacks/drain_town.py`, `attacks/overflow.py`, `attacks/logic_tamper.py`, `attacks/operator_on_site.py`, `docs/demo.md`.
    - Equations/numbers: #2 auto coil 2→0, pump 0→0 at 100% demand (60→15% takes 240 min: use a documented near-low fixture); #3 high SP raw 1000=100%, pump 0→1, near-high fixture; #4 CRC16 mismatch; #5 smaller dose <100% with presence/cabinet asserted.
    - Proof: log HOLD predictions, mismatch/revert and approval/release. Ordinary scripts target :1502; request owner fixtures for #4's authorized behind-gateway tamper and #5's read-only sensors/policy hold. Distinguish immediate setpoint rejection from overflow forecast; agent approval stays CompE-owned.

11. [ ] **Primary-source research — 60 min.** Goal: substantiate physical limits and product claims.
    - Files: `docs/research.md`, `physics/limits.py` (source comments only).
    - Equations/numbers: verify applicability of 4.0/0.2 mg/L, 95%/15%, 6 starts/h; source 400 m³, 60/45 m³/h, 600 g/h, k=0.05 h⁻¹; investigate system counts and remote-monitoring price [USD/station/month].
    - Proof: dated primary links with units, context and one-line takeaways; verify claimed 2026 incidents against agency advisories; label design assumptions, estimates and unknowns rather than inventing statistics or citations.

12. [ ] **SATURDAY MORNING: operator outreach — 60 min, NON-CODE.** Goal: attempt a real operator/integrator conversation for product evidence and Peri.
    - Files: `docs/research.md` (draft, contact log, consented evidence links); retain original screenshots/notes privately and share redacted copies.
    - Numbers: 1 real person, 3 questions, 5 min requested; record contact/reply timestamps in ET. Schedule this morning task independently of preceding coding completion.
    - Draft: “Hi [Name], I'm an ME student at HackPrinceton building a prototype that flags pump/chlorine commands that could harm a water system. Could you spare 5 minutes today for three workflow questions? No credentials or sensitive details needed. May we retain your reply and quote your name/role for our demo and Peri entry?”
    - Questions: “How do you reach your PLCs remotely?” “Who would you call if a setpoint changed at 3am?” “What would you want a text to say?”
    - Proof: name, role/organization, channel, timestamp, actual quote/reply or screenshot, and permission; redact personal/sensitive details publicly. Prepare the message for the user to send; log unsuccessful attempts honestly and verify Peri requirements before claiming eligibility.

13. [ ] **Scenario story and timed demo — 90 min.** Goal: prepare defensible explanations and judge Q&A.
    - Files: `docs/demo.md`, `docs/research.md`.
    - Numbers: 120 s pitch with 20/25/30/25/20 s beats; each scenario includes attacker intent, unprotected outcome [%, mg/L, plant-min] and a sourced analogous incident, or explicitly no verified analogue.
    - Proof: timed rehearsal/video; answer simulated vs real, physics vs firewall, fail-closed local control, buyer evidence and constrained AI actions; separate counterfactual prediction from actual twin water so held commands do not falsely change the real tank.

14. [ ] **Defensible tuning and replay — 60 min.** Goal: aim for scenario 1 crossing 4 mg/L in 10–20 plant-min without unsupported parameters.
    - Files: `physics/model.py` (parameters), `physics/tests/test_milestone.py`, `docs/plots/overdose.png`, `docs/research.md`, `docs/demo.md`.
    - Equations/numbers: at V=240 m³, 1→4 mg/L needs 720 g; 600 g/h needs ≥72 min before losses for a nonshrinking tank. A 10–20 min target needs ≥4.32–2.16 kg/h before dilution/decay, or a justified smaller volume/changed initial state.
    - Proof: cite chosen parameters, plot baseline/candidate against 4 mg/L, print crossing time and <20 ms runtime; rerun relevant regressions/end-to-end HOLD, then record reset recipe. Retain honest timing if defensible tuning cannot meet the target.
