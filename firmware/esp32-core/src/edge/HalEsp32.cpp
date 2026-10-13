#ifdef ARDUINO
#include <Arduino.h>
#include <math.h>
#include <stdarg.h>
#include "Hal.h"

namespace edge {

class Esp32Hal : public Hal {
 public:
  void pinOut(int pin, bool initialHigh) override {
    digitalWrite(pin, initialHigh ? HIGH : LOW);  // latch the level before the pin becomes an output
    pinMode(pin, OUTPUT);
  }
  void pinIn(int pin, bool pullup) override { pinMode(pin, pullup ? INPUT_PULLUP : INPUT); }
  void write(int pin, bool high) override { digitalWrite(pin, high ? HIGH : LOW); }
  bool read(int pin) override { return digitalRead(pin) == HIGH; }
  int adc(int pin) override {
    if (!adcInit_) { analogReadResolution(12); adcInit_ = true; }
    return analogRead(pin);
  }
  int pwmAttach(int pin, uint32_t freqHz, int bits) override {
    if (next_ >= 16) return -1;
    // LEDC timer is clocked at 80 MHz: resolution must satisfy freq * 2^bits <= 80e6.
    int maxBits = (int)floorf(log2f(80000000.0f / (float)freqHz));
    if (bits > maxBits) bits = maxBits;
    if (bits < 1) return -1;
    int ch = next_++;
    if (ledcSetup(ch, freqHz, bits) == 0) return -1;
    ledcAttachPin(pin, ch);
    bitsOf_[ch] = bits;
    return ch;
  }
  uint32_t pwmMaxDuty(int ch) override { return (1u << bitsOf_[ch]) - 1; }
  void pwmWrite(int ch, uint32_t duty) override { ledcWrite(ch, duty); }

 private:
  bool adcInit_ = false;
  int next_ = 0;
  int bitsOf_[16] = {0};
};

Hal* createDefaultHal() { static Esp32Hal h; return &h; }

void logf(const char* fmt, ...) {
  char buf[200];
  va_list ap; va_start(ap, fmt); vsnprintf(buf, sizeof buf, fmt, ap); va_end(ap);
  Serial.print("[edge] ");
  Serial.println(buf);
}

}  // namespace edge
#else
#include "FakeHal.h"
namespace edge {
Hal* createDefaultHal() { static FakeHal h; return &h; }
}  // namespace edge
#endif
