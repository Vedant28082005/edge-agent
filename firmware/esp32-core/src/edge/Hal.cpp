#include "Hal.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

namespace edge {

static Hal* g_override = nullptr;
Hal* createDefaultHal();  // HalEsp32.cpp on the board, FakeHal on the host

Hal& hal() {
  if (g_override) return *g_override;
  static Hal* def = createDefaultHal();
  return *def;
}
void setHal(Hal* h) { g_override = h; }

// Classic ESP32: 6-11 are the flash, 34-39 are input-only, 0/2/5/12/15 are strapping pins
// (allowed, but the README tells you to think about them for relays).
bool pinValidOutput(int pin) {
  if (pin < 0 || pin > 33) return false;
  if (pin >= 6 && pin <= 11) return false;
  return true;
}
bool pinValidInput(int pin) {
  if (pin < 0 || pin > 39) return false;
  if (pin >= 6 && pin <= 11) return false;
  return true;
}
// ADC2 shares hardware with Wi-Fi and returns garbage while Wi-Fi is on, so analog inputs must be ADC1.
bool pinIsAdc1(int pin) { return pin >= 32 && pin <= 39; }

#ifndef ARDUINO
void logf(const char* fmt, ...) {
  if (!getenv("EDGE_LOG")) return;
  va_list ap; va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap); fputc('\n', stderr);
}
#endif

}  // namespace edge
