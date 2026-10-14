// The fixed capability vocabulary (contracts/capabilities.md, contract version 1).
// A device can never invent a capability: the declarative API rejects unknown ids.
#pragma once
#include <math.h>
#include <string>
#include <vector>
#include "Value.h"

namespace edge {

enum class EpType : uint8_t { Light, Fan, Outlet, Lock, Cover, Thermostat, Sensor, Generic };

// Endpoint type constants for the declarative API: node.endpoint("ch1", FAN)
constexpr EpType LIGHT = EpType::Light;
constexpr EpType FAN = EpType::Fan;
constexpr EpType OUTLET = EpType::Outlet;
constexpr EpType LOCK = EpType::Lock;
constexpr EpType COVER = EpType::Cover;
constexpr EpType THERMOSTAT = EpType::Thermostat;
constexpr EpType SENSOR = EpType::Sensor;
constexpr EpType GENERIC = EpType::Generic;

const char* epTypeName(EpType t);
const char* capTypeName(CapType t);

struct CapSpec {
  const char* id;
  CapKind kind;
  CapType type;
  bool rangeRequired;      // announce must carry min/max (speed)
  bool valuesRequired;     // announce must carry values (mode)
  const char* defaultUnit; // nullptr = none
  float hardLo, hardHi;    // allowed range for min/max, NAN = unconstrained
  const char* fixedV0;     // fixed enum values (lock, contact), else nullptr
  const char* fixedV1;
  float deadband;          // default change threshold for sensors
};

const CapSpec* findCapSpec(const char* id);
bool unitAllowed(const std::string& unit);
bool isValidLocalId(const std::string& s);   // ^[a-z0-9_]{1,16}$
bool isValidEnumValue(const std::string& s); // ^[a-z0-9_]{1,24}$
bool isValidHwString(const std::string& s);  // ^[A-Za-z0-9._-]{1,32}$
bool isValidFwString(const std::string& s);  // ^[A-Za-z0-9.+-]{1,32}$

}  // namespace edge
