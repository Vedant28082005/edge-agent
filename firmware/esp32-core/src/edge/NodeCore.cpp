#include "NodeCore.h"
#include <ArduinoJson.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

namespace edge {

// ---- small validators (mirror the patterns in cmd.schema.json) ----------------------------------

static bool isHex(char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); }

bool isValidUuid(const char* s) {
  if (!s || strlen(s) != 36) return false;
  for (int k = 0; k < 36; k++) {
    if (k == 8 || k == 13 || k == 18 || k == 23) { if (s[k] != '-') return false; }
    else if (!isHex(s[k])) return false;
  }
  return true;
}

static bool charsIn(const char* s, size_t from, size_t minLen, size_t maxLen, bool allowDot) {
  size_t n = strlen(s);
  if (n < from + minLen || n > from + maxLen) return false;
  for (size_t k = from; k < n; k++) {
    char c = s[k];
    bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-' || (allowDot && c == '.');
    if (!ok) return false;
  }
  return true;
}

bool isValidBy(const char* s) {
  if (!s) return false;
  if (strcmp(s, "agent") == 0) return true;
  if (strncmp(s, "user:", 5) == 0) return charsIn(s, 5, 1, 64, true);
  if (strncmp(s, "scene:", 6) == 0) return charsIn(s, 6, 1, 32, false);
  if (strncmp(s, "rule:", 5) == 0) return charsIn(s, 5, 1, 32, false);
  return false;
}

// ---- begin -----------------------------------------------------------------------------------------

ApplyResult NodeCore::setActuator(CapDef& c, const Value& v, uint32_t now, bool boot) {
  ApplyResult r = ApplyResult::Ok;
  if (boot) c.act->applyBoot(v, now);
  else r = c.act->apply(v, now);
  if (r != ApplyResult::Ok) return r;
  Value before = c.cur;
  bool had = c.haveCur;
  if (!c.act->reportsOwnState() || boot) { c.cur = v; c.haveCur = true; }
  if (c.type == CapType::Bool) {
    if (v.b && !c.on) { c.on = true; c.onSince = now; }  // a repeated "on" does not restart the on-time
    else if (!v.b) c.on = false;
  }
  if (!had || before != c.cur) dirty_ = true;
  return r;
}

bool NodeCore::begin(uint32_t now) {
  if (started_) return true;
  if (!m_.validate()) return false;
  sensors_.clear();
  for (auto& ep : m_.endpoints) {
    for (auto& c : ep->caps) {
      if (c.kind == CapKind::Actuator) {
        c.act->bind(CapRange{c.lo, c.hi});
        if (!c.act->begin()) { m_.addError("endpoint '" + ep->localId + "' capability '" + c.id + "': driver failed to start"); return false; }
      } else {
        if (!c.sens->begin()) { m_.addError("endpoint '" + ep->localId + "' capability '" + c.id + "': driver failed to start"); return false; }
        sensors_.push_back(&c);
      }
    }
  }
  // boot_value is applied before any network activity.
  for (auto& ep : m_.endpoints)
    for (auto& c : ep->caps)
      if (c.kind == CapKind::Actuator) setActuator(c, c.boot, now, true);
  discSince_ = now;
  hubUp_ = false;
  failsafeDone_ = false;
  started_ = true;
  dirty_ = true;
  return true;
}

// ---- tick: buttons, drivers, max_on_s, failsafe ----------------------------------------------------

void NodeCore::runButtons(EndpointDef& ep, uint32_t now) {
  for (ButtonDef& b : ep.buttons) {
    bool raw = hal().read(b.pin);
    bool active = b.activeLow ? !raw : raw;
    if (!b.init) { b.init = true; b.stable = active; b.cand = active; b.since = now; continue; }
    if (active != b.cand) { b.cand = active; b.since = now; continue; }
    if (b.cand == b.stable || elapsedMs(now, b.since) < 30) continue;
    b.stable = b.cand;
    if (!b.stable) continue;  // act on press, not release
    CapDef& c = ep.caps[b.capIdx];
    Value v = c.cur;
    if (b.mode == ButtonDef::Toggle) v = Value::makeBool(!c.cur.b);
    else {
      int32_t nx = c.cur.i + 1;
      v = Value::makeInt(nx > (int32_t)c.hi ? (int32_t)c.lo : nx);
    }
    setActuator(c, v, now, false);
    c.localOverride = true;  // the person's choice beats failsafe until the hub next sets this capability
    dirty_ = true;
  }
}

bool NodeCore::failsafeNow(uint32_t now) {
  bool any = false;
  for (auto& ep : m_.endpoints)
    for (auto& c : ep->caps)
      if (c.kind == CapKind::Actuator && c.hasFailsafe && !c.localOverride) {
        setActuator(c, c.failsafe, now, false);
        any = true;
      }
  return any;
}

bool NodeCore::tick(uint32_t now, bool hubConnected) {
  if (!started_) return false;
  for (auto& ep : m_.endpoints) {
    runButtons(*ep, now);
    for (auto& c : ep->caps) {
      if (c.kind != CapKind::Actuator) continue;
      c.act->tick(now);
      Value v;
      if (c.act->pollChanged(v)) { c.cur = v; c.haveCur = true; dirty_ = true; }
      // max_on_s: forced off after a continuous on-time, whoever turned it on
      if (c.type == CapType::Bool && c.on && c.maxOnS > 0 && elapsedMs(now, c.onSince) >= c.maxOnS * 1000u) {
        setActuator(c, Value::makeBool(false), now, false);
        dirty_ = true;
        logf("max_on_s reached on %s/%s: forced off", ep->localId.c_str(), c.id.c_str());
      }
    }
  }
  // failsafe: after failsafe_after_s without the hub connection, apply failsafe_value once per outage
  if (hubConnected) {
    if (!hubUp_) logf("hub connection up");
    hubUp_ = true;
    failsafeDone_ = false;
  } else {
    if (hubUp_) { hubUp_ = false; discSince_ = now; }
    if (!failsafeDone_ && m_.failsafeAfterS > 0 && elapsedMs(now, discSince_) >= m_.failsafeAfterS * 1000u) {
      failsafeDone_ = true;
      if (failsafeNow(now)) { dirty_ = true; logf("failsafe applied after %us without hub", (unsigned)m_.failsafeAfterS); }
    }
  }
  return dirty_;
}

// ---- sensors -----------------------------------------------------------------------------------------

bool NodeCore::sensorDue(size_t k, uint32_t now) const {
  const CapDef& c = *sensors_[k];
  if (!c.sampled) return true;
  uint32_t period = c.sens->pollMs() ? c.sens->pollMs() : c.intervalS * 1000u;
  return elapsedMs(now, c.lastSample) >= period;
}

bool NodeCore::submitSensor(size_t k, const Value& vin, uint32_t now) {
  CapDef& c = *sensors_[k];
  c.sampled = true;
  c.lastSample = now;
  Value v = vin;
  if (v.type == CapType::Float) {
    if (!isfinite(v.f)) return false;
    v.f = roundf(v.f * 100.0f) / 100.0f;
  }
  bool changed = !c.haveCur;
  if (!changed) {
    if (c.type == CapType::Float) changed = fabsf(v.f - c.cur.f) >= (c.deadband > 0 ? c.deadband : 0.01f);
    else if (c.type == CapType::Int) changed = abs(v.i - c.cur.i) >= (c.deadband >= 1 ? (int)c.deadband : 1);
    else changed = v != c.cur;
  }
  if (changed) { c.cur = v; c.haveCur = true; dirty_ = true; }
  return changed;
}

uint32_t NodeCore::effectiveHeartbeatS() const {
  uint32_t h = m_.heartbeatS;
  for (const CapDef* c : sensors_) if (c->intervalS < h) h = c->intervalS;
  return h < 5 ? 5 : h;
}

// ---- JSON out ----------------------------------------------------------------------------------------

static void putNumber(JsonObject o, const char* key, CapType t, float f) {
  if (t == CapType::Float) o[key] = f;
  else o[key] = (int32_t)lroundf(f);
}

static void putValue(JsonObject o, const char* key, const CapDef& c, const Value& v) {
  switch (c.type) {
    case CapType::Bool: o[key] = v.b; break;
    case CapType::Int: o[key] = v.i; break;
    case CapType::Float: o[key] = v.f; break;
    default:
      if (v.i >= 0 && (size_t)v.i < c.values.size()) o[key] = c.values[(size_t)v.i];
      break;
  }
}

std::string NodeCore::announceJson(const std::string& nodeId) const {
  JsonDocument doc;
  doc["schema"] = 2;
  doc["node_id"] = nodeId;
  doc["hw"] = m_.hw;
  doc["fw"] = m_.fw;
  doc["failsafe_after_s"] = m_.failsafeAfterS;
  JsonArray eps = doc["endpoints"].to<JsonArray>();
  for (const auto& ep : m_.endpoints) {
    JsonObject eo = eps.add<JsonObject>();
    eo["local_id"] = ep->localId;
    eo["type"] = epTypeName(ep->type);
    JsonArray caps = eo["capabilities"].to<JsonArray>();
    for (const CapDef& c : ep->caps) {
      JsonObject co = caps.add<JsonObject>();
      co["id"] = c.id;
      co["kind"] = c.kind == CapKind::Actuator ? "actuator" : "sensor";
      co["type"] = capTypeName(c.type);
      if (c.type == CapType::Int || c.type == CapType::Float) {
        if (c.hasRange) {
          putNumber(co, "min", c.type, c.lo);
          putNumber(co, "max", c.type, c.hi);
        }
      }
      if (c.type == CapType::Enum) {
        JsonArray vs = co["values"].to<JsonArray>();
        for (const std::string& s : c.values) vs.add(s);
      }
      if (!c.unit.empty()) co["unit"] = c.unit;
      if (c.kind == CapKind::Sensor) {
        co["interval_s"] = c.intervalS;
      } else {
        putValue(co, "boot_value", c, c.boot);
        if (c.hasFailsafe) putValue(co, "failsafe_value", c, c.failsafe);
        if (c.type == CapType::Bool) co["max_on_s"] = c.maxOnS;
        if (c.settleS != 0) co["settle_s"] = c.settleS;
      }
    }
  }
  std::string out;
  serializeJson(doc, out);
  return out;
}

bool NodeCore::stateJson(int64_t ts, std::string& out) const {
  JsonDocument doc;
  doc["ts"] = (int64_t)ts;
  JsonObject eps = doc["endpoints"].to<JsonObject>();
  size_t n = 0;
  for (const auto& ep : m_.endpoints) {
    JsonObject eo;
    for (const CapDef& c : ep->caps) {
      if (!c.haveCur) continue;
      if (eo.isNull()) eo = eps[ep->localId].to<JsonObject>();
      putValue(eo, c.id.c_str(), c, c.cur);
    }
    if (!eo.isNull()) n++;
  }
  if (n == 0) return false;
  out.clear();
  serializeJson(doc, out);
  return true;
}

const Value* NodeCore::value(const char* endpoint, const char* cap) const {
  int e = m_.findEndpoint(endpoint);
  if (e < 0) return nullptr;
  int c = m_.endpoints[(size_t)e]->findCap(cap);
  if (c < 0 || !m_.endpoints[(size_t)e]->caps[(size_t)c].haveCur) return nullptr;
  return &m_.endpoints[(size_t)e]->caps[(size_t)c].cur;
}

bool NodeCore::ledOn(uint32_t now) {
  if (identifying_) {
    if ((int32_t)(now - identifyUntil_) >= 0) identifying_ = false;
    else return (now / 100) % 2 == 0;
  }
  switch (phase_) {
    case NetPhase::Unconfigured: return (now / 250) % 2 == 0;   // 2 Hz: waiting for configuration
    case NetPhase::Provisioning: return (now / 125) % 2 == 0;   // 4 Hz: BLE provisioning window open
    case NetPhase::Wifi:
    case NetPhase::Ntp:
    case NetPhase::Mqtt: return (now % 1000) < 100;             // short flash each second: connecting
    case NetPhase::Error: return true;                          // solid: invalid declaration
    default: return false;                                      // running: dark
  }
}

// ---- commands -----------------------------------------------------------------------------------------

namespace {

const char* kErr[] = {nullptr, "unknown_endpoint", "unknown_capability", "out_of_range", "bad_type", "busy", "unsupported", "bad_request"};
enum E { OK = 0, UNKNOWN_ENDPOINT, UNKNOWN_CAPABILITY, OUT_OF_RANGE, BAD_TYPE, BUSY, UNSUPPORTED, BAD_REQUEST };

struct Pending {
  CapDef* cap;
  Value v;
};

std::string buildAck(const char* cmdId, int err, const char* endpoint, const std::vector<Pending>* applied) {
  JsonDocument doc;
  doc["cmd_id"] = cmdId;
  doc["ok"] = err == OK;
  if (endpoint) doc["endpoint"] = endpoint; else doc["endpoint"] = nullptr;
  JsonObject vals = doc["values"].to<JsonObject>();
  if (err == OK && applied)
    for (const Pending& p : *applied) putValue(vals, p.cap->id.c_str(), *p.cap, p.v);
  if (err == OK) doc["error"] = nullptr; else doc["error"] = kErr[err];
  std::string out;
  serializeJson(doc, out);
  return out;
}

int parseValue(const CapDef& c, JsonVariantConst jv, Value& out) {
  switch (c.type) {
    case CapType::Bool:
      if (!jv.is<bool>()) return BAD_TYPE;
      out = Value::makeBool(jv.as<bool>());
      return OK;
    case CapType::Int: {
      double d;
      if (jv.is<long>()) d = (double)jv.as<long>();
      else if (jv.is<double>()) { d = jv.as<double>(); if (d != floor(d)) return BAD_TYPE; }
      else return BAD_TYPE;
      if (d < c.lo || d > c.hi) return OUT_OF_RANGE;
      out = Value::makeInt((int32_t)d);
      return OK;
    }
    case CapType::Float: {
      if (!jv.is<double>()) return BAD_TYPE;
      double d = jv.as<double>();
      if (!isfinite(d)) return BAD_TYPE;
      if (d < c.lo || d > c.hi) return OUT_OF_RANGE;
      out = Value::makeFloat((float)d);
      return OK;
    }
    default: {
      if (!jv.is<const char*>()) return BAD_TYPE;
      int idx = c.enumIndex(jv.as<const char*>());
      if (idx < 0) return OUT_OF_RANGE;
      out = Value::makeEnum(idx);
      return OK;
    }
  }
}

}  // namespace

NodeCore::Outcome NodeCore::handleCommand(const char* payload, size_t len, uint32_t now) {
  Outcome o;
  if (!started_ || !payload || len == 0 || len > 1024) return o;
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return o;
  if (!doc.is<JsonObject>() || !doc["cmd_id"].is<const char*>()) return o;
  const char* cmdId = doc["cmd_id"].as<const char*>();
  if (!isValidUuid(cmdId)) return o;  // the ack schema requires a uuid: nothing sensible to acknowledge
  o.hasAck = true;

  if (const std::string* prev = dedup_.find(cmdId)) {
    o.ack = *prev;
    o.replay = true;
    return o;
  }

  auto finish = [&](int err, const char* endpoint, const std::vector<Pending>* applied) {
    o.ack = buildAck(cmdId, err, endpoint, applied);
    dedup_.remember(cmdId, o.ack);
    return o;
  };

  if (doc["cmd"].is<const char*>()) {
    const char* name = doc["cmd"].as<const char*>();
    if (strcmp(name, "identify") == 0) {
      JsonVariantConst d = doc["duration_s"];
      if (!d.is<int>() || d.as<int>() < 1 || d.as<int>() > 60) return finish(BAD_REQUEST, nullptr, nullptr);
      o.action = Outcome::Identify;
      o.identifyS = (uint32_t)d.as<int>();
      return finish(OK, nullptr, nullptr);
    }
    if (strcmp(name, "unprovision") == 0) {
      o.action = Outcome::Unprovision;
      return finish(OK, nullptr, nullptr);
    }
    return finish(UNSUPPORTED, nullptr, nullptr);
  }

  if (!doc["endpoint"].is<const char*>() || !doc["set"].is<JsonObjectConst>() || !doc["by"].is<const char*>())
    return finish(BAD_REQUEST, nullptr, nullptr);
  const char* epName = doc["endpoint"].as<const char*>();
  const char* echoEp = isValidLocalId(epName) ? epName : nullptr;
  if (!isValidBy(doc["by"].as<const char*>())) return finish(BAD_REQUEST, echoEp, nullptr);
  JsonObjectConst set = doc["set"].as<JsonObjectConst>();
  if (set.size() == 0 || set.size() > 16) return finish(BAD_REQUEST, echoEp, nullptr);

  int ei = m_.findEndpoint(epName);
  if (ei < 0) return finish(UNKNOWN_ENDPOINT, echoEp, nullptr);
  EndpointDef& ep = *m_.endpoints[(size_t)ei];

  // Validate everything first; apply nothing unless the whole command is valid.
  std::vector<Pending> todo;
  for (JsonPairConst kv : set) {
    int ci = ep.findCap(kv.key().c_str());
    if (ci < 0) return finish(UNKNOWN_CAPABILITY, ep.localId.c_str(), nullptr);
    CapDef& c = ep.caps[(size_t)ci];
    if (c.kind != CapKind::Actuator) return finish(UNSUPPORTED, ep.localId.c_str(), nullptr);
    Value v;
    int e = parseValue(c, kv.value(), v);
    if (e != OK) return finish(e, ep.localId.c_str(), nullptr);
    todo.push_back({&c, v});
  }
  for (const Pending& p : todo) {
    p.cap->localOverride = false;  // the hub is in charge of this capability again: failsafe re-armed
    ApplyResult r = setActuator(*p.cap, p.v, now, false);
    if (r == ApplyResult::Unsupported) return finish(UNSUPPORTED, ep.localId.c_str(), nullptr);
    if (r == ApplyResult::Busy) return finish(BUSY, ep.localId.c_str(), nullptr);
  }
  dirty_ = true;
  o.stateChanged = true;
  return finish(OK, ep.localId.c_str(), &todo);
}

}  // namespace edge
