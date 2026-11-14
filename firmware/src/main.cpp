// Backflow station firmware. Steps 1-2: Wi-Fi + OLED, Modbus TCP server with the register map.
#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <ModbusIP_ESP8266.h>

#include "secrets.h"  // WIFI_SSID, WIFI_PASS (git-ignored, see secrets.h.example)

// ---- pins (keep firmware/README.md in sync) ----
const uint8_t PIN_SDA = 21;
const uint8_t PIN_SCL = 22;
const uint8_t PIN_LED = LED_BUILTIN;  // GPIO 2, on-board blue LED


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

// Called every loop: derived read-only registers.
void modbusUpdate() {
  mb.Ireg(IR_LOGIC_HASH, logicHash());
  mb.Ireg(IR_UPTIME_S, (uint16_t)((millis() / 1000UL) & 0xFFFF));
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

void drawScreen() {
  if (!oledOk) return;
  bool up = WiFi.status() == WL_CONNECTED;
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(2);
  oled.setCursor(0, 0);
  oled.print("BACKFLOW");
  oled.setTextSize(1);
  oled.setCursor(0, 24);
  oled.print(up ? "WiFi OK  " : "WiFi ... ");
  oled.print(WIFI_SSID);
  oled.setCursor(0, 40);
  oled.print("IP ");
  if (up) oled.print(WiFi.localIP()); else oled.print("-");
  oled.setCursor(0, 54);
  oled.printf("up %lus", (unsigned long)(millis() / 1000));
  oled.display();
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  Serial.println("\nBackflow station, step 1");

  Wire.begin(PIN_SDA, PIN_SCL);
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

  if (mbStarted) mb.task();  // non-blocking; returns after at most ~100 ms of work
  modbusUpdate();            // every loop, so LOGIC_HASH never lags a write

  if (now - tScreen >= 1000) {
    tScreen = now;
    drawScreen();
  }
}
