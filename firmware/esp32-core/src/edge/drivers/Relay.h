// Relay / digital on-off output. Active-high or active-low.
#pragma once
#include "../Drivers.h"

namespace edge {

class RelayOut : public Actuator {
 public:
  explicit RelayOut(int pin, bool activeHigh = true) : pin_(pin), activeHigh_(activeHigh) {}
  bool validate(std::string& err) const override {
    if (!pinValidOutput(pin_)) { err = "RelayOut: pin " + std::to_string(pin_) + " is not a usable output"; return false; }
    return true;
  }
  bool begin() override {
    hal().pinOut(pin_, !activeHigh_);  // inactive level first: no glitch at start-up
    return true;
  }
  ApplyResult apply(const Value& v, uint32_t) override {
    hal().write(pin_, v.b ? activeHigh_ : !activeHigh_);
    return ApplyResult::Ok;
  }

 private:
  int pin_;
  bool activeHigh_;
};

}  // namespace edge
