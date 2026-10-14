// Thin hardware abstraction. Drivers only talk to this, so all pin-level behavior
// (relay polarity, curtain interlock, PWM duty) is testable on the host with FakeHal.
#pragma once
#include <stdint.h>

namespace edge {

class Hal {
 public:
  virtual ~Hal() {}
  virtual void pinOut(int pin, bool initialHigh) = 0;  // configure as output, level set before enabling
  virtual void pinIn(int pin, bool pullup) = 0;
  virtual void write(int pin, bool high) = 0;
  virtual bool read(int pin) = 0;
  virtual int adc(int pin) = 0;                                    // 0..adcMax()
  virtual int adcMax() { return 4095; }
  virtual int pwmAttach(int pin, uint32_t freqHz, int bits) = 0;   // channel, or -1
  virtual uint32_t pwmMaxDuty(int channel) = 0;
  virtual void pwmWrite(int channel, uint32_t duty) = 0;
};

Hal& hal();
void setHal(Hal* h);  // tests; pass nullptr to restore the default

// Pin sanity rules for classic ESP32 (esp32dev). Used when validating a declaration.
bool pinValidOutput(int pin);
bool pinValidInput(int pin);
bool pinIsAdc1(int pin);

void logf(const char* fmt, ...) __attribute__((format(printf, 1, 2)));

}  // namespace edge
