// Relay bench test (not the station firmware). Toggles IN1 (GPIO 26) every 2 s and IN2
// (GPIO 27) one second later, and says on serial what the relay should be doing.
//
// Send one character on serial (115200) to change how "off" is driven:
//   h = off is OUTPUT HIGH (3.3 V)      <- default, the normal way
//   z = off is INPUT (pin floating)     <- for modules that will not release at 3.3 V
// "on" is always OUTPUT LOW (these modules are active LOW).
#include <Arduino.h>

const uint8_t PIN_IN1 = 26, PIN_IN2 = 27;
bool offIsHiZ = false;
bool on1 = false, on2 = false;
uint32_t t1 = 0, t2 = 1000;

void drive(uint8_t pin, bool on) {
  if (on) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);
  } else if (offIsHiZ) {
    pinMode(pin, INPUT);
  } else {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, HIGH);
  }
}

void setup() {
  Serial.begin(115200);
  drive(PIN_IN1, false);
  drive(PIN_IN2, false);
  Serial.println("\nRelay test: h = off is HIGH, z = off is floating");
}

void loop() {
  uint32_t now = millis();
  if (Serial.available()) {
    char c = Serial.read();
    if (c == 'h' || c == 'z') {
      offIsHiZ = (c == 'z');
      Serial.printf("off mode: %s\n", offIsHiZ ? "INPUT (floating)" : "OUTPUT HIGH");
    }
  }
  if (now - t1 >= 2000) {
    t1 = now;
    on1 = !on1;
    drive(PIN_IN1, on1);
    Serial.printf("IN1 (GPIO 26) -> relay 1 should be %s\n", on1 ? "ON  (click, LED on)" : "OFF (click, LED off)");
  }
  if (now - t2 >= 2000) {
    t2 = now;
    on2 = !on2;
    drive(PIN_IN2, on2);
    Serial.printf("IN2 (GPIO 27) -> relay 2 should be %s\n", on2 ? "ON" : "OFF");
  }
}
