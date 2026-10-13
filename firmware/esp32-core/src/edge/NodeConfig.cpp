#include "NodeConfig.h"
#include <stdlib.h>
#include <string.h>

namespace edge {

static bool hostChars(const std::string& s) {
  for (char c : s) {
    bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '.' || c == '-';
    if (!ok) return false;
  }
  return true;
}

static bool printable(const std::string& s) {
  for (unsigned char c : s) if (c < 0x20 || c == 0x7f) return false;
  return true;
}

std::string NodeConfig::validate() const {
  if (wifiSsid.empty() || wifiSsid.size() > 32 || !printable(wifiSsid)) return "wifi_ssid";
  if (!(wifiPass.empty() || (wifiPass.size() >= 8 && wifiPass.size() <= 63)) || !printable(wifiPass)) return "wifi_pass";
  if (mqttHost.empty() || mqttHost.size() > 64 || !hostChars(mqttHost)) return "mqtt_host";
  if (mqttPort == 0) return "mqtt_port";
  if (mqttUser.empty() || mqttUser.size() > 64 || !printable(mqttUser)) return "mqtt_user";
  if (mqttPass.empty() || mqttPass.size() > 128 || !printable(mqttPass)) return "mqtt_pass";
  if (ntpHost.size() > 64 || !hostChars(ntpHost)) return "ntp_host";
  static const char* kBegin = "-----BEGIN CERTIFICATE-----";
  static const char* kEnd = "-----END CERTIFICATE-----";
  if (caPem.size() < 100 || caPem.size() > 3800) return "ca";
  if (caPem.compare(0, strlen(kBegin), kBegin) != 0) return "ca";
  size_t endPos = caPem.rfind(kEnd);
  if (endPos == std::string::npos) return "ca";
  return "";
}

ConfigShell::ConfigShell(ConfigStore& store) : store_(store) { store_.load(draft_); }

static std::string trimEol(const std::string& s) {
  size_t e = s.size();
  while (e > 0 && (s[e - 1] == '\r' || s[e - 1] == '\n')) e--;
  return s.substr(0, e);
}

std::string ConfigShell::handleLine(const std::string& raw, bool& reboot) {
  reboot = false;
  std::string line = trimEol(raw);
  if (line.size() > 400) return "err too_long";
  size_t sp = line.find(' ');
  std::string cmd = sp == std::string::npos ? line : line.substr(0, sp);
  std::string rest = sp == std::string::npos ? "" : line.substr(sp + 1);

  if (cmd.empty()) return "";
  if (cmd == "help")
    return "commands: status | set <key> <value> | ca clear | ca line <pem line> | ca end | save | wipe | reboot  "
           "keys: wifi_ssid wifi_pass mqtt_host mqtt_port mqtt_user mqtt_pass ntp_host";
  if (cmd == "status") {
    std::string bad = draft_.validate();
    return std::string("config: ") + (draft_.complete() ? "complete" : "incomplete") + (bad.empty() ? "" : " (invalid: " + bad + ")") +
           ", wifi_ssid=" + (draft_.wifiSsid.empty() ? "unset" : "set") + ", mqtt_host=" + (draft_.mqttHost.empty() ? "unset" : draft_.mqttHost) +
           ", mqtt_port=" + std::to_string(draft_.mqttPort) + ", mqtt_user=" + (draft_.mqttUser.empty() ? "unset" : "set") +
           ", mqtt_pass=" + (draft_.mqttPass.empty() ? "unset" : "set") + ", ca=" + (draft_.caPem.empty() ? "unset" : "set");
  }
  if (cmd == "set") {
    size_t s2 = rest.find(' ');
    std::string key = s2 == std::string::npos ? rest : rest.substr(0, s2);
    std::string val = s2 == std::string::npos ? "" : rest.substr(s2 + 1);
    if (key == "wifi_ssid") draft_.wifiSsid = val;
    else if (key == "wifi_pass") draft_.wifiPass = val;
    else if (key == "mqtt_host") draft_.mqttHost = val;
    else if (key == "mqtt_user") draft_.mqttUser = val;
    else if (key == "mqtt_pass") draft_.mqttPass = val;
    else if (key == "ntp_host") draft_.ntpHost = val;
    else if (key == "mqtt_port") {
      char* end = nullptr;
      long p = strtol(val.c_str(), &end, 10);
      if (val.empty() || *end || p < 1 || p > 65535) return "err invalid mqtt_port";
      draft_.mqttPort = (uint16_t)p;
    } else return "err bad_key";
    return "ok";
  }
  if (cmd == "ca") {
    size_t s2 = rest.find(' ');
    std::string sub = s2 == std::string::npos ? rest : rest.substr(0, s2);
    std::string arg = s2 == std::string::npos ? "" : rest.substr(s2 + 1);
    if (sub == "clear") { caBuf_.clear(); inCa_ = true; return "ok"; }
    if (sub == "line") {
      if (!inCa_) return "err ca_not_started";
      if (caBuf_.size() + arg.size() + 1 > 3900) { caBuf_.clear(); inCa_ = false; return "err too_long"; }
      caBuf_ += arg;
      caBuf_ += '\n';
      return "ok";
    }
    if (sub == "end") {
      if (!inCa_) return "err ca_not_started";
      NodeConfig probe = draft_;
      probe.caPem = caBuf_;
      inCa_ = false;
      // Only the CA is judged here; the other fields may still be unset.
      std::string saved = caBuf_;
      caBuf_.clear();
      if (saved.size() < 100 || saved.compare(0, 27, "-----BEGIN CERTIFICATE-----") != 0 ||
          saved.rfind("-----END CERTIFICATE-----") == std::string::npos)
        return "err invalid ca";
      draft_.caPem = saved;
      return "ok";
    }
    return "err bad_ca_command";
  }
  if (cmd == "save") {
    std::string bad = draft_.validate();
    if (!bad.empty()) return "err invalid " + bad;
    if (!store_.save(draft_)) return "err storage_failed";
    reboot = true;
    return "ok saved, rebooting";
  }
  if (cmd == "wipe") {
    store_.wipe();
    draft_ = NodeConfig();
    reboot = true;
    return "ok wiped, rebooting";
  }
  if (cmd == "reboot") { reboot = true; return "ok rebooting"; }
  return "err unknown_command";
}

}  // namespace edge
