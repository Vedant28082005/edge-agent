// Door/room sensor node: reed switch (GPIO27 to GND), PIR (GPIO14, active-high output) and an LDR divider on
// ADC1 pin GPIO34 (10 k fixed resistor to 3V3, LDR to GND). Digital inputs report changes immediately.
#ifdef ARDUINO
#include <Arduino.h>
#endif
#include <EdgeNode.h>

using namespace edge;

EdgeNode node;

void declareDevice() {
  node.hw("esp32-sensors");
  node.endpoint("door", SENSOR).enumSensor("contact", {"open", "closed"}, ReedIn(27), 60);
  node.endpoint("hall", SENSOR).boolSensor("motion", PirIn(14), 60);
  node.endpoint("lux", SENSOR).floatSensor("illuminance", LdrIn(34, 10000.0f, /*ldrToGnd=*/true), 30);
}

#ifndef EDGE_NATIVE_TEST
void setup() {
  declareDevice();
  node.begin();
}
void loop() { node.loop(); }
#endif
