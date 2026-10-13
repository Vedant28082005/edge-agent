// Curtain motor: two relays (open = GPIO26, close = GPIO27) drive a reversible AC/DC motor through an
// interlocked pair. The firmware never energises both and waits 500 ms between directions.
//
// Position 0 = closed, 100 = open. Travel times below are placeholders: measure yours with a stopwatch
// (full close and full open) and enter them. With limit switches use .limitSwitches(openPin, closedPin)
// and the times are learned from full runs.
//
// boot_value is the ASSUMED position at power-up (the curtain does not move at boot).
#ifdef ARDUINO
#include <Arduino.h>
#endif
#include <EdgeNode.h>

using namespace edge;

EdgeNode node;

void declareDevice() {
  node.hw("esp32-curtain");
  node.endpoint("c1", COVER)
      .intCap("position", 0, 100,
              CurtainMotor(26, 27)
                  .travelTime(20000 /*open ms*/, 20000 /*close ms*/)
                  .maxRun(30000)
                  .deadTime(500))
      .bootValue(0);
}

#ifndef EDGE_NATIVE_TEST
void setup() {
  declareDevice();
  node.begin();
}
void loop() { node.loop(); }
#endif
