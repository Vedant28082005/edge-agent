#include "NodeModel.h"
#include <math.h>
#include <string.h>

namespace edge {

CapDef* EndpointBuilder::last() {
  if (ep_->caps.empty()) {
    m_->addError("endpoint '" + ep_->localId + "': modifier used before any capability was declared");
    return nullptr;
  }
  return &ep_->caps.back();
}

static bool sameValues(const std::vector<std::string>& v, const char* a, const char* b) {
  return v.size() == 2 && v[0] == a && v[1] == b;
}

EndpointBuilder& EndpointBuilder::addActuator(const char* id, CapType t, bool hasRange, float lo, float hi,
                                              std::vector<std::string> values, Actuator* drv) {
  std::unique_ptr<Actuator> owned(drv);
  CapDef c;
  c.id = id ? id : "";
  c.kind = CapKind::Actuator;
  c.type = t;
  c.hasRange = hasRange;
  c.lo = lo;
  c.hi = hi;
  c.values = std::move(values);
  c.act = std::move(owned);
  const CapSpec* sp = findCapSpec(id);
  std::string where = "endpoint '" + ep_->localId + "' capability '" + c.id + "': ";
  if (!sp) m_->addError(where + "not in the capability vocabulary");
  else {
    if (sp->kind != CapKind::Actuator) m_->addError(where + "is a sensor capability, declared as actuator");
    if (sp->type != t) m_->addError(where + "must be of type " + capTypeName(sp->type));
    if (sp->defaultUnit) c.unit = sp->defaultUnit;
    if (sp->fixedV0 && t == CapType::Enum && !sameValues(c.values, sp->fixedV0, sp->fixedV1))
      m_->addError(where + "values must be exactly [" + sp->fixedV0 + ", " + sp->fixedV1 + "]");
  }
  ep_->caps.push_back(std::move(c));
  return *this;
}

EndpointBuilder& EndpointBuilder::addSensor(const char* id, CapType t, std::vector<std::string> values,
                                            uint32_t intervalS, SensorDriver* drv) {
  std::unique_ptr<SensorDriver> owned(drv);
  CapDef c;
  c.id = id ? id : "";
  c.kind = CapKind::Sensor;
  c.type = t;
  c.values = std::move(values);
  c.intervalS = intervalS;
  c.sens = std::move(owned);
  const CapSpec* sp = findCapSpec(id);
  std::string where = "endpoint '" + ep_->localId + "' capability '" + c.id + "': ";
  if (!sp) m_->addError(where + "not in the capability vocabulary");
  else {
    if (sp->kind != CapKind::Sensor) m_->addError(where + "is an actuator capability, declared as sensor");
    if (sp->type != t) m_->addError(where + "must be of type " + capTypeName(sp->type));
    if (sp->defaultUnit) c.unit = sp->defaultUnit;
    c.deadband = sp->deadband;
    if (sp->fixedV0 && t == CapType::Enum && !sameValues(c.values, sp->fixedV0, sp->fixedV1))
      m_->addError(where + "values must be exactly [" + sp->fixedV0 + ", " + sp->fixedV1 + "]");
  }
  ep_->caps.push_back(std::move(c));
  return *this;
}

EndpointBuilder& EndpointBuilder::setBool(bool boot, bool v) {
  CapDef* c = last();
  if (!c) return *this;
  if (c->kind != CapKind::Actuator || c->type != CapType::Bool) {
    m_->addError("capability '" + c->id + "': a bool value was given for a non-bool actuator");
    return *this;
  }
  if (boot) { c->boot = Value::makeBool(v); c->bootSet = true; }
  else { c->failsafe = Value::makeBool(v); c->hasFailsafe = true; }
  return *this;
}

EndpointBuilder& EndpointBuilder::setNum(bool boot, double d) {
  CapDef* c = last();
  if (!c) return *this;
  Value v;
  if (c->kind != CapKind::Actuator) { m_->addError("capability '" + c->id + "': sensors have no boot/failsafe value"); return *this; }
  if (c->type == CapType::Int) {
    if (d != floor(d) || fabs(d) > 2147483000.0) { m_->addError("capability '" + c->id + "': int value expected"); return *this; }
    v = Value::makeInt((int32_t)d);
  } else if (c->type == CapType::Float) {
    v = Value::makeFloat((float)d);
  } else {
    m_->addError("capability '" + c->id + "': a number was given for a non-numeric actuator");
    return *this;
  }
  if (boot) { c->boot = v; c->bootSet = true; }
  else { c->failsafe = v; c->hasFailsafe = true; }
  return *this;
}

EndpointBuilder& EndpointBuilder::setStr(bool boot, const char* s) {
  CapDef* c = last();
  if (!c) return *this;
  if (c->kind != CapKind::Actuator || c->type != CapType::Enum) {
    m_->addError("capability '" + c->id + "': a string value was given for a non-enum actuator");
    return *this;
  }
  int idx = c->enumIndex(s ? s : "");
  if (idx < 0) { m_->addError("capability '" + c->id + "': value is not in the enum values"); return *this; }
  if (boot) { c->boot = Value::makeEnum(idx); c->bootSet = true; }
  else { c->failsafe = Value::makeEnum(idx); c->hasFailsafe = true; }
  return *this;
}

EndpointBuilder& EndpointBuilder::maxOnS(uint32_t s) {
  CapDef* c = last();
  if (!c) return *this;
  if (c->kind != CapKind::Actuator || c->type != CapType::Bool) {
    m_->addError("capability '" + c->id + "': maxOnS applies to bool actuators only");
    return *this;
  }
  c->maxOnS = s;
  c->maxOnSet = true;
  return *this;
}

EndpointBuilder& EndpointBuilder::unit(const char* u) {
  if (CapDef* c = last()) c->unit = u ? u : "";
  return *this;
}

EndpointBuilder& EndpointBuilder::deadband(float d) {
  CapDef* c = last();
  if (!c) return *this;
  if (c->kind != CapKind::Sensor || d < 0) { m_->addError("capability '" + c->id + "': deadband applies to sensors, must be >= 0"); return *this; }
  c->deadband = d;
  return *this;
}

EndpointBuilder& EndpointBuilder::addButton(const char* capId, int pin, bool activeLow, ButtonDef::Mode mode) {
  int idx = ep_->findCap(capId ? capId : "");
  if (idx < 0) {
    m_->addError("endpoint '" + ep_->localId + "': local control for unknown capability '" + std::string(capId ? capId : "") + "' (declare the capability first)");
    return *this;
  }
  ButtonDef b;
  b.pin = pin;
  b.activeLow = activeLow;
  b.mode = mode;
  b.capIdx = (size_t)idx;
  ep_->buttons.push_back(b);
  return *this;
}

bool NodeModel::validate() {
  if (validated_) return errors_.empty();
  validated_ = true;
  if (!isValidHwString(hw)) addError("hw string invalid (^[A-Za-z0-9._-]{1,32}$)");
  if (!isValidFwString(fw)) addError("fw string invalid (^[A-Za-z0-9.+-]{1,32}$)");
  if (failsafeAfterS > 86400) addError("failsafe_after_s must be <= 86400");
  if (heartbeatS < 5 || heartbeatS > 3600) addError("heartbeat must be 5..3600 s");
  if (endpoints.empty() || endpoints.size() > 16) addError("a node needs 1..16 endpoints");

  for (size_t e = 0; e < endpoints.size(); e++) {
    EndpointDef& ep = *endpoints[e];
    std::string ew = "endpoint '" + ep.localId + "': ";
    if (!isValidLocalId(ep.localId)) addError(ew + "local_id must match ^[a-z0-9_]{1,16}$");
    for (size_t o = 0; o < e; o++)
      if (endpoints[o]->localId == ep.localId) addError(ew + "duplicate local_id");
    if (ep.caps.empty() || ep.caps.size() > 16) addError(ew + "needs 1..16 capabilities");

    for (size_t k = 0; k < ep.caps.size(); k++) {
      CapDef& c = ep.caps[k];
      std::string w = ew + "capability '" + c.id + "': ";
      for (size_t o = 0; o < k; o++)
        if (ep.caps[o].id == c.id) addError(w + "declared twice");
      const CapSpec* sp = findCapSpec(c.id.c_str());
      if (!c.unit.empty() && !unitAllowed(c.unit)) addError(w + "unit not allowed");

      // range / values
      if (c.type == CapType::Int || c.type == CapType::Float) {
        if (c.kind == CapKind::Actuator) {
          if (!c.hasRange) addError(w + "needs a range");
          if (c.lo > c.hi) addError(w + "min > max");
          if (sp && !isnan(sp->hardLo) && (c.lo < sp->hardLo || c.hi > sp->hardHi))
            addError(w + "range must lie within " + std::to_string((int)sp->hardLo) + ".." + std::to_string((int)sp->hardHi));
        }
      }
      if (c.type == CapType::Enum) {
        if (c.values.size() < 2 || c.values.size() > 16) addError(w + "enum needs 2..16 values");
        for (size_t a = 0; a < c.values.size(); a++) {
          if (!isValidEnumValue(c.values[a])) addError(w + "enum value must match ^[a-z0-9_]{1,24}$");
          for (size_t b = 0; b < a; b++) if (c.values[a] == c.values[b]) addError(w + "duplicate enum value");
        }
      }

      if (c.kind == CapKind::Actuator) {
        if (!c.act) { addError(w + "no driver"); continue; }
        std::string de;
        if (!c.act->validate(de)) addError(w + de);
        // default boot value: lowest / off / first
        if (!c.bootSet) {
          switch (c.type) {
            case CapType::Bool: c.boot = Value::makeBool(false); break;
            case CapType::Int: c.boot = Value::makeInt((int32_t)c.lo); break;
            case CapType::Float: c.boot = Value::makeFloat(c.lo); break;
            default: c.boot = Value::makeEnum(0); break;
          }
          c.bootSet = true;
        }
        auto inRange = [&](const Value& v) {
          if (c.type == CapType::Int) return v.i >= (int32_t)c.lo && v.i <= (int32_t)c.hi;
          if (c.type == CapType::Float) return v.f >= c.lo && v.f <= c.hi;
          return true;
        };
        if (!inRange(c.boot)) addError(w + "boot_value outside the range");
        if (c.hasFailsafe && !inRange(c.failsafe)) addError(w + "failsafe_value outside the range");
        if (c.maxOnS > 86400) addError(w + "max_on_s must be <= 86400");
        if (c.intervalS) addError(w + "interval_s is for sensors");
      } else {
        if (!c.sens) { addError(w + "no driver"); continue; }
        std::string de;
        if (!c.sens->validate(de)) addError(w + de);
        if (c.intervalS < 1 || c.intervalS > 86400) addError(w + "interval_s must be 1..86400");
        if (c.hasFailsafe || c.bootSet || c.maxOnSet) addError(w + "sensors cannot have boot/failsafe/max_on");
        if (c.type == CapType::Int && sp && !isnan(sp->hardLo) && c.hasRange && (c.lo < sp->hardLo || c.hi > sp->hardHi))
          addError(w + "range outside the allowed one");
      }
    }

    for (const ButtonDef& b : ep.buttons) {
      if (b.capIdx >= ep.caps.size()) { addError(ew + "local control bound to a missing capability"); continue; }
      const CapDef& c = ep.caps[b.capIdx];
      std::string w = ew + "local control for '" + c.id + "': ";
      if (c.kind != CapKind::Actuator) addError(w + "needs an actuator");
      else if (b.mode == ButtonDef::Toggle && c.type != CapType::Bool) addError(w + "toggle needs a bool capability");
      else if (b.mode == ButtonDef::Cycle && c.type != CapType::Int) addError(w + "cycle needs an int capability");
      if (!pinValidInput(b.pin)) addError(w + "pin " + std::to_string(b.pin) + " is not a usable input");
      else if (b.activeLow && b.pin >= 34) addError(w + "pins 34-39 have no internal pull-up: use activeLow=false with an external pull-down");
    }
  }
  return errors_.empty();
}

}  // namespace edge
