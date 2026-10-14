// DHT22 temperature + humidity sensor on GPIO4 (10 k pull-up to 3V3 on the data line), read every 30 s.
#ifdef ARDUINO
#include <Arduino.h>
#endif
#include <EdgeNode.h>

using namespace edge;

EdgeNode node;

void declareDevice() {
  static DhtSensor dht(4, kDht22);
  node.hw("esp32-dht");
  node.endpoint("t1", SENSOR)
      .floatSensor("temperature", dht.temperature(), 30)
      .floatSensor("humidity", dht.humidity(), 30);
}

#ifndef EDGE_NATIVE_TEST
void setup() {
  declareDevice();
  node.begin();
}
void loop() { node.loop(); }
#endif
