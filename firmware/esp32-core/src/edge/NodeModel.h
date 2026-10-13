// The declarative device description: node -> endpoints -> capabilities (+ their drivers).
//
//   node.endpoint("ch1", FAN)
//       .boolCap("power", RelayOut(26)).failsafeValue(false).maxOnS(3600)
//       .intCap("speed", 1, 5, PwmOut(27, 25000))
//       .localToggle("power", 33);
//
// Declaration mistakes (unknown capability, wrong type, bad range, invalid pin ...) are collected in
// errors() and checked by validate(); begin() refuses to start a node whose declaration is invalid.
#pragma once
#include <initializer_list>
#include <memory>
#include <string>
#include <type_traits>
#include <vector>
#include "Drivers.h"
#include "Value.h"
#include "Vocabulary.h"

namespace edge {

struct CapDef {
  std::string id;
  CapKind kind = CapKind::Actuator;
  CapType type = CapType::Bool;
  bool hasRange = false;
  float lo = 0, hi = 0;
  std::vector<std::string> values;
  std::string unit;
  uint32_t intervalS = 0;  // sensors: max seconds between readings
  Value boot;
  bool bootSet = false;
  bool hasFailsafe = false;
  Value failsafe;
  uint32_t maxOnS = 0;
  bool maxOnSet = false;
  float deadband = 0;
  std::unique_ptr<Actuator> act;
  std::unique_ptr<SensorDriver> sens;

  // runtime state
  Value cur;
  bool haveCur = false;
  bool on = false;        // bool actuator currently true (continuous on-time tracking)
  uint32_t onSince = 0;
  bool sampled = false;
  uint32_t lastSample = 0;

  int enumIndex(const std::string& s) const {
    for (size_t k = 0; k < values.size(); k++) if (values[k] == s) return (int)k;
    return -1;
  }
};

struct ButtonDef {
  enum Mode : uint8_t { Toggle, Cycle };
  int pin = -1;
  bool activeLow = true;
  Mode mode = Toggle;
  size_t capIdx = 0;
  bool init = false, stable = false, cand = false;
  uint32_t since = 0;
};

struct EndpointDef {
  std::string localId;
  EpType type = EpType::Generic;
  std::vector<CapDef> caps;
  std::vector<ButtonDef> buttons;
  int findCap(const std::string& id) const {
    for (size_t k = 0; k < caps.size(); k++) if (caps[k].id == id) return (int)k;
    return -1;
  }
};

class NodeModel {
 public:
  std::vector<std::unique_ptr<EndpointDef>> endpoints;
  std::string hw = "esp32-devkit";
  std::string fw = "0.1.0";
  uint32_t failsafeAfterS = 60;  // 0 = keep last values
  uint32_t heartbeatS = 60;      // state republished at least this often while connected
  int ledPin = -1;
  bool ledActiveHigh = true;

  void addError(const std::string& e) { if (errors_.size() < 24) errors_.push_back(e); }
  const std::vector<std::string>& errors() const { return errors_; }
  // Fills in default boot values and runs every static check. Returns true if the declaration is usable.
  bool validate();
  int findEndpoint(const std::string& localId) const {
    for (size_t k = 0; k < endpoints.size(); k++) if (endpoints[k]->localId == localId) return (int)k;
    return -1;
  }

 private:
  std::vector<std::string> errors_;
  bool validated_ = false;
};

class EndpointBuilder {
 public:
  EndpointBuilder(NodeModel& m, EndpointDef* ep) : m_(&m), ep_(ep) {}

  // ---- actuators -------------------------------------------------------------------------------
  template <class D> EndpointBuilder& boolCap(const char* id, D drv) {
    static_assert(std::is_base_of<Actuator, D>::value, "driver must be an Actuator");
    return addActuator(id, CapType::Bool, false, 0, 0, {}, new D(std::move(drv)));
  }
  template <class D> EndpointBuilder& intCap(const char* id, int lo, int hi, D drv) {
    static_assert(std::is_base_of<Actuator, D>::value, "driver must be an Actuator");
    return addActuator(id, CapType::Int, true, (float)lo, (float)hi, {}, new D(std::move(drv)));
  }
  template <class D> EndpointBuilder& floatCap(const char* id, float lo, float hi, D drv) {
    static_assert(std::is_base_of<Actuator, D>::value, "driver must be an Actuator");
    return addActuator(id, CapType::Float, true, lo, hi, {}, new D(std::move(drv)));
  }
  template <class D> EndpointBuilder& enumCap(const char* id, std::initializer_list<const char*> values, D drv) {
    static_assert(std::is_base_of<Actuator, D>::value, "driver must be an Actuator");
    return addActuator(id, CapType::Enum, false, 0, 0, std::vector<std::string>(values.begin(), values.end()), new D(std::move(drv)));
  }

  // ---- sensors (intervalS = maximum seconds between readings / state refresh) --------------------
  template <class D> EndpointBuilder& boolSensor(const char* id, D drv, uint32_t intervalS) {
    static_assert(std::is_base_of<SensorDriver, D>::value, "driver must be a SensorDriver");
    return addSensor(id, CapType::Bool, {}, intervalS, new D(std::move(drv)));
  }
  template <class D> EndpointBuilder& intSensor(const char* id, D drv, uint32_t intervalS) {
    static_assert(std::is_base_of<SensorDriver, D>::value, "driver must be a SensorDriver");
    return addSensor(id, CapType::Int, {}, intervalS, new D(std::move(drv)));
  }
  template <class D> EndpointBuilder& floatSensor(const char* id, D drv, uint32_t intervalS) {
    static_assert(std::is_base_of<SensorDriver, D>::value, "driver must be a SensorDriver");
    return addSensor(id, CapType::Float, {}, intervalS, new D(std::move(drv)));
  }
  template <class D> EndpointBuilder& enumSensor(const char* id, std::initializer_list<const char*> values, D drv, uint32_t intervalS) {
    static_assert(std::is_base_of<SensorDriver, D>::value, "driver must be a SensorDriver");
    return addSensor(id, CapType::Enum, std::vector<std::string>(values.begin(), values.end()), intervalS, new D(std::move(drv)));
  }

  // ---- modifiers: apply to the capability declared last ------------------------------------------
  EndpointBuilder& bootValue(bool v) { return setBool(true, v); }
  EndpointBuilder& bootValue(int v) { return setNum(true, (double)v); }
  EndpointBuilder& bootValue(double v) { return setNum(true, v); }
  EndpointBuilder& bootValue(const char* v) { return setStr(true, v); }
  EndpointBuilder& failsafeValue(bool v) { return setBool(false, v); }
  EndpointBuilder& failsafeValue(int v) { return setNum(false, (double)v); }
  EndpointBuilder& failsafeValue(double v) { return setNum(false, v); }
  EndpointBuilder& failsafeValue(const char* v) { return setStr(false, v); }
  EndpointBuilder& maxOnS(uint32_t s);         // bool actuators only; 0 = unlimited
  EndpointBuilder& unit(const char* u);        // overrides the vocabulary default
  EndpointBuilder& deadband(float d);          // sensors: smallest change that is published

  // ---- local controls (work offline) --------------------------------------------------------------
  // Pushbutton to GND (internal pull-up): toggles a bool capability / steps an int capability min..max and wraps.
  EndpointBuilder& localToggle(const char* capId, int pin, bool activeLow = true) { return addButton(capId, pin, activeLow, ButtonDef::Toggle); }
  EndpointBuilder& localCycle(const char* capId, int pin, bool activeLow = true) { return addButton(capId, pin, activeLow, ButtonDef::Cycle); }

 private:
  EndpointBuilder& addActuator(const char* id, CapType t, bool hasRange, float lo, float hi, std::vector<std::string> values, Actuator* drv);
  EndpointBuilder& addSensor(const char* id, CapType t, std::vector<std::string> values, uint32_t intervalS, SensorDriver* drv);
  EndpointBuilder& setBool(bool boot, bool v);
  EndpointBuilder& setNum(bool boot, double d);
  EndpointBuilder& setStr(bool boot, const char* s);
  EndpointBuilder& addButton(const char* capId, int pin, bool activeLow, ButtonDef::Mode mode);
  CapDef* last();
  NodeModel* m_;
  EndpointDef* ep_;
};

}  // namespace edge
