// Time plausibility rule (contracts/mqtt.md, "Firmware time requirement").
// Time is valid only if FW_BUILD_TS <= t < FW_BUILD_TS + 50 years. Anything else is "no time":
// the board does not open a TLS connection and never disables certificate date checks to get online.
#pragma once
#include <stdint.h>

namespace edge {

constexpr int64_t kFiftyYearsS = 1577880000LL;  // 50 * 365.25 days

inline bool timePlausible(int64_t t, int64_t buildTs) {
  return buildTs > 0 && t >= buildTs && t < buildTs + kFiftyYearsS;
}

}  // namespace edge
