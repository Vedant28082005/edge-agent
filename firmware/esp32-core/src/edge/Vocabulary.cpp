#include "Vocabulary.h"
#include <string.h>

namespace edge {

static const float NA = NAN;
static const CapSpec kSpecs[] = {
  // id            kind                type             range  values defUnit  lo   hi   v0        v1        deadband
  {"power",        CapKind::Actuator,  CapType::Bool,   false, false, nullptr, NA,  NA,  nullptr,  nullptr,  0},
  {"brightness",   CapKind::Actuator,  CapType::Int,    false, false, "%",     0,   100, nullptr,  nullptr,  0},
  {"color_temp",   CapKind::Actuator,  CapType::Int,    false, false, "K",     NA,  NA,  nullptr,  nullptr,  0},
  {"speed",        CapKind::Actuator,  CapType::Int,    true,  false, "step",  NA,  NA,  nullptr,  nullptr,  0},
  {"position",     CapKind::Actuator,  CapType::Int,    false, false, "%",     0,   100, nullptr,  nullptr,  0},
  {"lock",         CapKind::Actuator,  CapType::Enum,   false, false, nullptr, NA,  NA,  "locked", "unlocked", 0},
  {"target_temp",  CapKind::Actuator,  CapType::Float,  false, false, "C",     NA,  NA,  nullptr,  nullptr,  0},
  {"mode",         CapKind::Actuator,  CapType::Enum,   false, true,  nullptr, NA,  NA,  nullptr,  nullptr,  0},
  {"temperature",  CapKind::Sensor,    CapType::Float,  false, false, "C",     NA,  NA,  nullptr,  nullptr,  0.2f},
  {"humidity",     CapKind::Sensor,    CapType::Float,  false, false, "%",     NA,  NA,  nullptr,  nullptr,  1.0f},
  {"illuminance",  CapKind::Sensor,    CapType::Float,  false, false, "lux",   NA,  NA,  nullptr,  nullptr,  10.0f},
  {"motion",       CapKind::Sensor,    CapType::Bool,   false, false, nullptr, NA,  NA,  nullptr,  nullptr,  0},
  {"contact",      CapKind::Sensor,    CapType::Enum,   false, false, nullptr, NA,  NA,  "open",   "closed", 0},
  {"power_w",      CapKind::Sensor,    CapType::Float,  false, false, "W",     NA,  NA,  nullptr,  nullptr,  1.0f},
  {"current_a",    CapKind::Sensor,    CapType::Float,  false, false, "A",     NA,  NA,  nullptr,  nullptr,  0.05f},
  {"voltage_v",    CapKind::Sensor,    CapType::Float,  false, false, "V",     NA,  NA,  nullptr,  nullptr,  1.0f},
  {"battery",      CapKind::Sensor,    CapType::Int,    false, false, "%",     0,   100, nullptr,  nullptr,  1},
};

const CapSpec* findCapSpec(const char* id) {
  if (!id) return nullptr;
  for (const CapSpec& s : kSpecs)
    if (strcmp(s.id, id) == 0) return &s;
  return nullptr;
}

static const char* kEp[] = {"light", "fan", "outlet", "lock", "cover", "thermostat", "sensor", "generic"};
const char* epTypeName(EpType t) { return kEp[(int)t]; }
const char* capTypeName(CapType t) {
  switch (t) {
    case CapType::Bool: return "bool";
    case CapType::Int: return "int";
    case CapType::Float: return "float";
    default: return "enum";
  }
}

bool unitAllowed(const std::string& u) {
  static const char* ok[] = {"C", "%", "lux", "W", "A", "V", "K", "step"};
  for (const char* s : ok) if (u == s) return true;
  return false;
}

static bool allIn(const std::string& s, size_t maxLen, bool upper, bool dot, bool dash, bool plus) {
  if (s.empty() || s.size() > maxLen) return false;
  for (char c : s) {
    bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || (!dot && !dash && !plus && c == '_');
    if (upper && c >= 'A' && c <= 'Z') ok = true;
    if (dot && c == '.') ok = true;
    if (dash && c == '-') ok = true;
    if (plus && c == '+') ok = true;
    if (upper && c == '_') ok = true;
    if (!ok) return false;
  }
  return true;
}
bool isValidLocalId(const std::string& s) { return allIn(s, 16, false, false, false, false); }
bool isValidEnumValue(const std::string& s) { return allIn(s, 24, false, false, false, false); }
bool isValidHwString(const std::string& s) { return allIn(s, 32, true, true, true, false) ; }
bool isValidFwString(const std::string& s) {
  if (s.empty() || s.size() > 32) return false;
  for (char c : s) {
    bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '.' || c == '+' || c == '-';
    if (!ok) return false;
  }
  return true;
}

}  // namespace edge
