# firmware/ (ESP32 station)

Spec: `docs/prompts/PROMPT_EE.md`. Contract: `shared/registers.yaml`, `README.md` section 4.

## Status

Started by the CompE on Saturday morning to unblock gateway step 13. EE takes over from here.

| Build step | Code | Tested on the board |
|---|---|---|
| 1. Blink + serial + Wi-Fi + IP on OLED | done | yes: serial, Wi-Fi, ping. OLED picture not confirmed by eye yet |
| 2. Modbus server, full register map | done | yes (see below) |
| 3. Relays follow coils 0 and 1 | done | yes (see below) |
| 4. Inputs (pot, button, PIR, LDR, DHT11, IR) | not started | |
| 5. Control logic, safe mode, interlock, logic hash | not started | |
| 6. OLED screen, LEDs, buzzer | not started | |
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

Planned pins for later steps are in `docs/prompts/PROMPT_EE.md`; they move into this table
when the code uses them.

## Wiring (module pin -> ESP32 pin)

Drawn in `docs/wiring.svg` (wired parts so far, newest wire highlighted).

Relay module (5 V, 2 channel, optocoupler type, blue JD-VCC jumper cap left in place):

- Relay `VCC` (4-pin header) -> `VIN` (5 V from USB)
- Relay `GND` (4-pin header) -> `GND` next to `VIN`
- Relay `IN1` -> `D26` (GPIO 26)
- Relay `IN2` -> `D27` (GPIO 27)
- Screw terminals: empty. **No pump or fan wired yet.**

Relay behaviour (measured, this module): **active LOW, releases cleanly at 3.3 V logic with VCC = 5 V and the JD-VCC cap on.** So the code drives `LOW` = on, `HIGH` = off; the floating-pin workaround from the spec is not needed. Relays are off from reset: the output latch is loaded HIGH through `gpio_set_level` before `pinMode(OUTPUT)`, first thing in `setup()`; floating pins also read as off on an active-LOW module. Each coil draws about 70 mA from USB through `VIN`; if the board drops off USB when a relay switches, use a direct laptop port and a short cable, or feed `JD-VCC` from a separate 5 V supply.

OLED (0.96" SSD1306, I2C; not currently connected, rewire when the screen step comes):

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

## Acceptance tests

- [x] `python -m sim.poke --host <ESP32 IP> --port 502 read all` reads every register with the documented defaults after boot.
- [ ] Writing coil 0 with `AUTO_MODE = 0` clicks relay 1 within 300 ms. (Step 3 click and latency verified with AUTO_MODE untouched; there is no auto logic yet. Re-run after step 5.)
- [ ] Covering/uncovering the photoresistor flips `CABINET_OPEN` without chatter.
- [x] Changing any of holding registers 0–4 changes `LOGIC_HASH` (tested holding 0).
- [ ] With `SAFE_MODE = 1`, a write to `DOSER_RATE_PCT` has no effect.
- [x] `PUMP_RUNNING` / `DOSER_RUNNING` match the relays; with the pump off, relay 2 stays off. (Step 3 rule only; the rate > 0 and SAFE_MODE conditions arrive in step 5.)
- [ ] Unplugging the hotspot and bringing it back: the station reconnects without a reset.
- [ ] Runs 30 minutes without a hang.

The test tool is `sim/poke.py` at the repo root (run from the repo root); there is no
separate `firmware/tools/poke.py`.
