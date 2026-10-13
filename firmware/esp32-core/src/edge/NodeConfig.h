// Stored node configuration (written to NVS) and the serial configuration shell that edits it.
// BLE provisioning (S2b) will fill the same NodeConfig; nothing here depends on how it got there.
// Secrets (Wi-Fi password, MQTT password, CA PEM) are never printed or logged.
#pragma once
#include <stdint.h>
#include <string>

namespace edge {

struct NodeConfig {
  std::string wifiSsid, wifiPass;
  std::string mqttHost;
  uint16_t mqttPort = 8883;
  std::string mqttUser, mqttPass;
  std::string ntpHost;  // optional LAN NTP server, tried before pool.ntp.org
  std::string caPem;    // CA certificate that signed the broker certificate

  bool complete() const { return !wifiSsid.empty() && !mqttHost.empty() && !mqttUser.empty() && !mqttPass.empty() && !caPem.empty() && mqttPort != 0; }
  // Empty string if valid, otherwise the name of the first bad field.
  std::string validate() const;
};

class ConfigStore {
 public:
  virtual ~ConfigStore() {}
  virtual bool load(NodeConfig& out) = 0;  // false: nothing stored
  virtual bool save(const NodeConfig& c) = 0;
  virtual bool wipe() = 0;
};

class MemConfigStore : public ConfigStore {
 public:
  bool has = false;
  NodeConfig cfg;
  bool load(NodeConfig& out) override { if (!has) return false; out = cfg; return true; }
  bool save(const NodeConfig& c) override { cfg = c; has = true; return true; }
  bool wipe() override { cfg = NodeConfig(); has = false; return true; }
};

// Line-based shell for the USB serial port:
//   status | set <key> <value> | ca clear | ca line <pem line> | ca end | save | wipe | reboot | help
// keys: wifi_ssid wifi_pass mqtt_host mqtt_port mqtt_user mqtt_pass ntp_host
class ConfigShell {
 public:
  explicit ConfigShell(ConfigStore& store);
  // Returns the reply line. Sets reboot when the caller should restart the chip after printing it.
  std::string handleLine(const std::string& line, bool& reboot);
  const NodeConfig& draft() const { return draft_; }

 private:
  ConfigStore& store_;
  NodeConfig draft_;
  std::string caBuf_;
  bool inCa_ = false;
};

}  // namespace edge
