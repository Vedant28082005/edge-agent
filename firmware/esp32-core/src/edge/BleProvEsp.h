// ESP32-only glue for BLE provisioning: NimBLE GATT server (UUIDs from contracts/ble-provisioning.md), mbedtls
// crypto, PoP storage. All rules live in BleProv (host-tested); this file only forwards bytes.
// NOT compiled by the host tests: it needs the Arduino-ESP32 core and NimBLE-Arduino.
#pragma once
#ifdef ARDUINO
#include <string>
#include "BleProv.h"

namespace edge {

// mbedtls-backed primitives (X25519, HKDF-SHA256, AES-256-GCM), hardware RNG.
ProvCrypto& mbedProvCrypto();

// Per-board PoP lives in NVS namespace "edgeid" (survives `wipe` of the config namespace "edgecfg").
// Set once at manufacturing through the serial shell ("pop set <value>"); never printed, never logged.
std::string loadPop();
bool storePop(const std::string& pop);
bool validPopFormat(const std::string& pop);  // 16..64 chars of [A-Za-z2-7]; the factory tool generates 26 (128 bit)

class BleProvEsp {
 public:
  // `prov` is shared with the BLE task; all access goes through lock().
  BleProvEsp(BleProv& prov, const std::string& nodeId);
  void start();  // NimBLE init, GATT service, advertising "Setup-XXXX" + service UUID (no secret, no full node_id)
  void stop();   // advertising off, NimBLE deinit (releases the controller memory)
  bool running() const { return running_; }
  // Guards BleProv between the NimBLE host task (writes) and the BLE housekeeping task.
  void lock();
  void unlock();
  BleProv& prov() { return prov_; }
  void pushResult();  // notify `result` with the current state

 private:
  friend struct ProvCallbacks;
  BleProv& prov_;
  std::string nodeId_;
  bool running_ = false;
  void* mu_ = nullptr;
  void* chSession_ = nullptr;
  void* chData_ = nullptr;
  void* chResult_ = nullptr;
  void* chInfo_ = nullptr;
};

}  // namespace edge
#endif
