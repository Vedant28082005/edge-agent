// Dimmable light on the ESP32 DevKit's onboard LED (GPIO2, active high). No wiring needed.
// PWM on GPIO2 gives power + brightness. No local button (GPIO0 is the BOOT pin).
#ifdef ARDUINO
#include <Arduino.h>
#endif
#include <EdgeNode.h>

using namespace edge;

EdgeNode node;

void declareDevice() {
  static PwmChannel led(2, 5000);
  node.hw("esp32-light");
  node.endpoint("l1", LIGHT)
      .boolCap("power", led.power()).bootValue(false).failsafeValue(false)
      .intCap("brightness", 0, 100, led.level()).bootValue(50);
}

#ifndef EDGE_NATIVE_TEST
void setup() {
  declareDevice();
  node.begin();
}
void loop() { node.loop(); }
#endif
