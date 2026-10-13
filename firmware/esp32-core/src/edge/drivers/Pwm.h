// PWM output through the LEDC peripheral.
//
// Intended for DC fans and LED strips (low-voltage, MOSFET-driven) ONLY.
// It CANNOT dim mains loads: phase-cut mains dimming needs a zero-cross detector and a TRIAC
// dimmer module, which this driver does not implement. Never connect a mains lamp or fan to a PWM pin.
#pragma once
#include <memory>
#include "../Drivers.h"

namespace edge {

namespace detail {
struct PwmShared {
  int pin = -1;
  uint32_t freq = 1000;
  int channel = -1;
  bool power = true;     // gate (only meaningful when a PwmChannel power facet is used)
  float fraction = 0.0f; // 0..1 from the level facet
  bool attach() {
    if (channel >= 0) return true;
    channel = hal().pwmAttach(pin, freq, 12);
    return channel >= 0;
  }
  void write() {
    if (channel < 0) return;
    float f = power ? fraction : 0.0f;
    if (f < 0) f = 0;
    if (f > 1) f = 1;
    hal().pwmWrite(channel, (uint32_t)(f * (float)hal().pwmMaxDuty(channel) + 0.5f));
  }
};
}  // namespace detail

// Level facet: value v in [lo,hi] maps to duty = v / hi (so 0 is off). Standalone: PwmOut(27, 25000).
class PwmOut : public Actuator {
 public:
  PwmOut(int pin, uint32_t freqHz) : s_(std::make_shared<detail::PwmShared>()) { s_->pin = pin; s_->freq = freqHz; }
  explicit PwmOut(std::shared_ptr<detail::PwmShared> s) : s_(std::move(s)) {}
  bool validate(std::string& err) const override {
    if (!pinValidOutput(s_->pin)) { err = "PwmOut: pin " + std::to_string(s_->pin) + " is not a usable output"; return false; }
    if (s_->freq < 1 || s_->freq > 40000000u) { err = "PwmOut: bad frequency"; return false; }
    return true;
  }
  bool begin() override {
    hal().pinOut(s_->pin, false);
    if (!s_->attach()) return false;
    s_->write();
    return true;
  }
  void bind(const CapRange& r) override { hi_ = r.hi; }
  ApplyResult apply(const Value& v, uint32_t) override {
    s_->fraction = hi_ > 0 ? v.asFloat() / hi_ : 0.0f;
    s_->write();
    return ApplyResult::Ok;
  }

 private:
  std::shared_ptr<detail::PwmShared> s_;
  float hi_ = 1.0f;
};

// One MOSFET channel driven by two capabilities: power (gate) and brightness/speed (level).
//   PwmChannel led(25, 5000);
//   .boolCap("power", led.power()).intCap("brightness", 0, 100, led.level())
// Output duty = power ? level/max : 0.
class PwmChannel {
 public:
  PwmChannel(int pin, uint32_t freqHz) : s_(std::make_shared<detail::PwmShared>()) { s_->pin = pin; s_->freq = freqHz; s_->power = false; }
  class Power : public Actuator {
   public:
    explicit Power(std::shared_ptr<detail::PwmShared> s) : s_(std::move(s)) {}
    ApplyResult apply(const Value& v, uint32_t) override { s_->power = v.b; s_->write(); return ApplyResult::Ok; }
   private:
    std::shared_ptr<detail::PwmShared> s_;
  };
  Power power() const { return Power(s_); }
  PwmOut level() const { return PwmOut(s_); }

 private:
  std::shared_ptr<detail::PwmShared> s_;
};

}  // namespace edge
