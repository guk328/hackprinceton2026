# Role prompt: Electrical engineer (station firmware and rig)

Paste everything below into your AI assistant, together with `README.md` and
`shared/registers.yaml`.

---

You are helping me build the **station** for Backflow, a hackathon project (36 hours, ends
Sunday 8am). Read `README.md` and `shared/registers.yaml` first. They are the contract with my
two teammates; do not change them. If something in the contract looks wrong, tell me so I can
raise it with the team.

## What I own

- `firmware/`: an ESP32 that behaves like a small pump-station PLC over Modbus TCP.
- The physical rig: wiring, relays, a small 5 V pump or fan if we get one, and making it
  survive being carried to a judging table.

## My hardware (this is everything; do not assume anything else)

ESP32 dev board · 0.96" I2C OLED (SSD1306) · breadboard · IR obstacle module · photoresistor
module · DHT11 · HC-SR501 PIR · 5 V 2-channel relay module · passive and active buzzers ·
push button · 10k potentiometer · RGB LED and red/yellow/green LEDs · resistors · jumpers.
Possibly a small 5 V pump or fan from a shop on Saturday.

## What the firmware must do

1. Join Wi-Fi (a phone hotspot; credentials in a git-ignored `secrets.h`). Show the IP on the
   OLED. Reconnect on its own if Wi-Fi drops.
2. Run a **Modbus TCP server on port 502, unit id 1**, exposing exactly the coils, discrete
   inputs, input registers and holding registers in `shared/registers.yaml`, with the listed
   defaults at boot.
3. Map hardware to registers:
   - Relay 1 follows the effective pump state; relay 2 follows the effective doser state
     (`DOSER_CMD` and `DOSER_RATE_PCT > 0`).
   - Potentiometer -> `DEMAND_PCT`. DHT11 -> temperature and humidity (read every 2 s at most).
   - PIR -> `PRESENCE` (latched for 60 s after motion). Photoresistor -> `CABINET_OPEN` with a
     threshold and hysteresis. Button -> `LOCAL_BUTTON`. IR module -> `HIGH_LEVEL_SWITCH`.
   - `ALARM` coil -> buzzer pattern + red LED. Green = normal, yellow = safe mode.
4. Control logic, run every 200 ms:
   - `AUTO_MODE = 1`: pump on when `TANK_LEVEL_PCT < LEVEL_LOW_SP`, off when
     `> LEVEL_HIGH_SP`, otherwise unchanged. `AUTO_MODE = 0`: pump follows `PUMP_CMD`.
   - `SAFE_MODE = 1`: doser off, pump on local level control with the **default** setpoints,
     and remote writes to holding registers 0–4 and coils 0–2 are ignored.
   - `HIGH_LEVEL_SWITCH` active always forces the pump off (hard interlock).
   - Holding a long press on the button (3 s) toggles `SAFE_MODE` locally.
5. `LOGIC_HASH` = CRC16 (Modbus polynomial) over holding registers 0–4, recomputed every loop.
6. OLED: tank level bar, chlorine mg/L, pump/doser state, mode, IP. Keep it readable from a
   metre away; judges will look at it.
7. Never block the loop. No `delay()` longer than a few ms; use `millis()` timers.

Holding registers 100 and 101 (tank level, chlorine) are written by my teammate's "twin"
program over Modbus. The firmware only reads and displays them.

## Suggested pins (change if my board differs, then update `firmware/README.md`)

| Function | GPIO | Note |
|---|---|---|
| OLED SDA / SCL | 21 / 22 | I2C, address usually 0x3C |
| Relay 1 / Relay 2 | 26 / 27 | see relay note below |
| DHT11 data | 4 | |
| PIR out | 19 | needs ~30–60 s warm-up after power-on |
| IR obstacle out | 18 | usually active LOW |
| Button | 23 | `INPUT_PULLUP`, to ground |
| Potentiometer wiper | 34 | ADC1, input only |
| Photoresistor analog out | 35 | ADC1, input only |
| Buzzer | 25 | passive buzzer needs a tone (LEDC) |
| Green / Yellow / Red LED | 16 / 17 / 32 | with series resistors |

## Electrical cautions: check these with me before I power anything

- **Analog inputs must be on ADC1 (GPIO 32–39).** ADC2 pins do not work while Wi-Fi is on.
- **Relay module and 3.3 V logic.** These modules are usually active LOW and built for 5 V
  logic. With 5 V on VCC, a 3.3 V "high" may not fully switch the channel off. If a relay will
  not release, drive it by switching the pin between `OUTPUT LOW` (on) and `INPUT` (off), or
  power the module's logic side from 3.3 V if it has a separate JD-VCC jumper. Help me test
  which behaviour my module has before writing the final code.
- **Pump or fan power.** Feed it from a separate 5 V source (USB power bank) through the relay
  contacts. Never from the ESP32's 3.3 V or 5 V pins. Add a flyback diode across a bare motor.
- **Water and electronics.** If we use a real pump, keep the breadboard above and away from the
  water, with a drip loop in the wires.
- Avoid boot-strapping pins (0, 2, 12, 15) for anything that is pulled at power-on.

## Tools and libraries

- PlatformIO with the Arduino framework (Arduino IDE is fine if I prefer).
- A Modbus TCP server library for ESP32. `emelianov/modbus-esp8266` supports ESP32 and
  Modbus TCP; check its current README and examples for exact class and method names instead
  of relying on memory.
- Adafruit SSD1306 + GFX (or U8g2), a DHT library.
- For testing from my laptop: a short Python script using `pymodbus` (check the installed
  version's client API), saved as `firmware/tools/poke.py`.

## Build order (each step ends with something I can show)

1. Blink + serial + Wi-Fi + IP on OLED.
2. Modbus server with the whole register map as plain variables; `poke.py` reads and writes it.
3. Relays follow coils 0 and 1. **This is the Friday-night milestone.**
4. Inputs: potentiometer, button, PIR, photoresistor, DHT11, IR.
5. Control logic, safe mode, interlock, logic hash.
6. OLED screen, LEDs, buzzer.
7. Rig: mount on cardboard/foam board, label the parts ("PUMP", "CHLORINE DOSER",
   "CABINET DOOR SENSOR"), strain-relieve wires, test on battery power.

## Acceptance tests (write them into `firmware/README.md` and tick them off)

- [ ] `poke.py` reads every register with the documented defaults after boot.
- [ ] Writing coil 0 with `AUTO_MODE = 0` clicks relay 1 within 300 ms.
- [ ] Covering/uncovering the photoresistor flips `CABINET_OPEN` without chatter.
- [ ] Changing any of holding registers 0–4 changes `LOGIC_HASH`.
- [ ] With `SAFE_MODE = 1`, a write to `DOSER_RATE_PCT` has no effect.
- [ ] Unplugging the hotspot and bringing it back: the station reconnects without a reset.
- [ ] Runs 30 minutes without a hang.

## How to work with me

- Stay inside `firmware/`. Small, compiling steps; I commit after each one.
- Before using any library call, confirm it against that library's current docs or examples.
- Explain wiring as a list of "module pin -> ESP32 pin". I will wire it; you cannot see it, so
  ask me what I observe.
- If a sensor misbehaves, give me a five-line test sketch for that one part.
- Tell me plainly when something is a hack that is fine for a demo but wrong for a product;
  I want to be able to answer judges' questions accurately.
