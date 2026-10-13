// Driver interfaces. Pin-level code lives behind these; the core never touches a pin.
#pragma once
#include <memory>
#include <string>
#include "Hal.h"
#include "Value.h"

namespace edge {

// Passed to a driver so it can scale values to the declared range.
struct CapRange {
  float lo = 0, hi = 1;
};

class Actuator {
 public:
  virtual ~Actuator() {}
  // Static check of the declaration (pins valid, calibration present). Returns false and an error text.
  virtual bool validate(std::string& err) const { (void)err; return true; }
  // Configure pins. Must leave the output in its safe (off) state.
  virtual bool begin() { return true; }
  virtual void bind(const CapRange& r) { (void)r; }
  // Value already validated against the declared range/type by the core.
  virtual ApplyResult apply(const Value& v, uint32_t now) = 0;
  // Boot value. Default is a normal apply; a curtain only records the assumed position (never moves at boot).
  virtual void applyBoot(const Value& v, uint32_t now) { apply(v, now); }
  virtual void tick(uint32_t now) { (void)now; }
  // Drivers whose real state differs from the last applied value (curtain position) report changes here.
  virtual bool pollChanged(Value& out) { (void)out; return false; }
  // True if the real state differs from what was commanded and is reported via pollChanged (curtain).
  virtual bool reportsOwnState() const { return false; }
};

class SensorDriver {
 public:
  virtual ~SensorDriver() {}
  virtual bool validate(std::string& err) const { (void)err; return true; }
  virtual bool begin() { return true; }
  // Fast-polled inputs (switches, PIR) return their poll period; 0 = sample once per interval_s.
  virtual uint32_t pollMs() const { return 0; }
  // Returns false if no valid reading is available (the core keeps the last value and does not publish junk).
  virtual bool read(uint32_t now, Value& out) = 0;
};

}  // namespace edge
