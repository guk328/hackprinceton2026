# firmware/ (ESP32 station)

Spec: `docs/prompts/PROMPT_EE.md`. Contract: `shared/registers.yaml`, `README.md` section 4.

## Status

Started by the CompE on Saturday morning to unblock gateway step 13. EE takes over from here.

| Build step | Code | Tested on the board |
|---|---|---|
| 1. Blink + serial + Wi-Fi + IP on OLED | done | yes: serial, Wi-Fi, ping. OLED picture not confirmed by eye yet |
| 2. Modbus server, full register map | done | yes (see below) |
| 3. Relays follow coils 0 and 1 | done | yes (see below) |
| 4. Inputs (pot, button, PIR, LDR, DHT11, IR) | done | yes for pot, button, PIR, photoresistor, IR; **DHT11 never answered; being taken off the rig** (registers 1-2 read 0) |
| 5. Control logic, safe mode, interlock, logic hash | done (compiles; same rules as `sim/station.py`). Button: hold 3 s = toggle `SAFE_MODE`, short press = clear `ALARM` | yes, Sun 15 Nov: flashed, `python -m firmware.tools.check_control --host 172.20.10.9 --port 502` passed all 17 checks (run it with twin and gateway stopped; it clicks the relays). Short press clearing `ALARM` confirmed by the operator and over Modbus. 3 s hold for `SAFE_MODE` not tried by hand |
| 6. OLED screen, LEDs, buzzer | done | yes (see below) |
| 7. Rig | not started | |

## Build and flash

Board: 30-pin ESP32 DevKit (ESP32-WROOM-32; pins printed `D26`, `VIN`, ...). PlatformIO env `station`, board definition `nodemcu-32s` (same chip, same on-board LED on GPIO 2).
PlatformIO Core 6.2.0 installed with `pip install platformio` (no VS Code extension needed).

```
cd firmware
copy src\secrets.h.example src\secrets.h     # then edit: hotspot SSID + password
python -m platformio run -t upload           # add --upload-port COM5 if auto-detect fails
python -m platformio device monitor          # 115200 baud, Ctrl+C to quit
```

- `src/secrets.h` is git-ignored. Never commit it.
- The ESP32 is 2.4 GHz only. iPhone hotspot: turn on "Maximize Compatibility".
- If upload hangs at `Connecting....`, hold the **BOOT** button until it starts writing.
- If the port is "busy", close any running `device monitor`. If no COM port exists, the board is not on USB (reseat the cable; CP210x driver must be installed).
- Bench test for the relay module alone: `python -m platformio run -e relaytest -t upload --upload-port COM9` (toggles IN1/IN2, source in `tools/relay_test/`). Flash `-e station` again afterwards.
- Pinned versions are in `platformio.ini` (platform espressif32 8.0.0, Arduino core 3.3.x).

## Modbus library notes

- `emelianov/modbus-esp8266@4.1.0`, `ModbusIP mb; mb.server(502)` called once after the first Wi-Fi connect, `mb.task()` every loop.
- Up to 8 simultaneous clients on ESP32 (`MODBUSIP_MAX_CLIENTS`).
- **Patched at build time:** the library's `MODBUSIP_UNIQUE_CLIENTS` closes an older connection when the same IP connects again. Gateway, twin and poke share the laptop's IP, so `tools/patch_modbus_lib.py` (PlatformIO pre-script) disables that line in `.pio/libdeps`. If the build says "line not found", the library changed.
- **Known gap:** the library answers any unit id, not just 1. Fine for the demo; do not claim it filters.
- Registers are created with `addCoil/addIsts/addIreg/addHreg` using the addresses in `shared/registers.yaml`. Unmapped addresses return exception 2.
- Step 2 only: sensor registers read 0 and the station accepts any write (no control logic yet). `LOGIC_HASH` and `UPTIME_S` are live. Expected `LOGIC_HASH` at defaults is 42108.

## Pins actually used

| Function | GPIO | State |
|---|---|---|
| OLED SDA / SCL | 21 / 22 | in code (step 1); **OLED was unplugged during step 3**, serial says "OLED not found" until it is rewired |
| On-board LED (heartbeat) | 2 | in code (step 1) |
| Relay 1 IN1 (pump) | 26 (`D26`) | in code and wired (step 3) |
| Relay 2 IN2 (doser) | 27 (`D27`) | in code and wired (step 3) |

| Pot wiper -> `DEMAND_PCT` | 34 (`D34`, ADC1) | in code (step 4) |
| Photoresistor AO -> `CABINET_OPEN` | 35 (`D35`, ADC1) | in code (step 4) |
| DHT11 data | 4 (`D4`) | in code (step 4) |
| PIR OUT -> `PRESENCE` | 19 (`D19`) | in code (step 4) |
| IR module OUT -> `HIGH_LEVEL_SWITCH` | 18 (`D18`), active LOW | in code (step 4) |
| Button -> `LOCAL_BUTTON` | 23 (`D23`), `INPUT_PULLUP`, other leg to GND | in code (step 4) |
| Green LED (normal) | 16 (printed `RX2`), 120 ohm | in code and wired (step 6) |
| Yellow LED (safe mode) | 17 (printed `TX2`), 120 ohm | in code and wired (step 6) |
| Red LED (ALARM) | 32 (`D32`), 120 ohm | in code and wired (step 6) |
| Active buzzer (ALARM) | 25 (`D25`), HIGH = sound | in code and wired (step 6) |

## Wiring (module pin -> ESP32 pin)

Drawn in `docs/wiring.svg` (wired parts so far, newest wire highlighted).

Relay module (5 V, 2 channel, optocoupler type, blue JD-VCC jumper cap left in place):

- Relay `VCC` (4-pin header) -> `VIN` (5 V from USB)
- Relay `GND` (4-pin header) -> `GND` next to `VIN`
- Relay `IN1` -> `D26` (GPIO 26)
- Relay `IN2` -> `D27` (GPIO 27)
- Screw terminals: empty. **No pump or fan wired yet.**

Relay behaviour (measured, this module): **active LOW, releases cleanly at 3.3 V logic with VCC = 5 V and the JD-VCC cap on.** So the code drives `LOW` = on, `HIGH` = off; the floating-pin workaround from the spec is not needed. Relays are off from reset: the output latch is loaded HIGH through `gpio_set_level` before `pinMode(OUTPUT)`, first thing in `setup()`; floating pins also read as off on an active-LOW module. Each coil draws about 70 mA from USB through `VIN`; if the board drops off USB when a relay switches, use a direct laptop port and a short cable, or feed `JD-VCC` from a separate 5 V supply.

**Full wiring through step 6** (breadboard holes, LEDs on GPIO 16/17/32 with 220 ohm, buzzer on GPIO 25, pin table, wiring rounds): `docs/wiring_full.svg`. It replaces `docs/wiring.svg` for building; same pins, same three moved wires.

### Step 4 wiring (breadboard rails; see `docs/wiring.svg`)

Rails: **5V rail** <- `VIN`; **3V3 rail** <- `3V3` (bottom row, first pin); **GND rail** <- `GND` (bottom row, next to 3V3). Relay GND stays on the GND next to `VIN`.
Three existing wires move: relay `VCC` VIN -> 5V rail; OLED `VCC` 3V3 pin -> 3V3 rail; OLED `GND` -> GND rail.

| Module pin | -> | Goes to |
|---|---|---|
| DHT11 `S`/`OUT` | | `D4` (GPIO 4) |
| DHT11 `VCC`/`+` | | 3V3 rail |
| DHT11 `GND`/`-` | | GND rail |
| IR `OUT` | | `D18` (GPIO 18) |
| IR `VCC` / `GND` | | 3V3 rail / GND rail |
| PIR `OUT` | | `D19` (GPIO 19) |
| PIR `VCC` | | **5V rail** |
| PIR `GND` | | GND rail |
| Button leg A (opposite corner from B) | | `D23` (GPIO 23) |
| Button leg B | | GND rail |
| Pot wiper (middle) | | `D34` (GPIO 34, ADC1) |
| Pot outer legs | | 3V3 rail and GND rail |
| Photoresistor `AO` | | `D35` (GPIO 35, ADC1) |
| Photoresistor `VCC` / `GND` | | 3V3 rail / GND rail (`DO` unused) |

3V3 parts: DHT11, IR, photoresistor, pot, OLED. 5V parts: PIR and the relay. Never power the IR or photoresistor module from 5V (their output would reach the ESP32 pin at 5 V). The PIR output is 3.3 V so a 5 V PIR is safe. Analog inputs are on ADC1 only. PIR warm-up about 1 minute: the firmware ignores the PIR for 60 s after every boot.

Hack worth knowing for judges: the DHT11 is read by bit-banging the pin, which blocks the loop for about 4 ms once every 2.5 s (interrupts off on one core). Fine for a demo, a product would use the RMT peripheral.

OLED (0.96" SSD1306, I2C):

- OLED GND -> GND
- OLED VCC -> 3V3
- OLED SCL -> GPIO 22
- OLED SDA -> GPIO 21

The OLED address is selectable (0x3C or 0x3D); the firmware scans the bus at boot, prints
what it finds on serial and uses whichever of the two answers.

## What has been tested

Step 1 on the real board (COM9, CP210x driver needed on Windows):
- I2C scan finds the OLED at 0x3C; `oled.begin` ok.
- Joined the phone hotspot, IP 172.20.10.9, RSSI -13 dBm; blue LED blinks slowly when connected.
- Laptop on the same hotspot pings the board (about 74 ms, 0% loss).
- Not yet checked by eye: OLED text layout. Not yet tested: Wi-Fi drop and reconnect.

Step 2 on the real board:
- Serial: CRC self-check 0x4B37 ok, `LOGIC_HASH at defaults: 42108`, server listening on 502.
- `python -m sim.poke --host <IP> --port 502 read all`: all 5 coils, 6 discrete inputs, 7 holding and 5 input registers read, with the documented defaults; `LOGIC_HASH` 42108; `UPTIME_S` counts.
- Write holding 0 = 55 from one client is seen by another; `LOGIC_HASH` goes 42108 -> 13680 and back to 42108 on restore, with no lag on an immediate read.
- Coil 0 write reads back 1.
- Three simultaneous clients from the laptop (same IP): 300 mixed reads, 16.6 ms average.
- Not yet tested: 4+ clients from different IPs, gateway against the board.

Step 3 on the real board:
- Relay bench test (`-e relaytest`): clicks every 2 s, IN1 LED fully on then fully off. Active LOW confirmed.
- Station firmware: coil 0 drives relay 1, coil 1 drives relay 2 (only while coil 0 is on; `DOSER_CMD` defaults to 1, so a plain follow would click relay 2 at every boot). `PUMP_RUNNING` / `DOSER_RUNNING` mirror the pins.
- Via `sim.poke`-style writes: PUMP_CMD=1 -> both relays on; DOSER_CMD=0 -> relay 2 off; DOSER_CMD=1 -> relay 2 on; PUMP_CMD=0 -> both off. Discrete inputs matched each step. Operator confirmed the clicks and LEDs.
- Latency: write acknowledged in 9-35 ms; coil write to `PUMP_RUNNING` following: 19-30 ms over 10 toggles (limit 300 ms).
- Not yet tested: relays with the pump/fan connected, AUTO_MODE/SAFE_MODE behaviour (step 5), 30-minute run.

Step 4 on the real board (all watched over Modbus with `sim.poke`'s `read_all`, about 3 reads/s, plus the serial log):
- Pot: `DEMAND_PCT` sweeps 0.7 % to 98.5 % and back, both directions, smooth, no jitter at rest (0.3 % dead band); 1000 at the stop.
- Button: `LOCAL_BUTTON` 1 while held (12.7 s hold seen), 0 on release; serial `button DOWN/UP`.
- IR module: `HIGH_LEVEL_SWITCH` 1 with a hand 3-5 cm away, 0 when removed. Needed its trimmer turned down first (it saw the table and read "obstacle" permanently).
- PIR: ignored for 60 s after boot, then motion sets `PRESENCE` = 1; the 60 s latch expiry back to 0 is seen on serial.
- Photoresistor: our module is the **3-pin LM393 type (`DO GND VCC`, no `AO`)**, so D35 reads a comparator output: about 0 in light, 4095 in the dark. The threshold is the module's blue trimmer; firmware adds smoothing plus a 1800/2400 hysteresis band (`LDR_LIGHT_IS_HIGH = false`). Cover -> `CABINET_OPEN` 0, uncover -> 1, one clean change each way.
- DHT11: **fails**. The sensor never answers the start pulse ("stage 1" in the serial log, line idles high). Firmware reader fixed for a release-timing race; still no answer, so wiring is suspected. `CABINET_TEMP_C` / `CABINET_HUMIDITY` stay 0.
- Serial tip: `python -m platformio device monitor` (and pyserial with default settings) toggles DTR/RTS and resets the board; open the port with DTR and RTS held low to read the log without a reboot.
- LED resistors on this build are 120 ohm (brown-red-black-black-brown), about 11-12 mA per LED: fine for an ESP32 pin.

Step 6 on the real board (scripted `pymodbus` writes, run twice, operator watching):
- Screen: tank bar (left), level `60.0%` and chlorine `1.00 mg/L` in size-2 text, PUMP/DOSE boxes (filled white = running, from `PUMP_RUNNING`/`DOSER_RUNNING`), bottom line mode (`AUTO`/`MAN`/`SAFE`/`ALARM`) + IP. Redrawn every 500 ms; I2C at 400 kHz so a frame takes about 25 ms (blocking: a demo hack, acceptable at this rate).
- `ALARM = 1`: red LED, active buzzer three 150 ms beeps every 1.6 s, screen flashes inverted, `ALARM` on screen. `ALARM = 0` stops all of it.
- `SAFE_MODE = 1`: yellow LED, green off, `SAFE` on screen. Green is on only when neither SAFE_MODE nor ALARM is set.
- Tank 25.0 % / chlorine 1.80 written to holding 100/101: bar and numbers follow; `PUMP_CMD = 1`: relay clicks and both boxes fill. Defaults restored afterwards and read back.
- Modbus reads during the run (alarm flashing, redraws): median 15-16 ms, p95 54-56 ms, max 79-91 ms over about 205 reads.
- Operator confirmed every stage by eye, OLED readable at about a metre, buzzer loud enough.

## Acceptance tests

- [x] `python -m sim.poke --host <ESP32 IP> --port 502 read all` reads every register with the documented defaults after boot.
- [ ] Writing coil 0 with `AUTO_MODE = 0` clicks relay 1 within 300 ms. (Step 3 click and latency verified with AUTO_MODE untouched; there is no auto logic yet. Re-run after step 5.)
- [x] Covering/uncovering the photoresistor flips `CABINET_OPEN` without chatter. (Step 4: one transition each way over Modbus; threshold set by the module's trimmer.)
- [x] Changing any of holding registers 0–4 changes `LOGIC_HASH` (tested holding 0).
- [ ] With `SAFE_MODE = 1`, a write to `DOSER_RATE_PCT` has no effect.
- [x] `PUMP_RUNNING` / `DOSER_RUNNING` match the relays; with the pump off, relay 2 stays off. (Step 3 rule only; the rate > 0 and SAFE_MODE conditions arrive in step 5.)
- [ ] Unplugging the hotspot and bringing it back: the station reconnects without a reset.
- [ ] Runs 30 minutes without a hang.

The test tool is `sim/poke.py` at the repo root (run from the repo root); there is no
separate `firmware/tools/poke.py`.
