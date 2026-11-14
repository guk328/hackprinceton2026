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
