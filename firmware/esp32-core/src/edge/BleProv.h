// BLE provisioning state machine and protocol (contracts/ble-provisioning.md, proto 1), without any radio code:
// the GATT glue (BleProvEsp.cpp) only forwards reads/writes here, so every rule below is host-tested.
//
// Security rules implemented here (S2b):
//  * Window only when the node has no credentials (first boot / after factory reset) or after a physical
//    button hold; 5 minutes, then BLE is off until the next button hold or power cycle.
//  * Proof of possession: a per-board secret (printed label / QR, kept in NVS namespace "edgeid", NOT wiped by
//    unprovision) is mixed into the HKDF info. A client with the wrong PoP derives another key, the GCM tag check
//    fails and that counts as a failed attempt. `requirePop` (default) refuses to open BLE for a board without PoP.
//    The PoP is never sent over the air (a non-empty `pop` field in the session write is rejected).
//  * maxFailures failed attempts lock BLE for lockMs.
//  * Secrets are never logged, never returned over BLE, never in advertising data; key and buffers are zeroed.
#pragma once
#include <stdint.h>
#include <string>
#include "NodeConfig.h"
#include "ProvCrypto.h"

namespace edge {

struct ProvPolicy {
  uint32_t windowMs = 300000;      // contract: 5 minutes
  int maxFailures = 5;             // failed PoP/tag attempts before the lock
  uint32_t lockMs = 300000;        // cool-down
  uint32_t idleMs = 30000;         // contract: abort a transfer after 30 s without a chunk
  bool requirePop = true;          // refuse to open BLE when the board has no PoP
  uint32_t buttonHoldMs = 3000;    // open a window
  uint32_t factoryHoldMs = 10000;  // wipe the config (contract: unprovision by 10 s hold)
};

enum class ProvState : uint8_t { Closed, Open, Session, Receiving, Stored, Locked };

// Detects the BOOT button holds. Feed it the (debounced) pressed state every few ms.
class HoldDetector {
 public:
  enum Event : uint8_t { None, Hold3s, Hold10s };
  HoldDetector(uint32_t shortMs = 3000, uint32_t longMs = 10000) : s_(shortMs), l_(longMs) {}
  Event feed(bool pressed, uint32_t now) {
    if (!pressed) { down_ = false; firedS_ = firedL_ = false; return None; }
    if (!down_) { down_ = true; since_ = now; return None; }
    uint32_t held = now - since_;
    if (held >= l_ && !firedL_) { firedL_ = true; return Hold10s; }
    if (held >= s_ && !firedS_) { firedS_ = true; return Hold3s; }
    return None;
  }

 private:
  uint32_t s_, l_, since_ = 0;
  bool down_ = false, firedS_ = false, firedL_ = false;
};

class BleProv {
 public:
  BleProv(ProvCrypto& crypto, ConfigStore& store, const std::string& nodeId, const std::string& pop,
          const std::string& hw, const std::string& fw, const ProvPolicy& policy = ProvPolicy());
  ~BleProv();

  // Boot: opens the window when the node has no credentials. Returns whether BLE is now on.
  bool boot(uint32_t now, bool hasCreds);
  // Physical button held 3 s: open (or re-open) the window. False if refused (no PoP while required, locked).
  bool buttonHold(uint32_t now);
  // Button held 10 s: wipe the stored config (not the PoP) and open a fresh window. Caller reboots.
  bool factoryReset(uint32_t now);
  void tick(uint32_t now);

  bool advertising() const { return state_ == ProvState::Open || state_ == ProvState::Session || state_ == ProvState::Receiving; }
  ProvState state() const { return state_; }
  int failures() const { return failures_; }

  // ---- GATT surface -----------------------------------------------------------------------------
  std::string info(uint32_t now) const;  // read `info`
  std::string result() const;            // read/notify `result`
  // write `session`; on true `indicate` holds the board's reply JSON.
  bool writeSession(const uint8_t* d, size_t n, uint32_t now, std::string& indicate);
  // write `data` (one chunk); `notify` receives the 3-byte chunk ack [seq u16 BE][status u8].
  bool writeData(const uint8_t* d, size_t n, uint32_t now, uint8_t notify[3]);
  void clientDisconnected(uint32_t now);  // drops the session and any partial data

  // True once after the bundle was stored: caller turns BLE off and reboots.
  bool takeStored() { bool s = storedPending_; storedPending_ = false; return s; }
  // Last result code ("ok" | "bad_session" | ...).
  const char* code() const { return code_; }

 private:
  void open(uint32_t now);
  void closeWindow();
  void dropSession();
  void fail(const char* code, bool countAttempt, uint32_t now);
  bool finish(uint32_t now);

  ProvCrypto& c_;
  ConfigStore& store_;
  std::string nodeId_, pop_, hw_, fw_;
  ProvPolicy p_;
  ProvState state_ = ProvState::Closed;
  bool hasCreds_ = false;
  uint32_t windowEnd_ = 0, lockEnd_ = 0, lastChunk_ = 0;
  int failures_ = 0;
  uint8_t key_[32];
  bool haveKey_ = false;
  uint16_t nextSeq_ = 0;
  std::string blob_;
  const char* code_ = "ok";
  bool storedPending_ = false;
};

// Bundle JSON (proto 1) -> NodeConfig; false (and `why` = field name, never a value) if invalid.
bool parseBundle(const std::string& json, const std::string& nodeId, NodeConfig& out, std::string& why);

}  // namespace edge
