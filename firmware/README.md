# firmware/ (ESP32 station)

Spec: `docs/prompts/PROMPT_EE.md`. Contract: `shared/registers.yaml`, `README.md` section 4.

## Status

Started by the CompE on Saturday morning to unblock gateway step 13. EE takes over from here.

| Build step | Code | Tested on the board |
|---|---|---|
| 1. Blink + serial + Wi-Fi + IP on OLED | done | yes: serial, Wi-Fi, ping. OLED picture not confirmed by eye yet |
| 2. Modbus server, full register map | not started | |
| 3. Relays follow coils 0 and 1 | not started | |
| 4. Inputs (pot, button, PIR, LDR, DHT11, IR) | not started | |
| 5. Control logic, safe mode, interlock, logic hash | not started | |
| 6. OLED screen, LEDs, buzzer | not started | |
| 7. Rig | not started | |

## Build and flash

Board: **ESP-32S (NodeMCU-32S, ESP32-WROOM-32)**, PlatformIO env `station`, board `nodemcu-32s`.
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
- Pinned versions are in `platformio.ini` (platform espressif32 8.0.0, Arduino core 3.3.x).

## Pins actually used

| Function | GPIO | State |
|---|---|---|
| OLED SDA / SCL | 21 / 22 | in code (step 1) |
| On-board LED (heartbeat) | 2 | in code (step 1) |

Planned pins for later steps are in `docs/prompts/PROMPT_EE.md`; they move into this table
when the code uses them.

## Wiring (module pin -> ESP32 pin)

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

## Acceptance tests

- [ ] `python -m sim.poke --host <ESP32 IP> --port 502 read all` reads every register with the documented defaults after boot.
- [ ] Writing coil 0 with `AUTO_MODE = 0` clicks relay 1 within 300 ms.
- [ ] Covering/uncovering the photoresistor flips `CABINET_OPEN` without chatter.
- [ ] Changing any of holding registers 0–4 changes `LOGIC_HASH`.
- [ ] With `SAFE_MODE = 1`, a write to `DOSER_RATE_PCT` has no effect.
- [ ] `PUMP_RUNNING` / `DOSER_RUNNING` match the relays; with the pump off, relay 2 stays off.
- [ ] Unplugging the hotspot and bringing it back: the station reconnects without a reset.
- [ ] Runs 30 minutes without a hang.

The test tool is `sim/poke.py` at the repo root (run from the repo root); there is no
separate `firmware/tools/poke.py`.
