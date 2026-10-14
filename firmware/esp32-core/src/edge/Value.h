// A typed capability value. Enums are stored as an index into the capability's value list.
#pragma once
#include <stdint.h>
#include <math.h>

namespace edge {

enum class CapKind : uint8_t { Actuator, Sensor };
enum class CapType : uint8_t { Bool, Int, Float, Enum };

struct Value {
  CapType type = CapType::Bool;
  bool b = false;
  int32_t i = 0;  // Int, or Enum index
  float f = 0.0f;

  static Value makeBool(bool v) { Value x; x.type = CapType::Bool; x.b = v; return x; }
  static Value makeInt(int32_t v) { Value x; x.type = CapType::Int; x.i = v; return x; }
  static Value makeFloat(float v) { Value x; x.type = CapType::Float; x.f = v; return x; }
  static Value makeEnum(int32_t idx) { Value x; x.type = CapType::Enum; x.i = idx; return x; }

  float asFloat() const {
    switch (type) {
      case CapType::Bool: return b ? 1.0f : 0.0f;
      case CapType::Float: return f;
      default: return (float)i;
    }
  }
  bool operator==(const Value& o) const {
    if (type != o.type) return false;
    switch (type) {
      case CapType::Bool: return b == o.b;
      case CapType::Float: return f == o.f;
      default: return i == o.i;
    }
  }
  bool operator!=(const Value& o) const { return !(*this == o); }
};

// Result of asking a driver to apply a value.
enum class ApplyResult : uint8_t { Ok, Unsupported, Busy };

// Unsigned subtraction keeps this correct across the 49-day millis() wrap.
inline uint32_t elapsedMs(uint32_t now, uint32_t since) { return now - since; }

}  // namespace edge
