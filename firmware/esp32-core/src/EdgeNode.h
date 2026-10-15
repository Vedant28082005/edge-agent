// Public header of the ESP32 core library.
//
//   #include <EdgeNode.h>
//   using namespace edge;
//   EdgeNode node;
//   void setup() {
//     node.endpoint("ch1", LIGHT).boolCap("power", RelayOut(26)).localToggle("power", 33);
//     node.begin();
//   }
//   void loop() { node.loop(); }
#pragma once
#include <stdint.h>
#include <string>
#include "edge/Backoff.h"
#include "edge/BleProv.h"
#include "edge/CmdDedup.h"
#include "edge/Drivers.h"
#include "edge/NodeConfig.h"
#include "edge/NodeCore.h"
#include "edge/NodeModel.h"
#include "edge/TimeGate.h"
#include "edge/Vocabulary.h"
#include "edge/drivers/Curtain.h"
#include "edge/drivers/Dht.h"
#include "edge/drivers/Inputs.h"
#include "edge/drivers/Pwm.h"
#include "edge/drivers/Relay.h"

#ifdef ARDUINO
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#endif

namespace edge {

// node_id = "esp32-" + the 12 lowercase hex digits of the Wi-Fi station MAC.
std::string nodeIdFromMac(const uint8_t mac[6]);

// Firmware build time (Unix seconds, UTC), injected by the build. 0 on the host unless set by a test.
int64_t fwBuildTs();
void setFwBuildTsForTest(int64_t ts);

class EdgeNode {
 public:
  EdgeNode();

  // ---- declaration -------------------------------------------------------------------------------
  EndpointBuilder endpoint(const char* localId, EpType type);
  EdgeNode& hw(const char* name) { model_.hw = name; return *this; }
  EdgeNode& firmwareVersion(const char* v) { model_.fw = v; return *this; }
  // Seconds without the hub connection before every failsafe_value is applied (0 = keep last values).
  EdgeNode& failsafeAfter(uint32_t seconds) { model_.failsafeAfterS = seconds; return *this; }
  EdgeNode& heartbeat(uint32_t seconds) { model_.heartbeatS = seconds; return *this; }
  // GPIO of the provisioning button (active low, internal pull-up; default 0 = BOOT on devkits). Held 3 s: open
  // the BLE provisioning window; held 10 s: factory reset (wipe the stored config, keep the PoP).
  EdgeNode& provisionButton(int pin) { provButtonPin_ = pin; return *this; }
  // Policy knobs (tests, bring-up): defaults are 5 min window, 5 failures, 5 min lock, PoP required.
  EdgeNode& provisionPolicy(const ProvPolicy& p) { provPolicy_ = p; return *this; }
  EdgeNode& statusLed(int pin, bool activeHigh = true) { model_.ledPin = pin; model_.ledActiveHigh = activeHigh; return *this; }

  NodeModel& model() { return model_; }
  NodeCore& core() { return core_; }
  std::string announce(const std::string& nodeId) { return core_.announceJson(nodeId); }

#ifdef ARDUINO
  // Validates the declaration, applies boot values, starts the network/sensor/actuator tasks.
  // Returns false (and prints the reasons) if the declaration is invalid; no pin is touched in that case.
  bool begin();
  // Call from Arduino loop(): services the serial configuration shell.
  void loop();
  const std::string& nodeId() const { return nodeId_; }
#endif

 private:
  int provButtonPin_ = 0;
  ProvPolicy provPolicy_;
  NodeModel model_;
  NodeCore core_;

#ifdef ARDUINO
  struct Lock;
  static void netTaskTramp(void* p);
  static void sensorTaskTramp(void* p);
  static void actuatorTaskTramp(void* p);
  static void bleTaskTramp(void* p);
  void bleTask();
  bool handlePopLine(const std::string& line, std::string& reply);
  void netTask();
  void sensorTask();
  void actuatorTask();
  bool connectWifi();
  bool waitForTime();
  bool mqttConnect();
  bool mqttSetup();
  void runConnected();
  void teardownMqtt(bool publishOffline);
  bool pub(const std::string& topic, const std::string& payload, int qos, bool retain);
  void publishState();
  void drainAcks(bool publish);
  void setPhase(NetPhase p);
  void backoffWait();
  void unprovisionNow();
  uint32_t nowMs() const;

  static void mqttEvent(void* arg, const char* base, int32_t id, void* data);
  void onMqttEvent(int32_t id, void* data);

  std::string nodeId_, tAnnounce_, tStatus_, tState_, tCmd_, tAck_;
  NodeConfig cfg_;
  bool cfgOk_ = false;
  void* store_ = nullptr;    // NvsConfigStore*
  void* shell_ = nullptr;    // ConfigShell*
  void* client_ = nullptr;   // esp_mqtt_client_handle_t
  void* prov_ = nullptr;     // BleProv*
  void* bleEsp_ = nullptr;   // BleProvEsp*
  volatile bool mqttUp_ = false;
  volatile bool mqttFailed_ = false;
  volatile bool unprovPending_ = false;
  std::string rx_;
  bool rxOverflow_ = false;
  std::string line_;
  bool lineOverflow_ = false;
  int bufSize_ = 2048;
  Backoff backoff_;
  QueueHandle_t cmdQ_ = nullptr;
  QueueHandle_t ackQ_ = nullptr;
  SemaphoreHandle_t mu_ = nullptr;
  bool started_ = false;
#endif
};

}  // namespace edge
