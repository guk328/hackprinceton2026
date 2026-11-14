// Backflow station firmware. Step 1: blink, serial, Wi-Fi, IP on the OLED.
#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#include "secrets.h"  // WIFI_SSID, WIFI_PASS (git-ignored, see secrets.h.example)

// ---- pins (keep firmware/README.md in sync) ----
const uint8_t PIN_SDA = 21;
const uint8_t PIN_SCL = 22;
const uint8_t PIN_LED = LED_BUILTIN;  // GPIO 2, on-board blue LED

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

  WiFi.mode(WIFI_STA);
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

  if (now - tScreen >= 1000) {
    tScreen = now;
    drawScreen();
  }
}
