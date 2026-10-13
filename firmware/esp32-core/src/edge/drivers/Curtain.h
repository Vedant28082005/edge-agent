// Curtain / blind / shutter motor with open/close interlock.
//
//   position 0 = fully closed, 100 = fully open.
//   - Two outputs (open, close). The controller is ONE state machine whose outputs are derived from its
//     phase, so both can never be energised at once; the driver also enforces it at the pin level.
//   - Direction reversal always passes through a dead time with both outputs off.
//   - Position comes from travel-time calibration (open/close ms), limit switches, or both.
//     With limit switches the travel times are learned from uninterrupted end-to-end runs.
//   - A maximum run time stops the motor no matter what (stuck curtain, broken limit switch).
//   - The boot value is the ASSUMED position: the curtain never moves at boot.
#pragma once
#include <math.h>
#include "../Drivers.h"

namespace edge {

class CurtainController {
 public:
  struct Config {
    uint32_t openMs = 0, closeMs = 0;  // full travel times (0 = unknown; learned from limit switches)
    bool limitOpen = false, limitClosed = false;
    uint32_t maxRunMs = 0;
    uint32_t deadMs = 500;
  };
  enum class Phase : uint8_t { Idle, Dead, Opening, Closing };

  void configure(const Config& c) { c_ = c; }
  const Config& config() const { return c_; }
  bool calibrated() const { return c_.openMs > 0 && c_.closeMs > 0; }

  void setAssumedPosition(float p) { pos_ = clamp(p); lastReported_ = pos_; }
  float position() const { return pos_; }
  Phase phase() const { return phase_; }
  bool moving() const { return phase_ != Phase::Idle; }
  bool faultTimeout() const { return fault_; }
  bool openOut() const { return phase_ == Phase::Opening; }
  bool closeOut() const { return phase_ == Phase::Closing; }

  ApplyResult moveTo(int target, uint32_t now, bool atOpenLimit, bool atClosedLimit) {
    float t = clamp((float)target);
    bool toOpen = t >= 100.0f, toClosed = t <= 0.0f;
    bool bothLimits = c_.limitOpen && c_.limitClosed;
    if (c_.maxRunMs == 0) return ApplyResult::Unsupported;
    if (!calibrated() && !((toOpen && c_.limitOpen) || (toClosed && c_.limitClosed))) {
      (void)bothLimits;
      return ApplyResult::Unsupported;  // cannot position mid-way without travel times
    }
    integrate(now);
    if (toOpen && c_.limitOpen && atOpenLimit) { pos_ = 100; stopNow(now); return ApplyResult::Ok; }
    if (toClosed && c_.limitClosed && atClosedLimit) { pos_ = 0; stopNow(now); return ApplyResult::Ok; }
    bool limitDriven = (toOpen && c_.limitOpen) || (toClosed && c_.limitClosed);
    if (!limitDriven && fabsf(t - pos_) < 1.0f) { stopNow(now); return ApplyResult::Ok; }
    Phase want = limitDriven ? (toOpen ? Phase::Opening : Phase::Closing) : (t > pos_ ? Phase::Opening : Phase::Closing);
    target_ = t;
    if (phase_ == want) return ApplyResult::Ok;  // retarget while already moving the right way
    if (phase_ == Phase::Opening || phase_ == Phase::Closing) endRun(now);
    pendingDir_ = want;
    startAfter_ = (lastDir_ != Phase::Idle && lastDir_ != want) ? lastStop_ + c_.deadMs : now;
    if ((int32_t)(startAfter_ - now) <= 0) beginRun(want, now);
    else phase_ = Phase::Dead;
    return ApplyResult::Ok;
  }

  void stop(uint32_t now) { integrate(now); stopNow(now); }

  void tick(uint32_t now, bool atOpenLimit, bool atClosedLimit) {
    integrate(now);
    if (phase_ == Phase::Dead) {
      if ((int32_t)(now - startAfter_) >= 0) beginRun(pendingDir_, now);
      return;
    }
    if (phase_ == Phase::Idle) return;
    bool opening = phase_ == Phase::Opening;
    if (opening && c_.limitOpen && atOpenLimit) { hitLimit(100.0f, now); return; }
    if (!opening && c_.limitClosed && atClosedLimit) { hitLimit(0.0f, now); return; }
    if (elapsedMs(now, runStart_) >= c_.maxRunMs) { fault_ = true; endRun(now); return; }
    bool limitDriven = (opening && target_ >= 100.0f && c_.limitOpen) || (!opening && target_ <= 0.0f && c_.limitClosed);
    if (limitDriven || !calibrated()) return;  // keep running until the limit switch (or the max run time)
    if ((opening && pos_ >= target_) || (!opening && pos_ <= target_)) endRun(now);
  }

  // Position changes worth publishing: motion stopped, or moved >= 5 since the last report.
  bool pollChanged(int& out) {
    bool stopped = phase_ == Phase::Idle;
    if ((stopped && lastReported_ != pos_) || fabsf(pos_ - lastReported_) >= 5.0f) {
      lastReported_ = pos_;
      out = (int)lroundf(pos_);
      return true;
    }
    return false;
  }

 private:
  static float clamp(float p) { return p < 0 ? 0 : (p > 100 ? 100 : p); }
  void integrate(uint32_t now) {
    if (phase_ == Phase::Opening || phase_ == Phase::Closing) {
      uint32_t dt = elapsedMs(now, lastInt_);
      uint32_t full = phase_ == Phase::Opening ? c_.openMs : c_.closeMs;
      if (full > 0) pos_ = clamp(pos_ + (phase_ == Phase::Opening ? 1.0f : -1.0f) * 100.0f * (float)dt / (float)full);
    }
    lastInt_ = now;
  }
  void beginRun(Phase dir, uint32_t now) {
    phase_ = dir;
    runStart_ = now;
    lastInt_ = now;
    fault_ = false;
    startedAtKnownEnd_ = (dir == Phase::Opening) ? atClosedEnd_ : atOpenEnd_;
    atOpenEnd_ = atClosedEnd_ = false;
  }
  void endRun(uint32_t now) {  // motor off; remembers the direction/time for the dead-time rule
    lastDir_ = phase_;
    lastStop_ = now;
    phase_ = Phase::Idle;
  }
  void stopNow(uint32_t now) {
    if (phase_ == Phase::Opening || phase_ == Phase::Closing) endRun(now);
    phase_ = Phase::Idle;
  }
  void hitLimit(float p, uint32_t now) {
    bool opening = phase_ == Phase::Opening;
    uint32_t took = elapsedMs(now, runStart_);
    if (startedAtKnownEnd_ && took > 500) {  // uninterrupted end-to-end run: learn the travel time
      uint32_t& tm = opening ? c_.openMs : c_.closeMs;
      tm = tm > 0 ? (tm * 3 + took) / 4 : took;
    }
    pos_ = p;
    (opening ? atOpenEnd_ : atClosedEnd_) = true;
    endRun(now);
  }

  Config c_;
  Phase phase_ = Phase::Idle, pendingDir_ = Phase::Idle, lastDir_ = Phase::Idle;
  float pos_ = 0, target_ = 0, lastReported_ = 0;
  uint32_t runStart_ = 0, lastInt_ = 0, lastStop_ = 0, startAfter_ = 0;
  bool fault_ = false, startedAtKnownEnd_ = false, atOpenEnd_ = false, atClosedEnd_ = false;
};

class CurtainMotor : public Actuator {
 public:
  CurtainMotor(int openPin, int closePin, bool activeHigh = true) : openPin_(openPin), closePin_(closePin), activeHigh_(activeHigh) {}
  CurtainMotor& travelTime(uint32_t openMs, uint32_t closeMs) { cfg_.openMs = openMs; cfg_.closeMs = closeMs; return *this; }
  // Limit switches wired to GND with the internal pull-up (pressed = low).
  CurtainMotor& limitSwitches(int openLimitPin, int closedLimitPin) {
    limOpenPin_ = openLimitPin; limClosedPin_ = closedLimitPin; cfg_.limitOpen = cfg_.limitClosed = true; return *this;
  }
  CurtainMotor& maxRun(uint32_t ms) { cfg_.maxRunMs = ms; return *this; }
  CurtainMotor& deadTime(uint32_t ms) { cfg_.deadMs = ms; return *this; }

  bool validate(std::string& err) const override {
    if (!pinValidOutput(openPin_) || !pinValidOutput(closePin_)) { err = "CurtainMotor: open/close pins must be usable outputs"; return false; }
    if (openPin_ == closePin_) { err = "CurtainMotor: open and close pins must differ"; return false; }
    if (cfg_.limitOpen) {
      if (!pinValidInput(limOpenPin_) || !pinValidInput(limClosedPin_) || limOpenPin_ == limClosedPin_) { err = "CurtainMotor: bad limit switch pins"; return false; }
      if (limOpenPin_ >= 34 || limClosedPin_ >= 34) { err = "CurtainMotor: limit pins 34-39 have no pull-up"; return false; }
    }
    bool cal = cfg_.openMs > 0 && cfg_.closeMs > 0;
    if (!cal && !cfg_.limitOpen) { err = "CurtainMotor: needs travelTime() or limitSwitches()"; return false; }
    if (!cal && cfg_.maxRunMs == 0) { err = "CurtainMotor: maxRun() is required without travel times"; return false; }
    return true;
  }
  bool begin() override {
    if (cfg_.maxRunMs == 0) {
      uint32_t m = cfg_.openMs > cfg_.closeMs ? cfg_.openMs : cfg_.closeMs;
      cfg_.maxRunMs = m + m / 2;  // 150 % of the slowest calibrated travel
    }
    ctl_.configure(cfg_);
    hal().pinOut(openPin_, !activeHigh_);
    hal().pinOut(closePin_, !activeHigh_);
    if (cfg_.limitOpen) { hal().pinIn(limOpenPin_, true); hal().pinIn(limClosedPin_, true); }
    outOpen_ = outClose_ = false;
    return true;
  }
  ApplyResult apply(const Value& v, uint32_t now) override {
    ApplyResult r = ctl_.moveTo(v.i, now, limOpen(), limClosed());
    drive();
    return r;
  }
  void applyBoot(const Value& v, uint32_t) override { ctl_.setAssumedPosition((float)v.i); }
  void tick(uint32_t now) override {
    ctl_.tick(now, limOpen(), limClosed());
    drive();
  }
  bool pollChanged(Value& out) override {
    int p;
    if (!ctl_.pollChanged(p)) return false;
    out = Value::makeInt(p);
    return true;
  }
  bool reportsOwnState() const override { return true; }
  const CurtainController& controller() const { return ctl_; }

 private:
  bool limOpen() { return cfg_.limitOpen && !hal().read(limOpenPin_); }
  bool limClosed() { return cfg_.limitClosed && !hal().read(limClosedPin_); }
  // Switch a pin OFF before the other one goes ON, and refuse to energise both.
  void drive() {
    bool wantO = ctl_.openOut(), wantC = ctl_.closeOut();
    if (wantO && wantC) wantO = wantC = false;  // unreachable by construction; belt and braces
    if (!wantO && outOpen_) { hal().write(openPin_, !activeHigh_); outOpen_ = false; }
    if (!wantC && outClose_) { hal().write(closePin_, !activeHigh_); outClose_ = false; }
    if (wantO && !outOpen_ && !outClose_) { hal().write(openPin_, activeHigh_); outOpen_ = true; }
    if (wantC && !outClose_ && !outOpen_) { hal().write(closePin_, activeHigh_); outClose_ = true; }
  }

  int openPin_, closePin_, limOpenPin_ = -1, limClosedPin_ = -1;
  bool activeHigh_;
  CurtainController::Config cfg_;
  CurtainController ctl_;
  bool outOpen_ = false, outClose_ = false;
};

}  // namespace edge
