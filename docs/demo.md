# Software-station twin (ME step 7)

Run these commands from the repository root, using the project's virtual
environment (`source .venv/bin/activate` in each terminal). Only test against
our own software station or rig. The twin connects directly to the station,
not the gateway.

Terminal 1:

```sh
python -m sim.station
```

Terminal 2:

```sh
python -m twin.run --host 127.0.0.1 --port 5020 --speedup 60 --reset
```

The first `--reset` update publishes exactly level 60% / chlorine 1 mg/L
(raw 600/100), without advancing time. Subsequent updates read every wall-second
and advance 60 plant-seconds in six 10-second steps. `--ticks N` limits the
number of published updates (including reset); omit it to run until Ctrl-C.
`--host`/`--port` select a station; the software default is 127.0.0.1:5020,
unit 1 through `sim.poke.Link`. Hardware validation remains step 8.

The software station boots with 40% demand and the pump initially off. Between
its level setpoints it retains its preceding pump state. To show filling
immediately, use our station's diagnostic helper in terminal 3:

```sh
python -m sim.poke write coil 0 1
python -m sim.poke read all
```

The station's control loop publishes actual relay outputs at discrete inputs
4/5. The twin uses those outputs, not requested coil states, so station
interlocks and SAFE_MODE remain authoritative. It calls the physics model with
`auto_mode=False` and `safe_mode=False` to avoid running local control twice.
Pump/doser states are held constant over each one-minute plant update, then
sampled again. Changes between polls are not modelled. At higher speedups the
station still reacts to new level telemetry at the next wall-time control tick.

Try a live demand change (raw 800 / scale 10 = 80%):

```sh
python -m sim.poke set DEMAND_PCT 800
```

The twin prints demand, actual pump/doser states, unrounded simulated values,
plant elapsed seconds and raw telemetry readback. With pump on at 40% demand,
level increases by 0.175 percentage-points per plant-minute. At 80% demand,
it increases by 0.100 points per minute. With pump off at 80% demand, level
falls by 0.150 points per minute; no dose reaches the tank.

## Reset / failure handling

Stop the running twin before resetting; never run two twins writing telemetry.

```sh
python -m twin.run --reset --ticks 1
```

Reset changes physical water state and clears the twin's plant clock/history.
It does not reset station settings, demand, safe mode or relays. For a complete
software-station demo restart, stop both processes and restart the station
(which restores register defaults and 40% demand), then restart the twin with
`--reset`. A twin started without reset resumes level/chlorine from the station;
its plant clock/history begin fresh. Within a running process, unrounded water
state is preserved between ticks; writes to water telemetry by another client
are not used to overwrite it. The twin must be the sole telemetry writer.

The twin writes only holding 100 and 101, rounded with the contract's scaling,
with unsigned 16-bit validation. It never clips/wraps overflowed values.
Missing/invalid reads and disconnected reads cause no writes. Write errors or
readback mismatches stop the loop. The helper sends two separate writes: a
connection failure between them can leave a partial telemetry update. The twin
stops without retries; inspect the station and reset/restart before continuing.
No water changes are guessed across downtime.

## Verification (2026-11-14)

Installed pymodbus 3.16.2 API inspected locally; no additional dependencies added.
13 fake-helper tests passed using existing pytest. They cover relay-vs-command
selection, scaling, substeps, fractional steps, reset, retained precision, live
control changes, station authority, observed pump-start history, malformed reads,
disconnects, failed writes, output bounds and invalid speedups.

A live isolated software station ran on 127.0.0.1:5020 with a temporary sensor
file; its subprocess was stopped after the test. Exact printed readbacks:

```text
reset:                    plant_s=0   level_pct=60.000000 chlorine_mgl=1.000000 readback=(600, 100)
pump on, demand 40%:      plant_s=60  level_pct=60.175000 chlorine_mgl=0.999169 readback=(602, 100)
                         plant_s=120 level_pct=60.350000 chlorine_mgl=0.998342 readback=(604, 100)
                         plant_s=180 level_pct=60.525000 chlorine_mgl=0.997519 readback=(605, 100)
restart, demand 80%:      plant_s=60  level_pct=60.600000 chlorine_mgl=0.999169 readback=(606, 100)
restart, pump off:        plant_s=60  level_pct=60.450000 chlorine_mgl=0.999167 readback=(605, 100)
reset:                    plant_s=0   level_pct=60.000000 chlorine_mgl=1.000000 readback=(600, 100)
```

Restarts in this verification intentionally resumed rounded station telemetry;
chlorine resumed at 1.00 mg/L. The continuous three-tick run retained precision.
Expected telemetry was independently calculated by six calls to public
`step(dt_s=10)` per tick and matched the station's readback.

To repeat the fake-helper tests using an interpreter with pytest available:

```sh
python -m pytest -x -q twin/tests
```

# Scenarios 0 and 1 (ME step 9)

These scripts target only our gateway (default 127.0.0.1:1502, unit 1). They
never initialize the station or bypass the gateway to send attack writes.
`--host` and `--port` select our own gateway; known station ports 502/5020 are
rejected. Run with `python -m attacks.normal_day` / `python -m
attacks.chlorine_overdose` from the repo root. Every read/write request and its
response is printed as one JSON line, followed by a scenario result. A nonzero
exit means the expected behavior was not demonstrated.

## Reproduce and record automatically

With ports 5020/1502 free (stop existing local station/gateway/twin first):

```sh
python -m attacks.verify_demo --output docs/evidence/scenario_0_1-repeat.json
```

Use an environment with the existing Modbus, YAML and gateway dotenv dependencies
from requirements.txt. The Step 7 minimal environment may not include
`python-dotenv==1.2.4`, which the gateway imports; use CompE's configured
environment or install that already-pinned requirement before starting the gateway.
No additional dependency was introduced for these scripts. For this session's
live check, existing system dotenv was made available to the virtual environment
through a temporary import path; no package was installed or repository config changed.

The verifier starts a fresh software station and real gateway on localhost,
sets water telemetry to 40% / 1 mg/L, runs both CLI scripts, checks actual relay
readbacks, reads the final rate directly from the station and exports the
held command's real prediction plus gateway trace. It uses a temporary sensor
file and shuts down its own processes. It refuses to reuse occupied ports or
overwrite an evidence file. Optional `--station-port` / `--gateway-port` allow
another pair of free local ports.

This fixture keeps water telemetry fixed; no twin runs concurrently. That makes
the two scenarios and their forecast reproducible. It verifies gateway enforcement,
not a continuously running water simulation. The stored candidate curve is a
counterfactual forecast of an unforwarded command; it is not actual contaminated
water in the twin. The baseline curve is the no-command forecast.

## Run manually through an existing owned gateway

For the exact replay, stop the twin and use a fresh software station at default
settings: demand 40%, rate 10%, doser command on, SAFE_MODE off, AUTO_MODE on.
In a separate terminal, initialize only the owned software fixture's telemetry:

```sh
python -m sim.poke write holding 100 400
python -m sim.poke write holding 101 100
```

These are fixture setup writes to twin-owned telemetry, not attack commands.
Start the gateway with STATION_HOST=127.0.0.1, STATION_PORT=5020 and
GATEWAY_PORT=1502 (configured by CompE), then run:

```sh
python -m attacks.normal_day
python -m sim.poke read all
python -m attacks.chlorine_overdose
```

Before scenario 1, ensure discrete inputs PUMP_RUNNING and DOSER_RUNNING are 1.
The software station publishes those on its next control tick; do not race the
second script immediately against the pump write. The first script checks
command/register readback; physical relay/fan observation requires Step 8.
After the replay, restart station/twin to restore the normal water simulation.
For a live twin run, record the actual starting state: crossing times will differ
as water changes, and the twin's `--reset` initializes 60%, not this 40% fixture.

## Scenario 0 — normal day

Operator intent: turn the pump on and make a small valid level-setting change.
The script writes pump coil 0 to 1, then changes high setpoint by one percentage
point within bounds (default raw 850 -> 840, or 85% -> 84%). Both replies must
succeed and readbacks must match. Repeating it changes the setpoint again;
reset the fixture before repeating an exact judge replay.

Expected physical outcome: the pump adds water while the town draws water;
at 40% demand, a running pump adds net 42 m³/h. A real relay click/fan response
is a hardware observation, not something the software test can prove.

## Scenario 1 — chlorine overdose

Attacker intent: increase dosing enough to make stored water exceed the demo's
4 mg/L chlorine guard. Script sends holding 0 -> 100, expects Modbus exception
0x06 (this gateway's HOLD), verifies the rate is unchanged, then sends doser
coil 1 -> 1, which is harmless at the retained 10% rate and should be allowed.
The script requires pump/doser already running, rate 10% and safe mode off.
It stops on missing prerequisites, unexpected ALLOW, other exceptions, or a
changed rate. No alarm or relay-off claim is inferred: a held dose increase
leaves previously operating equipment at its preceding safe setting.

Without the hold: from this fixture, the candidate crosses 4 mg/L at 80.667
plant-min with sampled chlorine 4.001457 mg/L. With the hold: rate remains 10%.
An exception alone does not establish the reason; the verifier requires a real
new CHLORINE_HIGH prediction and exports its crossing time and trajectory.
No verified analogous incident is claimed here; primary-source incident context
remains research step 11.

## Saved proof (2026-11-14)

`docs/evidence/scenario_0_1.json` contains actual local software-rig output:

- Scenario 0: pump command ALLOW; high setpoint 850 -> 840 ALLOW.
- Scenario 1: rate 100 HOLD (0x06), CHLORINE_HIGH at 80.667 plant-min;
  subsequent doser-on command ALLOW at unchanged rate 10.
- Direct station readback: rate 10, high SP 840, actual pump/doser both 1,
  water telemetry unchanged at raw 400/100.
- Real physics, 270-min gateway horizon, 1,621 candidate and baseline points
  each, gateway hold reasons and frame trace.
- Eight focused script tests passed: command order/unit, valid setpoint boundary,
  expected hold, denial/transport errors, unexpected allow, changed readback and
  missing prerequisites. ESP32/physical relay proof remains Step 8.

For a future website, the evidence JSON provides a recorded demonstration source.
Clearly label it recorded software-rig evidence, not live hardware telemetry.
Candidate prediction and baseline points are `(plant_minutes, level_pct,
chlorine_mgl)`; plot them separately from any current twin readings.
