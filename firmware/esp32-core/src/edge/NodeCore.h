// The pure logic of a node: boot values, command handling, safety timers, local buttons, sensor
// bookkeeping, announce and state JSON. No Wi-Fi, no MQTT, no RTOS, so it runs (and is tested) on the host.
// Not thread-safe: EdgeNode serialises access with a mutex.
#pragma once
#include <stddef.h>
#include <string>
#include "CmdDedup.h"
#include "NodeModel.h"

namespace edge {

enum class NetPhase : uint8_t { Unconfigured, Wifi, Ntp, Mqtt, Run, Error };

class NodeCore {
 public:
  explicit NodeCore(NodeModel& m) : m_(m) {}

  // Validates the declaration, configures the hardware and applies every boot_value.
  // Returns false (touching no pin) if the declaration is invalid.
  bool begin(uint32_t now);

  struct Outcome {
    bool hasAck = false;     // false: the payload had no usable cmd_id, nothing can be acknowledged
    std::string ack;         // ack payload (contracts/ack.schema.json)
    bool replay = false;     // duplicate cmd_id: ack re-sent, nothing applied
    bool stateChanged = false;
    enum Action : uint8_t { None, Identify, Unprovision } action = None;
    uint32_t identifyS = 0;
  };
  Outcome handleCommand(const char* payload, size_t len, uint32_t now);

  // Call every ~20-50 ms. hubConnected = the MQTT session to the hub broker is up.
  // Runs local buttons, driver ticks, max_on_s and the failsafe. Returns true if the state changed.
  bool tick(uint32_t now, bool hubConnected);

  // ---- sensors (the caller reads the driver outside any lock, then submits) ----------------------
  size_t sensorCount() const { return sensors_.size(); }
  bool sensorDue(size_t k, uint32_t now) const;
  SensorDriver* sensorDriver(size_t k) { return sensors_[k]->sens.get(); }
  // Returns true if the published value changed beyond the deadband.
  bool submitSensor(size_t k, const Value& v, uint32_t now);
  void markSampled(size_t k, uint32_t now) { sensors_[k]->sampled = true; sensors_[k]->lastSample = now; }

  // ---- output --------------------------------------------------------------------------------------
  std::string announceJson(const std::string& nodeId) const;
  // false if there is nothing to report yet (e.g. a sensor-only node before the first reading)
  bool stateJson(int64_t ts, std::string& out) const;
  // Heartbeat actually used: the configured one, capped by the fastest sensor interval.
  uint32_t effectiveHeartbeatS() const;

  bool dirty() const { return dirty_; }
  bool takeDirty() { bool d = dirty_; dirty_ = false; return d; }
  void setDirty() { dirty_ = true; }

  // ---- status LED ----------------------------------------------------------------------------------
  void setPhase(NetPhase p) { phase_ = p; }
  NetPhase phase() const { return phase_; }
  void startIdentify(uint32_t now, uint32_t seconds) { identifyUntil_ = now + seconds * 1000u; identifying_ = true; }
  bool ledOn(uint32_t now);

  // ---- inspection (tests, serial status) -------------------------------------------------------------
  const Value* value(const char* endpoint, const char* cap) const;
  bool failsafeApplied() const { return failsafeDone_; }
  bool started() const { return started_; }
  NodeModel& model() { return m_; }

 private:
  ApplyResult setActuator(CapDef& c, const Value& v, uint32_t now, bool boot);
  bool failsafeNow(uint32_t now);
  void runButtons(EndpointDef& ep, uint32_t now);

  NodeModel& m_;
  CmdDedup dedup_;
  std::vector<CapDef*> sensors_;
  bool started_ = false;
  bool dirty_ = false;
  bool hubUp_ = false, failsafeDone_ = false;
  uint32_t discSince_ = 0;
  NetPhase phase_ = NetPhase::Unconfigured;
  bool identifying_ = false;
  uint32_t identifyUntil_ = 0;
};

// What the node does on EVERY successful MQTT connect, in this order (contracts/mqtt.md, "Connect lifecycle").
enum class ConnectStep : uint8_t { StatusOnline, Announce, SubscribeCmd, State };
inline const ConnectStep* connectSequence(size_t& n) {
  static const ConnectStep seq[] = {ConnectStep::StatusOnline, ConnectStep::Announce, ConnectStep::SubscribeCmd, ConnectStep::State};
  n = sizeof seq / sizeof seq[0];
  return seq;
}
// client_id == username == node_id, always.
inline bool mqttIdentityOk(const std::string& nodeId, const std::string& user) { return !nodeId.empty() && nodeId == user; }

// Command field helpers, exposed for tests.
bool isValidUuid(const char* s);
bool isValidBy(const char* s);

}  // namespace edge
