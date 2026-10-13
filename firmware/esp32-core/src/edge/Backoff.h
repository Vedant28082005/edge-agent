// Exponential reconnect backoff with jitter. A connection that dies quickly does not reset the
// schedule, so a flapping link cannot hammer the broker or the Wi-Fi AP.
#pragma once
#include <stdint.h>

namespace edge {

class Backoff {
 public:
  Backoff(uint32_t baseMs = 1000, uint32_t maxMs = 60000, uint32_t stableMs = 30000)
      : base_(baseMs), max_(maxMs), stable_(stableMs) {}

  // Delay before the next attempt. `rnd` is any 32-bit random number (esp_random() on the board).
  uint32_t nextDelayMs(uint32_t rnd) {
    uint64_t d = base_;
    for (uint32_t k = 0; k < attempts_ && d < max_; k++) d *= 2;
    if (d > max_) d = max_;
    attempts_++;
    uint32_t jitter = (uint32_t)(d / 5);                         // +-20 %
    int64_t j = jitter ? (int64_t)(rnd % (2 * jitter + 1)) - (int64_t)jitter : 0;
    int64_t out = (int64_t)d + j;
    if (out < 100) out = 100;
    return (uint32_t)out;
  }
  void onConnected(uint32_t now) { upSince_ = now; up_ = true; }
  void onDisconnected(uint32_t now) {
    if (up_ && (uint32_t)(now - upSince_) >= stable_) attempts_ = 0;  // it was a real, stable connection
    up_ = false;
  }
  uint32_t attempts() const { return attempts_; }
  void reset() { attempts_ = 0; up_ = false; }

 private:
  uint32_t base_, max_, stable_;
  uint32_t attempts_ = 0;
  bool up_ = false;
  uint32_t upSince_ = 0;
};

}  // namespace edge
