// Remembers the last N command ids together with the ack that was sent, so a retried command
// (same cmd_id) is acknowledged again without being applied twice.
#pragma once
#include <string.h>
#include <string>

namespace edge {

class CmdDedup {
 public:
  static const int kSize = 16;

  // Returns the stored ack for a known cmd_id, or nullptr.
  const std::string* find(const char* cmdId) const {
    for (int k = 0; k < kSize; k++)
      if (used_[k] && strncmp(ids_[k], cmdId, sizeof ids_[k]) == 0) return &acks_[k];
    return nullptr;
  }
  void remember(const char* cmdId, const std::string& ack) {
    strncpy(ids_[next_], cmdId, sizeof ids_[next_] - 1);
    ids_[next_][sizeof ids_[next_] - 1] = 0;
    acks_[next_] = ack;
    used_[next_] = true;
    next_ = (next_ + 1) % kSize;
  }
  int count() const { int n = 0; for (int k = 0; k < kSize; k++) n += used_[k]; return n; }

 private:
  char ids_[kSize][40] = {{0}};
  std::string acks_[kSize];
  bool used_[kSize] = {false};
  int next_ = 0;
};

}  // namespace edge
