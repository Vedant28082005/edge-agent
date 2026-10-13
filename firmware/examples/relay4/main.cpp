// 4-channel active-low relay board. Relays on GPIO16/17/18/19, local buttons on GPIO32/33/25/26.
// r1 has an 1 h on-time limit (e.g. a heater), r3 comes up ON after a power cut and stays on if the hub is gone.
#ifdef ARDUINO
#include <Arduino.h>
#endif
#include <EdgeNode.h>

using namespace edge;

EdgeNode node;

void declareDevice() {
  node.hw("esp32-4relay");
  node.endpoint("r1", OUTLET)
      .boolCap("power", RelayOut(16, /*activeHigh=*/false)).bootValue(false).failsafeValue(false).maxOnS(3600)
      .localToggle("power", 32);
  node.endpoint("r2", OUTLET)
      .boolCap("power", RelayOut(17, false)).bootValue(false).failsafeValue(false)
      .localToggle("power", 33);
  node.endpoint("r3", LIGHT)
      .boolCap("power", RelayOut(18, false)).bootValue(true).failsafeValue(true)
      .localToggle("power", 25);
  node.endpoint("r4", OUTLET)
      .boolCap("power", RelayOut(19, false)).bootValue(false).failsafeValue(false)
      .localToggle("power", 26);
}

#ifndef EDGE_NATIVE_TEST
void setup() {
  declareDevice();
  node.begin();
}
void loop() { node.loop(); }
#endif
