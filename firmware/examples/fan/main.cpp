// Fan with speed: relay on GPIO26 switches the fan supply, PWM on GPIO27 (25 kHz) sets the speed of a 4-wire
// DC fan / MOSFET driver. Buttons: GPIO33 toggles power, GPIO32 cycles speed 1..5.
//
// Speed 1..5 maps to 20 %..100 % duty. Check that your fan actually starts at 20 %; if not raise the lower
// bound by declaring intCap("speed", 2, 5, ...). PWM cannot control an AC mains fan.
#ifdef ARDUINO
#include <Arduino.h>
#endif
#include <EdgeNode.h>

using namespace edge;

EdgeNode node;

void declareDevice() {
  node.hw("esp32-fan");
  node.endpoint("ch1", FAN)
      .boolCap("power", RelayOut(26)).bootValue(false).failsafeValue(false).maxOnS(8 * 3600)
      .intCap("speed", 1, 5, PwmOut(27, 25000)).bootValue(1)
      .localToggle("power", 33)
      .localCycle("speed", 32);
}

#ifndef EDGE_NATIVE_TEST
void setup() {
  declareDevice();
  node.begin();
}
void loop() { node.loop(); }
#endif
