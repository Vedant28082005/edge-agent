// Host-side fake used by the native tests (and by announce generation, which never touches pins).
#pragma once
#include <functional>
#include <map>
#include <vector>
#include "Hal.h"

namespace edge {

class FakeHal : public Hal {
 public:
  struct Write { int pin; bool high; };
  std::map<int, bool> level;
  std::map<int, bool> isOutput;
  std::map<int, int> adcValue;
  std::map<int, uint32_t> pwmDuty;       // by channel
  std::vector<Write> writes;
  std::function<void(FakeHal&)> afterWrite;  // invariant hook, called after every write
  int nextChannel = 0;
  uint32_t maxDuty = 255;

  void pinOut(int pin, bool initialHigh) override { isOutput[pin] = true; level[pin] = initialHigh; }
  void pinIn(int pin, bool pullup) override { isOutput[pin] = false; if (!level.count(pin)) level[pin] = pullup; }
  void write(int pin, bool high) override {
    level[pin] = high;
    writes.push_back({pin, high});
    if (afterWrite) afterWrite(*this);
  }
  bool read(int pin) override { auto it = level.find(pin); return it != level.end() && it->second; }
  int adc(int pin) override { auto it = adcValue.find(pin); return it == adcValue.end() ? 0 : it->second; }
  int pwmAttach(int, uint32_t, int) override { pwmDuty[nextChannel] = 0; return nextChannel++; }
  uint32_t pwmMaxDuty(int) override { return maxDuty; }
  void pwmWrite(int ch, uint32_t duty) override { pwmDuty[ch] = duty; }

  bool high(int pin) const { auto it = level.find(pin); return it != level.end() && it->second; }
};

}  // namespace edge
