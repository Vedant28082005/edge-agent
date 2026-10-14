// Dimmable LED strip: one MOSFET on GPIO25 driven by PWM (power gate + brightness), local button on GPIO32.
//
//   ESP32 GPIO25 --[gate resistor]--> logic-level N-MOSFET --> LED strip (12/24 V DC, NOT mains)
//   GPIO32 --- pushbutton --- GND   (internal pull-up, toggles power, works offline)
//
// PWM here is for low-voltage DC loads only. It cannot dim a mains lamp.
#ifdef ARDUINO
#include <Arduino.h>
#endif
#include <EdgeNode.h>

using namespace edge;

EdgeNode node;

void declareDevice() {
  static PwmChannel strip(25, 5000);
  node.hw("esp32-light");
  node.endpoint("l1", LIGHT)
      .boolCap("power", strip.power()).bootValue(false).failsafeValue(false)
      .intCap("brightness", 0, 100, strip.level()).bootValue(50)
      .localToggle("power", 32);
}

#ifndef EDGE_NATIVE_TEST
void setup() {
  declareDevice();
  node.begin();
}
void loop() { node.loop(); }
#endif
