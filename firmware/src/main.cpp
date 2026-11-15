// Backflow station firmware. Steps 1-6: Wi-Fi, Modbus TCP server, inputs, control logic (auto pump,
// safe mode, high-level interlock, logic hash), relays, OLED/LEDs/buzzer.
#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <ModbusIP_ESP8266.h>
#include <driver/gpio.h>  // gpio_set_level: preset the relay pins before they become outputs

#include "secrets.h"  // WIFI_SSID, WIFI_PASS (git-ignored, see secrets.h.example)

// ---- pins (keep firmware/README.md in sync) ----
const uint8_t PIN_SDA = 21;
const uint8_t PIN_SCL = 22;
const uint8_t PIN_LED = LED_BUILTIN;  // GPIO 2, on-board blue LED
const uint8_t PIN_RELAY1 = 26;        // D26 -> relay IN1 (pump)
const uint8_t PIN_RELAY2 = 27;        // D27 -> relay IN2 (doser)
const uint8_t PIN_POT = 34;           // D34, ADC1, input only: pot wiper
const uint8_t PIN_LDR = 35;           // D35, ADC1, input only: photoresistor module AO
const uint8_t PIN_DHT = 4;            // D4: DHT11 data
const uint8_t PIN_PIR = 19;           // D19: PIR OUT
const uint8_t PIN_IR = 18;            // D18: IR module OUT (active LOW)
const uint8_t PIN_BUTTON = 23;        // D23: button to GND, INPUT_PULLUP
const uint8_t PIN_LED_GREEN = 16;     // RX2: green LED via 120 ohm
const uint8_t PIN_LED_YELLOW = 17;    // TX2: yellow LED via 120 ohm
const uint8_t PIN_LED_RED = 32;       // D32: red LED via 120 ohm
const uint8_t PIN_BUZZER = 25;        // D25: active buzzer + leg (HIGH = sound)

// Input tuning (step 4)
const uint32_t PIR_WARMUP_MS = 60000;  // PIR is unreliable after power-on: ignored this long after boot
const uint32_t PIR_LATCH_MS = 60000;   // PRESENCE stays 1 this long after the last motion
const uint32_t DEBOUNCE_MS = 30;
const uint32_t ALARM_SOUND_MS = 8000;  // buzzer time per alarm; the red LED stays on until acknowledged
const uint32_t BUTTON_LONG_MS = 3000;  // hold this long to toggle SAFE_MODE locally
const uint32_t DHT_PERIOD_MS = 2500;   // DHT11 limit is 1 read / 2 s
// Photoresistor module: ours is the 3-pin LM393 type (DO GND VCC, no AO). DO is LOW in light, HIGH
// in the dark; the light threshold and its hysteresis are set by the module's blue trimmer. The
// firmware still reads it on ADC1 (D35) with smoothing and its own hysteresis, so a 4-pin module's
// AO would work too (set LDR_LIGHT_IS_HIGH to match). Thresholds are on a 0-4095 "light" metric.
const bool LDR_LIGHT_IS_HIGH = false;
const int LDR_OPEN_ABOVE = 2400;       // light metric above this -> CABINET_OPEN = 1
const int LDR_CLOSED_BELOW = 1800;     // light metric below this -> CABINET_OPEN = 0
// Pot ADC range (the ESP32 ADC is not linear near the ends; trim so both ends reach 0 and 100 %)
const int POT_RAW_MIN = 30, POT_RAW_MAX = 4060;

// Relay module is active LOW (tested: releases cleanly at 3.3 V with VCC = 5 V, JD-VCC cap on).
const uint8_t RELAY_ON = LOW, RELAY_OFF = HIGH;


// ---- Modbus register map (mirror of shared/registers.yaml; do not invent addresses) ----
// Coils
enum { C_PUMP_CMD = 0, C_DOSER_CMD = 1, C_AUTO_MODE = 2, C_SAFE_MODE = 3, C_ALARM = 4 };
// Discrete inputs
enum { DI_PRESENCE = 0, DI_CABINET_OPEN = 1, DI_LOCAL_BUTTON = 2, DI_HIGH_LEVEL_SWITCH = 3,
       DI_PUMP_RUNNING = 4, DI_DOSER_RUNNING = 5 };
// Input registers
enum { IR_DEMAND_PCT = 0, IR_CABINET_TEMP_C = 1, IR_CABINET_HUMIDITY = 2, IR_LOGIC_HASH = 10,
       IR_UPTIME_S = 11 };
// Holding registers
enum { HR_DOSER_RATE_PCT = 0, HR_LEVEL_HIGH_SP = 1, HR_LEVEL_LOW_SP = 2, HR_CHLORINE_TARGET = 3,
       HR_LOGIC_VERSION = 4, HR_TANK_LEVEL_PCT = 100, HR_CHLORINE_MGL = 101 };

ModbusIP mb;
bool mbStarted = false;

// CRC16/MODBUS: poly 0xA001 (reflected), init 0xFFFF.
uint16_t crc16(const uint8_t *d, size_t n) {
  uint16_t c = 0xFFFF;
  while (n--) {
    c ^= *d++;
    for (int i = 0; i < 8; i++) c = (c & 1) ? (c >> 1) ^ 0xA001 : c >> 1;
  }
  return c;
}

// LOGIC_HASH over holding 0-4 as 10 bytes, each register high byte first.
uint16_t logicHash() {
  uint8_t b[10];
  for (int i = 0; i < 5; i++) {
    uint16_t v = mb.Hreg(i);
    b[2 * i] = v >> 8;
    b[2 * i + 1] = v & 0xFF;
  }
  return crc16(b, sizeof b);
}

void modbusDefine() {
  mb.addCoil(C_PUMP_CMD, false);
  mb.addCoil(C_DOSER_CMD, true);
  mb.addCoil(C_AUTO_MODE, true);
  mb.addCoil(C_SAFE_MODE, false);
  mb.addCoil(C_ALARM, false);
  for (int a = DI_PRESENCE; a <= DI_DOSER_RUNNING; a++) mb.addIsts(a, false);
  mb.addIreg(IR_DEMAND_PCT, 0);
  mb.addIreg(IR_CABINET_TEMP_C, 0);
  mb.addIreg(IR_CABINET_HUMIDITY, 0);
  mb.addIreg(IR_LOGIC_HASH, 0);
  mb.addIreg(IR_UPTIME_S, 0);
  mb.addHreg(HR_DOSER_RATE_PCT, 10);
  mb.addHreg(HR_LEVEL_HIGH_SP, 850);
  mb.addHreg(HR_LEVEL_LOW_SP, 400);
  mb.addHreg(HR_CHLORINE_TARGET, 100);
  mb.addHreg(HR_LOGIC_VERSION, 1);
  mb.addHreg(HR_TANK_LEVEL_PCT, 600);
  mb.addHreg(HR_CHLORINE_MGL, 100);
}

// Relays off with no glitch. Before this runs the pins float (reset state), which is also "off"
// for an active-LOW module. Order matters: put HIGH in the output latch first, then enable the
// output driver, so the pin never drives LOW. (digitalWrite before pinMode is ignored by
// Arduino-ESP32 3.x, hence the IDF call.)
void relaysOffAtBoot() {
  gpio_set_level((gpio_num_t)PIN_RELAY1, RELAY_OFF);
  gpio_set_level((gpio_num_t)PIN_RELAY2, RELAY_OFF);
  pinMode(PIN_RELAY1, OUTPUT);
  pinMode(PIN_RELAY2, OUTPUT);
  digitalWrite(PIN_RELAY1, RELAY_OFF);
  digitalWrite(PIN_RELAY2, RELAY_OFF);
}

// ---------------- step 5: control logic ----------------
// Same rules as sim/station.py Station.step (the reference). Runs every loop, right after
// mb.task(), so a coil write reaches the relay pin in the same pass.
const uint16_t DEFAULT_LEVEL_HIGH_SP = 850, DEFAULT_LEVEL_LOW_SP = 400;  // used in SAFE_MODE

void controlApply() {
  static bool pumpPin = false, doserPin = false;
  bool safe = mb.Coil(C_SAFE_MODE);
  uint16_t lo = safe ? DEFAULT_LEVEL_LOW_SP : mb.Hreg(HR_LEVEL_LOW_SP);
  uint16_t hi = safe ? DEFAULT_LEVEL_HIGH_SP : mb.Hreg(HR_LEVEL_HIGH_SP);
  uint16_t level = mb.Hreg(HR_TANK_LEVEL_PCT);
  bool pump = mb.Coil(C_PUMP_CMD);
  if (safe || mb.Coil(C_AUTO_MODE)) {   // between the setpoints the coil is the memory
    if (level < lo) pump = true;
    else if (level > hi) pump = false;
  }
  if (mb.Ists(DI_HIGH_LEVEL_SWITCH)) pump = false;          // hard interlock
  if (pump != mb.Coil(C_PUMP_CMD)) mb.Coil(C_PUMP_CMD, pump);
  if (safe && mb.Coil(C_DOSER_CMD)) mb.Coil(C_DOSER_CMD, false);
  // No flow, no dose.
  bool doser = pump && !safe && mb.Coil(C_DOSER_CMD) && mb.Hreg(HR_DOSER_RATE_PCT) > 0;

  if (pump != pumpPin) {
    pumpPin = pump;
    digitalWrite(PIN_RELAY1, pump ? RELAY_ON : RELAY_OFF);
    Serial.printf("relay 1 (pump) %s\n", pump ? "ON" : "OFF");
  }
  if (doser != doserPin) {
    doserPin = doser;
    digitalWrite(PIN_RELAY2, doser ? RELAY_ON : RELAY_OFF);
    Serial.printf("relay 2 (doser) %s\n", doser ? "ON" : "OFF");
  }
  mb.Ists(DI_PUMP_RUNNING, pumpPin);    // exactly what the pins are doing
  mb.Ists(DI_DOSER_RUNNING, doserPin);
}

// SAFE_MODE: remote writes to coils 0-2 and holding 0-4 are silently ignored (the client still
// gets a normal reply, as on the software station). Done by putting the values back after
// mb.task() instead of with onSet callbacks: the library answers a refused single-coil write
// with an exception, and callbacks would also fire for the station's own writes.
void modbusTask() {
  bool locked = mb.Coil(C_SAFE_MODE);
  bool coils[3];
  uint16_t hregs[5];
  if (locked) {
    for (int i = 0; i < 3; i++) coils[i] = mb.Coil(i);
    for (int i = 0; i < 5; i++) hregs[i] = mb.Hreg(i);
  }
  mb.task();  // non-blocking; returns after at most ~100 ms of work
  if (locked) {
    for (int i = 0; i < 3; i++) if (mb.Coil(i) != coils[i]) mb.Coil(i, coils[i]);
    for (int i = 0; i < 5; i++) if (mb.Hreg(i) != hregs[i]) mb.Hreg(i, hregs[i]);
  }
}

// Called every loop: derived read-only registers.
void modbusUpdate() {
  mb.Ireg(IR_LOGIC_HASH, logicHash());
  mb.Ireg(IR_UPTIME_S, (uint16_t)((millis() / 1000UL) & 0xFFFF));
}

// ---------------- step 4: inputs ----------------
struct Debounced {
  bool state = false, last = false;
  uint32_t tChange = 0;
  bool update(bool raw, uint32_t now) {
    if (raw != last) { last = raw; tChange = now; }
    if (now - tChange >= DEBOUNCE_MS) state = raw;
    return state;
  }
};
Debounced dbButton, dbIr;
bool presence = false;
uint32_t presenceUntil = 0, tAdc = 0, tLdrLog = 0, tDht = 0, tDhtStart = 0;
float potEma = -1, ldrEma = -1;
int potRegLast = -1, potLogged = -1;
bool cabinetOpen = false, dhtBusy = false;
uint8_t dhtFails = 0;

// DHT11 single-wire read. Timing-critical part (about 4 ms) runs with interrupts off on this core.
// Duration (us) the pin stays at `level`; -1 on timeout. 0 is valid (pin was already at the other level).
static int32_t pulseUs(int level, uint32_t timeoutUs) {
  uint32_t t0 = micros();
  while (digitalRead(PIN_DHT) == level)
    if (micros() - t0 > timeoutUs) return -1;
  return micros() - t0;
}

int dhtStage = 0;  // where the last capture failed (0 = ok), for the serial log
bool dhtCapture(uint8_t d[5]) {
  static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
  int32_t lo[40], hi[40];
  bool ok = true;
  pinMode(PIN_DHT, INPUT_PULLUP);  // start pulse was already sent, release the line
  portENTER_CRITICAL(&mux);
  dhtStage = 1;
  pulseUs(LOW, 200);                    // let the line rise after release (may take a few us)
  if (pulseUs(HIGH, 100) < 0) ok = false;  // then the sensor pulls it low to answer
  if (ok) dhtStage = 2;
  if (ok && pulseUs(LOW, 120) < 0) ok = false;   // sensor ack low
  if (ok) dhtStage = 3;
  if (ok && pulseUs(HIGH, 120) < 0) ok = false;  // sensor ack high
  for (int i = 0; ok && i < 40; i++) {
    lo[i] = pulseUs(LOW, 100);
    hi[i] = pulseUs(HIGH, 120);
    if (lo[i] < 0 || hi[i] < 0) { ok = false; dhtStage = 10 + i; }
  }
  portEXIT_CRITICAL(&mux);
  if (!ok) return false;
  dhtStage = 0;
  for (int i = 0; i < 5; i++) d[i] = 0;
  for (int i = 0; i < 40; i++) d[i / 8] = (d[i / 8] << 1) | (hi[i] > lo[i] ? 1 : 0);
  return d[4] == (uint8_t)(d[0] + d[1] + d[2] + d[3]);
}

void inputsInit() {
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_IR, INPUT);
  pinMode(PIN_DHT, INPUT_PULLUP);
  analogReadResolution(12);
  tDht = millis() - DHT_PERIOD_MS + 1500;  // first DHT read ~1.5 s after boot
}

// Non-blocking (the DHT read blocks about 4 ms every 2.5 s). Call every loop.
void inputsUpdate() {
  uint32_t now = millis();

  // Button (pressed = pin LOW) and IR obstacle (module output LOW = obstacle), debounced.
  bool btn = dbButton.update(digitalRead(PIN_BUTTON) == LOW, now);
  bool ir = dbIr.update(digitalRead(PIN_IR) == LOW, now);
  if (btn != mb.Ists(DI_LOCAL_BUTTON)) Serial.printf("button %s\n", btn ? "DOWN" : "UP");
  if (ir != mb.Ists(DI_HIGH_LEVEL_SWITCH)) Serial.printf("IR %s\n", ir ? "obstacle" : "clear");
  // Local operator button: hold 3 s = toggle SAFE_MODE; short press = acknowledge (clear) ALARM.
  static uint32_t tBtnDown = 0;
  static bool btnLongDone = false;
  bool btnWas = mb.Ists(DI_LOCAL_BUTTON);
  if (btn && !btnWas) { tBtnDown = now; btnLongDone = false; }
  if (btn && !btnLongDone && now - tBtnDown >= BUTTON_LONG_MS) {
    btnLongDone = true;
    mb.Coil(C_SAFE_MODE, !mb.Coil(C_SAFE_MODE));
    Serial.printf("button long press: SAFE_MODE = %d\n", mb.Coil(C_SAFE_MODE));
  }
  if (!btn && btnWas && !btnLongDone && mb.Coil(C_ALARM)) {
    mb.Coil(C_ALARM, false);
    Serial.println("button short press: ALARM acknowledged");
  }
  mb.Ists(DI_LOCAL_BUTTON, btn);
  mb.Ists(DI_HIGH_LEVEL_SWITCH, ir);

  // PIR: ignored during warm-up; each motion restarts a 60 s latch.
  static bool warmLogged = false;
  if (now >= PIR_WARMUP_MS) {
    if (!warmLogged) { warmLogged = true; Serial.println("PIR warm-up over"); }
    if (digitalRead(PIN_PIR)) {
      if (!presence) Serial.println("PIR motion: PRESENCE = 1 for 60 s");
      presence = true;
      presenceUntil = now + PIR_LATCH_MS;
    }
  }
  if (presence && (int32_t)(now - presenceUntil) >= 0) { presence = false; Serial.println("PRESENCE = 0"); }
  mb.Ists(DI_PRESENCE, presence);

  // Pot and photoresistor on ADC1, sampled every 20 ms and smoothed.
  if (now - tAdc >= 20) {
    tAdc = now;
    int pr = analogRead(PIN_POT), lr = analogRead(PIN_LDR);
    potEma = potEma < 0 ? pr : potEma + (pr - potEma) * 0.125f;
    ldrEma = ldrEma < 0 ? lr : ldrEma + (lr - ldrEma) * 0.25f;
    int demand = constrain((int)((potEma - POT_RAW_MIN) * 1000.0f / (POT_RAW_MAX - POT_RAW_MIN)), 0, 1000);
    if (potRegLast < 0 || abs(demand - potRegLast) >= 3) {  // 0.3 % dead band keeps the register quiet
      potRegLast = demand;
      mb.Ireg(IR_DEMAND_PCT, demand);
      if (abs(demand - potLogged) >= 20) { potLogged = demand; Serial.printf("pot raw %d -> DEMAND_PCT %d.%d %%\n", pr, demand / 10, demand % 10); }
    }
    int light = LDR_LIGHT_IS_HIGH ? (int)ldrEma : 4095 - (int)ldrEma;
    bool wasOpen = cabinetOpen;
    if (!cabinetOpen && light > LDR_OPEN_ABOVE) cabinetOpen = true;
    else if (cabinetOpen && light < LDR_CLOSED_BELOW) cabinetOpen = false;
    if (cabinetOpen != wasOpen) Serial.printf("cabinet %s (ldr raw %d)\n", cabinetOpen ? "OPEN" : "CLOSED", (int)ldrEma);
    mb.Ists(DI_CABINET_OPEN, cabinetOpen);
    if (now - tLdrLog >= 2000) { tLdrLog = now; Serial.printf("ldr raw %d (open above %d, closed below %d, light-is-high=%d)\n", (int)ldrEma, LDR_OPEN_ABOVE, LDR_CLOSED_BELOW, LDR_LIGHT_IS_HIGH); }
  }

  // DHT11: start pulse (line low 20 ms, non-blocking), then capture.
  if (!dhtBusy && now - tDht >= DHT_PERIOD_MS) {
    pinMode(PIN_DHT, OUTPUT);
    digitalWrite(PIN_DHT, LOW);
    tDhtStart = now;
    dhtBusy = true;
  } else if (dhtBusy && now - tDhtStart >= 20) {
    dhtBusy = false;
    tDht = now;
    uint8_t d[5];
    if (dhtCapture(d)) {
      dhtFails = 0;
      int t = (d[2] & 0x7F) * 10 + d[3] % 10, h = d[0] * 10 + d[1] % 10;
      if (d[2] & 0x80) t = 0;  // DHT11 range is 0-50 C
      mb.Ireg(IR_CABINET_TEMP_C, t);
      mb.Ireg(IR_CABINET_HUMIDITY, h);
      Serial.printf("DHT11 %d.%d C  %d.%d %%RH\n", t / 10, t % 10, h / 10, h % 10);
    } else if (dhtFails < 255 && ++dhtFails <= 3) {
      Serial.printf("DHT11 read failed at stage %d (1-3 = no answer: check D4/VCC/GND; 10+ = bit error), idle level %d\n", dhtStage, digitalRead(PIN_DHT));
    }
  }
}

Adafruit_SSD1306 oled(128, 64, &Wire, -1);
bool oledOk = false;

uint32_t tBlink = 0, tWifiTry = 0, tScreen = 0;
bool wasConnected = false;

// Returns the first address that ACKs out of 0x3C / 0x3D, or 0 if none.
uint8_t scanOled() {
  uint8_t found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("I2C device at 0x%02X\n", a);
      if (!found && (a == 0x3C || a == 0x3D)) found = a;
    }
  }
  return found;
}

// ---------------- step 6: screen, LEDs, buzzer ----------------
// Screen layout (128x64): tank bar on the left, big level and chlorine, pump/doser blocks
// (filled = running), mode + IP at the bottom. ALARM flashes the whole screen inverted.
void drawBlock(int x, int y, const char *label, bool on) {
  if (on) {
    oled.fillRect(x, y, 48, 13, SSD1306_WHITE);
    oled.setTextColor(SSD1306_BLACK);
  } else {
    oled.drawRect(x, y, 48, 13, SSD1306_WHITE);
    oled.setTextColor(SSD1306_WHITE);
  }
  oled.setTextSize(1);
  oled.setCursor(x + (48 - 6 * (int)strlen(label)) / 2, y + 3);
  oled.print(label);
  oled.setTextColor(SSD1306_WHITE);
}

void drawScreen() {
  if (!oledOk) return;
  bool up = WiFi.status() == WL_CONNECTED;
  uint16_t level = mb.Hreg(HR_TANK_LEVEL_PCT);  // x10
  uint16_t cl = mb.Hreg(HR_CHLORINE_MGL);       // x100
  bool safe = mb.Coil(C_SAFE_MODE), alarm = mb.Coil(C_ALARM), autoMode = mb.Coil(C_AUTO_MODE);

  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);

  // Tank bar: 20 px wide, full height, filled from the bottom.
  int fill = constrain((int)level, 0, 1000) * 60 / 1000;
  oled.drawRect(0, 0, 20, 64, SSD1306_WHITE);
  oled.fillRect(2, 62 - fill, 16, fill, SSD1306_WHITE);

  // Level, big.
  oled.setTextSize(2);
  oled.setCursor(26, 0);
  oled.printf("%u.%u%%", level / 10, level % 10);
  // Chlorine, big, with small unit.
  oled.setCursor(26, 18);
  oled.printf("%u.%02u", cl / 100, cl % 100);
  oled.setTextSize(1);
  oled.setCursor(oled.getCursorX() + 2, 25);
  oled.print("mg/L");

  drawBlock(26, 36, "PUMP", mb.Ists(DI_PUMP_RUNNING));
  drawBlock(78, 36, "DOSE", mb.Ists(DI_DOSER_RUNNING));

  // Bottom line: mode + IP.
  oled.setCursor(26, 55);
  oled.print(alarm ? "ALARM" : safe ? "SAFE " : autoMode ? "AUTO " : "MAN  ");
  oled.setCursor(58, 55);
  if (up) oled.print(WiFi.localIP()); else oled.print("no WiFi");
  oled.display();
}

void outputsInit() {
  for (uint8_t p : {PIN_LED_GREEN, PIN_LED_YELLOW, PIN_LED_RED, PIN_BUZZER}) {
    digitalWrite(p, LOW);
    pinMode(p, OUTPUT);
    digitalWrite(p, LOW);
  }
}

// Green = normal, yellow = safe mode, red = ALARM coil. The buzzer sounds only for the first
// ALARM_SOUND_MS after ALARM comes on; the red LED and the flashing screen stay until ALARM is
// cleared (short button press, or a write of 0). Call every loop.
void outputsUpdate() {
  uint32_t now = millis();
  bool safe = mb.Coil(C_SAFE_MODE), alarm = mb.Coil(C_ALARM);
  digitalWrite(PIN_LED_GREEN, !safe && !alarm);
  digitalWrite(PIN_LED_YELLOW, safe);
  digitalWrite(PIN_LED_RED, alarm);
  // Active buzzer: three 150 ms beeps, then quiet, repeating every 1.6 s, for ALARM_SOUND_MS.
  static bool alarmWas = false;
  static uint32_t tAlarmOn = 0;
  if (alarm && !alarmWas) tAlarmOn = now;
  alarmWas = alarm;
  bool beep = false;
  if (alarm && now - tAlarmOn < ALARM_SOUND_MS) {
    uint32_t ph = (now - tAlarmOn) % 1600;
    beep = ph < 900 && (ph % 300) < 150;
  }
  digitalWrite(PIN_BUZZER, beep);
  // Screen flashes inverted once a second while alarmed.
  static bool inverted = false;
  bool wantInv = alarm && (now / 500) % 2;
  if (oledOk && wantInv != inverted) { inverted = wantInv; oled.invertDisplay(inverted); }
}

void setup() {
  relaysOffAtBoot();  // first thing, before serial, OLED or Wi-Fi
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  outputsInit();
  Serial.println("\nBackflow station, steps 1-6");

  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);  // full frame in ~25 ms instead of ~100 ms
  uint8_t addr = scanOled();
  if (addr) {
    oledOk = oled.begin(SSD1306_SWITCHCAPVCC, addr);
    Serial.printf("OLED at 0x%02X: %s\n", addr, oledOk ? "ok" : "begin failed");
  } else {
    Serial.println("OLED not found at 0x3C/0x3D (check SDA=21 SCL=22 VCC=3V3 GND)");
  }

  // CRC self-check from the spec: "123456789" -> 0x4B37.
  Serial.printf("CRC16 self-check: 0x%04X (want 0x4B37)\n", crc16((const uint8_t *)"123456789", 9));
  modbusDefine();
  inputsInit();
  modbusUpdate();
  Serial.printf("LOGIC_HASH at defaults: %u\n", mb.Ireg(IR_LOGIC_HASH));

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // modem sleep adds latency to every Modbus reply
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  tWifiTry = millis();
  Serial.printf("Joining \"%s\" ...\n", WIFI_SSID);
  drawScreen();
}

void loop() {
  uint32_t now = millis();
  bool up = WiFi.status() == WL_CONNECTED;

  // Heartbeat: slow blink when connected, fast while not.
  if (now - tBlink >= (up ? 500u : 100u)) {
    tBlink = now;
    digitalWrite(PIN_LED, !digitalRead(PIN_LED));
  }

  if (up != wasConnected) {
    wasConnected = up;
    if (up) {
      Serial.print("WiFi connected, IP ");
      Serial.print(WiFi.localIP());
      Serial.printf("  RSSI %d dBm\n", WiFi.RSSI());
      if (!mbStarted) {  // server() allocates a listener: call it exactly once
        mb.server(502);
        mbStarted = true;
        Serial.println("Modbus TCP server listening on port 502");
      }
    } else {
      Serial.println("WiFi lost");
      tWifiTry = now;
    }
    tScreen = now - 1000;  // redraw now
  }

  // Belt and braces on top of auto-reconnect: retry every 10 s while down.
  if (!up && now - tWifiTry >= 10000) {
    tWifiTry = now;
    Serial.println("WiFi retry");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }

  if (mbStarted) modbusTask();
  inputsUpdate();
  controlApply();            // right after task(), so a coil write reaches the pin in the same loop
  modbusUpdate();            // every loop, so LOGIC_HASH never lags a write
  outputsUpdate();

  if (now - tScreen >= 500) {
    tScreen = now;
    drawScreen();
  }
}
