// Digital inputs (PIR, reed switch) and analog inputs (LDR).
#pragma once
#include <math.h>
#include "../Drivers.h"

namespace edge {

// Debounced digital input. A level change is accepted only after it has been stable for debounceMs.
class DigitalIn : public SensorDriver {
 public:
  DigitalIn(int pin, bool pullup, bool activeLow, bool asContact, uint32_t debounceMs = 50)
      : pin_(pin), pullup_(pullup), activeLow_(activeLow), contact_(asContact), debounceMs_(debounceMs) {}
  bool validate(std::string& err) const override {
    if (!pinValidInput(pin_)) { err = "DigitalIn: pin " + std::to_string(pin_) + " is not a usable input"; return false; }
    if (pullup_ && pin_ >= 34) { err = "DigitalIn: pins 34-39 have no internal pull-up, use an external resistor"; return false; }
    return true;
  }
  bool begin() override { hal().pinIn(pin_, pullup_); return true; }
  uint32_t pollMs() const override { return 20; }
  bool read(uint32_t now, Value& out) override {
    bool raw = hal().read(pin_);
    if (!init_) { init_ = true; stable_ = raw; cand_ = raw; since_ = now; }
    if (raw != cand_) { cand_ = raw; since_ = now; }
    else if (cand_ != stable_ && elapsedMs(now, since_) >= debounceMs_) stable_ = cand_;
    bool active = activeLow_ ? !stable_ : stable_;
    out = contact_ ? Value::makeEnum(active ? 1 : 0)  // contact values are ["open","closed"]; active = closed
                   : Value::makeBool(active);
    return true;
  }

 private:
  int pin_;
  bool pullup_, activeLow_, contact_;
  uint32_t debounceMs_;
  bool init_ = false, stable_ = false, cand_ = false;
  uint32_t since_ = 0;
};

// PIR module: its output pin is driven (active high), no pull-up.  Capability: motion (bool).
class PirIn : public DigitalIn {
 public:
  explicit PirIn(int pin, uint32_t debounceMs = 50) : DigitalIn(pin, false, false, false, debounceMs) {}
};

// Reed switch wired between the pin and GND with the internal pull-up: closed = low.  Capability: contact (enum open/closed).
class ReedIn : public DigitalIn {
 public:
  explicit ReedIn(int pin, uint32_t debounceMs = 50) : DigitalIn(pin, true, true, true, debounceMs) {}
};

// Plain pushbutton/switch as a motion-style bool (active low with pull-up).
class SwitchIn : public DigitalIn {
 public:
  explicit SwitchIn(int pin, uint32_t debounceMs = 50) : DigitalIn(pin, true, true, false, debounceMs) {}
};

// LDR in a divider with a fixed resistor, read on an ADC1 pin. Output is illuminance in lux.
// The conversion lux = luxAt10k * (10000 / R_ldr)^(1/gamma) is a rough model: [est] until calibrated with a lux meter.
class LdrIn : public SensorDriver {
 public:
  // ldrToGnd=true: VCC -- Rfixed -- pin -- LDR -- GND.  false: VCC -- LDR -- pin -- Rfixed -- GND.
  LdrIn(int pin, float rFixedOhm = 10000.0f, bool ldrToGnd = true, float luxAt10k = 10.0f, float gamma = 0.7f)
      : pin_(pin), rFixed_(rFixedOhm), ldrToGnd_(ldrToGnd), lux10k_(luxAt10k), gamma_(gamma) {}
  bool validate(std::string& err) const override {
    if (!pinIsAdc1(pin_)) { err = "LdrIn: pin " + std::to_string(pin_) + " is not ADC1 (32-39); ADC2 does not work with Wi-Fi"; return false; }
    if (rFixed_ <= 0 || gamma_ <= 0 || lux10k_ <= 0) { err = "LdrIn: bad calibration constants"; return false; }
    return true;
  }
  bool begin() override { hal().pinIn(pin_, false); return true; }
  bool read(uint32_t, Value& out) override {
    long sum = 0;
    const int N = 16;
    for (int k = 0; k < N; k++) sum += hal().adc(pin_);
    float raw = (float)sum / N;
    float maxv = (float)hal().adcMax();
    if (raw < 1.0f || raw > maxv - 1.0f) return false;  // rail: wiring fault or saturated, do not invent a number
    float rLdr = ldrToGnd_ ? rFixed_ * raw / (maxv - raw) : rFixed_ * (maxv - raw) / raw;
    out = Value::makeFloat(lux10k_ * powf(10000.0f / rLdr, 1.0f / gamma_));
    return true;
  }

 private:
  int pin_;
  float rFixed_;
  bool ldrToGnd_;
  float lux10k_, gamma_;
};

// Raw ADC percentage (0-100) of full scale, e.g. for a battery divider or a generic analog sensor on ADC1.
class AdcPercentIn : public SensorDriver {
 public:
  explicit AdcPercentIn(int pin) : pin_(pin) {}
  bool validate(std::string& err) const override {
    if (!pinIsAdc1(pin_)) { err = "AdcPercentIn: pin must be ADC1 (32-39)"; return false; }
    return true;
  }
  bool begin() override { hal().pinIn(pin_, false); return true; }
  bool read(uint32_t, Value& out) override {
    long sum = 0;
    for (int k = 0; k < 16; k++) sum += hal().adc(pin_);
    out = Value::makeInt((int32_t)lroundf(100.0f * ((float)sum / 16.0f) / (float)hal().adcMax()));
    return true;
  }

 private:
  int pin_;
};

}  // namespace edge
