#pragma once
#ifdef ARDUINO
#include <Preferences.h>
#include "NodeConfig.h"

namespace edge {

// NVS namespace "edgecfg". The Wi-Fi/MQTT passwords and the CA live only here: never logged, never printed.
class NvsConfigStore : public ConfigStore {
 public:
  bool load(NodeConfig& out) override {
    Preferences p;
    if (!p.begin("edgecfg", true)) return false;
    bool has = p.isKey("mqtt_host");
    if (has) {
      out.wifiSsid = p.getString("wifi_ssid", "").c_str();
      out.wifiPass = p.getString("wifi_pass", "").c_str();
      out.mqttHost = p.getString("mqtt_host", "").c_str();
      out.mqttPort = p.getUShort("mqtt_port", 8883);
      out.mqttUser = p.getString("mqtt_user", "").c_str();
      out.mqttPass = p.getString("mqtt_pass", "").c_str();
      out.ntpHost = p.getString("ntp_host", "").c_str();
      out.caPem = p.getString("ca_pem", "").c_str();
    }
    p.end();
    return has;
  }
  bool save(const NodeConfig& c) override {
    Preferences p;
    if (!p.begin("edgecfg", false)) return false;
    bool ok = p.putString("wifi_ssid", c.wifiSsid.c_str()) > 0;
    p.putString("wifi_pass", c.wifiPass.c_str());
    ok = ok && p.putString("mqtt_host", c.mqttHost.c_str()) > 0;
    ok = ok && p.putUShort("mqtt_port", c.mqttPort) > 0;
    ok = ok && p.putString("mqtt_user", c.mqttUser.c_str()) > 0;
    ok = ok && p.putString("mqtt_pass", c.mqttPass.c_str()) > 0;
    p.putString("ntp_host", c.ntpHost.c_str());
    ok = ok && p.putString("ca_pem", c.caPem.c_str()) > 0;
    p.end();
    return ok;
  }
  bool wipe() override {
    Preferences p;
    if (!p.begin("edgecfg", false)) return false;
    bool ok = p.clear();
    p.end();
    return ok;
  }
};

}  // namespace edge
#endif
