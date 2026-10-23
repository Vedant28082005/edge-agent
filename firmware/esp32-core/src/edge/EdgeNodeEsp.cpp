// ESP32 runtime: FreeRTOS tasks, Wi-Fi, NTP, MQTT over TLS, serial configuration shell.
// Everything with logic in it lives in NodeCore / ConfigShell / TimeGate / Backoff (host-tested);
// this file is the glue and can only be exercised on a real board.
#ifdef ARDUINO
#include <Arduino.h>
#include <WiFi.h>
#include <esp_sntp.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <mqtt_client.h>
#include <time.h>
#include "../EdgeNode.h"
#include "NvsConfigStore.h"
#include "BleProvEsp.h"

namespace edge {

namespace {
const char* kPool = "pool.ntp.org";
const uint32_t kWifiTimeoutMs = 20000;
const uint32_t kNtpTimeoutMs = 90000;
const uint32_t kMqttTimeoutMs = 20000;
const size_t kMaxCmdBytes = 1024;
}  // namespace

struct EdgeNode::Lock {
  SemaphoreHandle_t m;
  explicit Lock(SemaphoreHandle_t mu) : m(mu) { xSemaphoreTake(m, portMAX_DELAY); }
  ~Lock() { xSemaphoreGive(m); }
};

uint32_t EdgeNode::nowMs() const { return (uint32_t)(esp_timer_get_time() / 1000); }

void EdgeNode::setPhase(NetPhase p) {
  Lock l(mu_);
  core_.setPhase(p);
}

// ---- begin ------------------------------------------------------------------------------------------

bool EdgeNode::begin() {
  if (started_) return true;
  Serial.begin(115200);
  delay(50);
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  nodeId_ = nodeIdFromMac(mac);
  tAnnounce_ = "home/v1/nodes/" + nodeId_ + "/announce";
  tStatus_ = "home/v1/nodes/" + nodeId_ + "/status";
  tState_ = "home/v1/nodes/" + nodeId_ + "/state";
  tCmd_ = "home/v1/nodes/" + nodeId_ + "/cmd";
  tAck_ = "home/v1/nodes/" + nodeId_ + "/ack";

  if (model_.ledPin >= 0) {
    pinMode(model_.ledPin, OUTPUT);
    digitalWrite(model_.ledPin, model_.ledActiveHigh ? LOW : HIGH);
  }
  if (fwBuildTs() <= 0) { logf("FW_BUILD_TS missing: refusing to start"); return false; }

  mu_ = xSemaphoreCreateMutex();
  cmdQ_ = xQueueCreate(8, sizeof(std::string*));
  ackQ_ = xQueueCreate(16, sizeof(std::string*));

  if (!core_.begin(nowMs())) {
    logf("invalid declaration, node NOT started:");
    for (const std::string& e : model_.errors()) logf("  %s", e.c_str());
    if (model_.ledPin >= 0) digitalWrite(model_.ledPin, model_.ledActiveHigh ? HIGH : LOW);  // solid LED = error
    return false;
  }
  std::string ann = core_.announceJson(nodeId_);
  if (ann.size() > 16384) { logf("announce larger than 16 KiB, node NOT started"); return false; }
  bufSize_ = (int)(ann.size() + 256 > 2048 ? ann.size() + 256 : 2048);

  store_ = new NvsConfigStore();
  auto* st = static_cast<NvsConfigStore*>(store_);
  shell_ = new ConfigShell(*st, nodeId_);
  cfgOk_ = st->load(cfg_) && cfg_.complete() && cfg_.validate().empty();
  if (cfgOk_ && !mqttIdentityOk(nodeId_, cfg_.mqttUser)) {
    logf("stored mqtt_user is not this node's id (%s): refusing to connect", nodeId_.c_str());
    cfgOk_ = false;
  }
  core_.setPhase(cfgOk_ ? NetPhase::Wifi : NetPhase::Unconfigured);

  logf("node %s fw %s, %u endpoint(s), announce %u bytes", nodeId_.c_str(), model_.fw.c_str(),
       (unsigned)model_.endpoints.size(), (unsigned)ann.size());
  if (!cfgOk_) logf("not configured: use the serial shell ('help'), local controls and safety timers are running");

  {
    auto* st2 = static_cast<NvsConfigStore*>(store_);
    auto* prov = new BleProv(mbedProvCrypto(), *st2, nodeId_, loadPop(), model_.hw, model_.fw, provPolicy_);
    prov_ = prov;
    bleEsp_ = new BleProvEsp(*prov, nodeId_);
    if (provPolicy_.requirePop && loadPop().empty())
      logf("no provisioning PoP stored: BLE provisioning stays OFF (serial: 'pop set <label code>')");
    pinMode(provButtonPin_, INPUT_PULLUP);
    prov->boot(nowMs(), cfgOk_);
  }
  xTaskCreatePinnedToCore(bleTaskTramp, "edge-ble", 6144, this, 2, nullptr, 0);
  xTaskCreatePinnedToCore(actuatorTaskTramp, "edge-act", 4096, this, 3, nullptr, 1);
  xTaskCreatePinnedToCore(sensorTaskTramp, "edge-sens", 4096, this, 1, nullptr, 1);
  xTaskCreatePinnedToCore(netTaskTramp, "edge-net", 8192, this, 2, nullptr, 0);
  started_ = true;
  return true;
}

// ---- serial configuration shell ---------------------------------------------------------------------

void EdgeNode::loop() {
  if (shell_) {
    while (Serial.available()) {
      int ch = Serial.read();
      if (ch < 0) break;
      if (ch == '\n') {
        std::string reply;
        bool reboot = false;
        if (lineOverflow_) reply = "err too_long";
        else if (handlePopLine(line_, reply)) {}
        else reply = static_cast<ConfigShell*>(shell_)->handleLine(line_, reboot);
        line_.clear();
        lineOverflow_ = false;
        if (!reply.empty()) Serial.println(reply.c_str());  // replies never contain secrets
        if (reboot) { Serial.flush(); delay(300); ESP.restart(); }
      } else if (ch != '\r') {
        if (line_.size() < 450) line_ += (char)ch; else lineOverflow_ = true;
      }
    }
  }
  vTaskDelay(pdMS_TO_TICKS(10));
}

// "pop set <code>" / "pop status": the per-board BLE proof of possession (the label secret). Never echoed.
bool EdgeNode::handlePopLine(const std::string& line, std::string& reply) {
  if (line.compare(0, 3, "pop") != 0 || (line.size() > 3 && line[3] != ' ')) return false;
  if (line == "pop status") { reply = loadPop().empty() ? "pop: unset" : "pop: set"; return true; }
  if (line.compare(0, 8, "pop set ") == 0) {
    if (!loadPop().empty()) { reply = "err pop_already_set"; return true; }  // set once; a factory reset keeps it
    reply = storePop(line.substr(8)) ? "ok (restart to apply)" : "err bad_pop_format";
    return true;
  }
  reply = "err usage: pop set <code> | pop status";
  return true;
}

// ---- BLE task: button, window, GATT on/off -------------------------------------------------------------

void EdgeNode::bleTaskTramp(void* p) { static_cast<EdgeNode*>(p)->bleTask(); }

void EdgeNode::bleTask() {
  auto* prov = static_cast<BleProv*>(prov_);
  auto* ble = static_cast<BleProvEsp*>(bleEsp_);
  HoldDetector hold(provPolicy_.buttonHoldMs, provPolicy_.factoryHoldMs);
  bool wasAdv = false;
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(50));
    uint32_t now = nowMs();
    HoldDetector::Event ev = hold.feed(digitalRead(provButtonPin_) == LOW, now);
    ble->lock();
    if (ev == HoldDetector::Hold3s) logf("provisioning button: %s", prov->buttonHold(now) ? "window opened" : "refused");
    if (ev == HoldDetector::Hold10s) {
      logf("provisioning button: factory reset");
      bool ok = prov->factoryReset(now);
      ble->unlock();
      if (ok) { Serial.flush(); delay(200); ESP.restart(); }
      continue;
    }
    prov->tick(now);
    bool adv = prov->advertising();
    bool stored = prov->takeStored();
    ble->unlock();
    if (stored) {  // credentials are in NVS: BLE off, reboot, join Wi-Fi (success is not reported over BLE)
      vTaskDelay(pdMS_TO_TICKS(800));  // let the `stored` notification leave
      ble->stop();
      logf("provisioned: restarting");
      Serial.flush();
      delay(200);
      ESP.restart();
    }
    if (adv && !ble->running()) ble->start();
    if (!adv && ble->running()) ble->stop();
    if (adv != wasAdv) {
      wasAdv = adv;
      if (!cfgOk_) setPhase(adv ? NetPhase::Provisioning : NetPhase::Unconfigured);
    }
  }
}

// ---- actuator task: commands, local buttons, safety timers -----------------------------------------

void EdgeNode::actuatorTaskTramp(void* p) { static_cast<EdgeNode*>(p)->actuatorTask(); }

void EdgeNode::actuatorTask() {
  for (;;) {
    std::string* msg = nullptr;
    if (xQueueReceive(cmdQ_, &msg, pdMS_TO_TICKS(20)) == pdTRUE && msg) {
      NodeCore::Outcome o;
      {
        Lock l(mu_);
        o = core_.handleCommand(msg->data(), msg->size(), nowMs());
        if (o.action == NodeCore::Outcome::Identify) core_.startIdentify(nowMs(), o.identifyS);
      }
      delete msg;
      if (o.hasAck) {
        auto* a = new std::string(o.ack);
        if (xQueueSend(ackQ_, &a, 0) != pdTRUE) delete a;
      }
      if (o.action == NodeCore::Outcome::Unprovision) unprovPending_ = true;
    }
    bool led = false;
    {
      Lock l(mu_);
      core_.tick(nowMs(), mqttUp_ && core_.phase() == NetPhase::Run);
      led = core_.ledOn(nowMs());
    }
    if (model_.ledPin >= 0) digitalWrite(model_.ledPin, (led == model_.ledActiveHigh) ? HIGH : LOW);
  }
}

// ---- sensor task --------------------------------------------------------------------------------------

void EdgeNode::sensorTaskTramp(void* p) { static_cast<EdgeNode*>(p)->sensorTask(); }

void EdgeNode::sensorTask() {
  for (;;) {
    size_t n;
    { Lock l(mu_); n = core_.sensorCount(); }
    for (size_t k = 0; k < n; k++) {
      bool due;
      { Lock l(mu_); due = core_.sensorDue(k, nowMs()); }
      if (!due) continue;
      Value v;
      uint32_t t = nowMs();
      bool ok = core_.sensorDriver(k)->read(t, v);  // drivers own their state; read outside the lock (DHT blocks ~25 ms)
      Lock l(mu_);
      if (ok) core_.submitSensor(k, v, t);
      else core_.markSampled(k, t);  // failed read: keep the old value, retry next interval
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

// ---- network task ---------------------------------------------------------------------------------------

void EdgeNode::netTaskTramp(void* p) { static_cast<EdgeNode*>(p)->netTask(); }

void EdgeNode::backoffWait() {
  uint32_t ms = backoff_.nextDelayMs(esp_random());
  logf("retry in %u ms", (unsigned)ms);
  for (uint32_t waited = 0; waited < ms; waited += 100) {
    if (unprovPending_) return;
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}

void EdgeNode::netTask() {
  for (;;) {
    if (unprovPending_) unprovisionNow();
    if (!cfgOk_) {
      vTaskDelay(pdMS_TO_TICKS(500));  // phase (Unconfigured / Provisioning) is owned by begin() and the BLE task
      continue;
    }
    setPhase(NetPhase::Wifi);
    if (!connectWifi()) { backoffWait(); continue; }
    setPhase(NetPhase::Ntp);
    if (!waitForTime()) { backoffWait(); continue; }  // no TLS until the time is plausible
    setPhase(NetPhase::Mqtt);
    if (!mqttConnect() || !mqttSetup()) {
      teardownMqtt(false);
      backoffWait();
      continue;
    }
    setPhase(NetPhase::Run);
    backoff_.onConnected(nowMs());
    logf("RUN (heap free %u, min free %u)", (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMinFreeHeap());
    runConnected();
    backoff_.onDisconnected(nowMs());
    setPhase(NetPhase::Mqtt);
    backoffWait();
  }
}

bool EdgeNode::connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.persistent(false);
  WiFi.setAutoReconnect(false);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(nodeId_.c_str());
  WiFi.disconnect(false);
  WiFi.begin(cfg_.wifiSsid.c_str(), cfg_.wifiPass.empty() ? nullptr : cfg_.wifiPass.c_str());
  uint32_t t0 = nowMs();
  while (WiFi.status() != WL_CONNECTED) {
    if (elapsedMs(nowMs(), t0) > kWifiTimeoutMs) { logf("wifi: timeout"); WiFi.disconnect(false); return false; }
    vTaskDelay(pdMS_TO_TICKS(200));
  }
  logf("wifi up");
  return true;
}

bool EdgeNode::waitForTime() {
  int64_t build = fwBuildTs();
  if (timePlausible((int64_t)time(nullptr), build)) return true;
  // LAN NTP (from the provisioning bundle) first, public pool as fallback.
  const char* s1 = cfg_.ntpHost.empty() ? kPool : cfg_.ntpHost.c_str();
  const char* s2 = cfg_.ntpHost.empty() ? "time.cloudflare.com" : kPool;
  configTime(0, 0, s1, s2);
  sntp_set_sync_interval(15000);  // re-ask every 15 s while we have no plausible time
  uint32_t t0 = nowMs();
  while (!timePlausible((int64_t)time(nullptr), build)) {
    if (WiFi.status() != WL_CONNECTED) return false;
    if (elapsedMs(nowMs(), t0) > kNtpTimeoutMs) { logf("ntp: no plausible time yet"); return false; }
    vTaskDelay(pdMS_TO_TICKS(500));
  }
  {
    // Diagnostic: show the UTC date the TLS certificate check will use (no secrets).
    time_t tnow = time(nullptr);
    struct tm tmv;
    gmtime_r(&tnow, &tmv);
    char stamp[32];
    strftime(stamp, sizeof stamp, "%Y-%m-%d %H:%M:%S", &tmv);
    logf("time ok %s UTC", stamp);
  }
  return true;
}

void EdgeNode::mqttEvent(void* arg, const char*, int32_t id, void* data) {
  static_cast<EdgeNode*>(arg)->onMqttEvent(id, data);
}

void EdgeNode::onMqttEvent(int32_t id, void* data) {
  auto* e = static_cast<esp_mqtt_event_handle_t>(data);
  switch (id) {
    case MQTT_EVENT_CONNECTED:
      mqttUp_ = true;
      break;
    case MQTT_EVENT_DISCONNECTED:
      mqttUp_ = false;
      mqttFailed_ = true;
      break;
    case MQTT_EVENT_ERROR:
      mqttFailed_ = true;  // details (type/tls error codes) only; never the credentials
      logf("mqtt error type %d", e && e->error_handle ? (int)e->error_handle->error_type : -1);
      break;
    case MQTT_EVENT_DATA: {
      if (!e) break;
      if (e->current_data_offset == 0) {
        rx_.clear();
        rxOverflow_ = false;
        // only our own cmd topic is subscribed; anything else is dropped
        if (e->topic_len != (int)tCmd_.size() || memcmp(e->topic, tCmd_.data(), tCmd_.size()) != 0) rxOverflow_ = true;
      }
      if (!rxOverflow_) {
        if (rx_.size() + (size_t)e->data_len > kMaxCmdBytes) rxOverflow_ = true;
        else rx_.append(e->data, (size_t)e->data_len);
      }
      if (e->current_data_offset + e->data_len >= e->total_data_len) {
        if (!rxOverflow_ && !rx_.empty()) {
          auto* m = new std::string(rx_);
          if (xQueueSend(cmdQ_, &m, 0) != pdTRUE) { delete m; logf("cmd queue full, command dropped"); }
        }
        rx_.clear();
      }
      break;
    }
    default:
      break;
  }
}

bool EdgeNode::mqttConnect() {
  esp_mqtt_client_config_t c = {};
  c.host = cfg_.mqttHost.c_str();
  c.port = cfg_.mqttPort;
  c.transport = MQTT_TRANSPORT_OVER_SSL;
  c.cert_pem = cfg_.caPem.c_str();          // verify the broker certificate against the stored CA
  c.skip_cert_common_name_check = false;     // host name must match the certificate; never disabled
  c.username = nodeId_.c_str();
  c.password = cfg_.mqttPass.c_str();
  c.client_id = nodeId_.c_str();             // client_id == username == node_id
  c.disable_clean_session = false;           // clean session: the cmd topic is never retained, nothing to resume
  c.lwt_topic = tStatus_.c_str();
  c.lwt_msg = "offline";
  c.lwt_msg_len = 7;
  c.lwt_qos = 1;
  c.lwt_retain = 1;
  c.keepalive = 30;
  c.disable_auto_reconnect = true;           // reconnects are ours (backoff, time and Wi-Fi checks)
  c.buffer_size = bufSize_;
  c.task_stack = 8192;
  mqttUp_ = false;
  mqttFailed_ = false;
  rx_.clear();
  auto cl = esp_mqtt_client_init(&c);
  if (!cl) { logf("mqtt init failed"); return false; }
  client_ = cl;
  esp_mqtt_client_register_event(cl, MQTT_EVENT_ANY, (esp_event_handler_t)&EdgeNode::mqttEvent, this);
  if (esp_mqtt_client_start(cl) != ESP_OK) { logf("mqtt start failed"); return false; }
  uint32_t t0 = nowMs();
  while (!mqttUp_) {
    if (mqttFailed_ || elapsedMs(nowMs(), t0) > kMqttTimeoutMs) { logf("mqtt connect failed"); return false; }
    vTaskDelay(pdMS_TO_TICKS(100));
  }
  return true;
}

void EdgeNode::teardownMqtt(bool publishOffline) {
  auto cl = static_cast<esp_mqtt_client_handle_t>(client_);
  if (!cl) return;
  if (publishOffline && mqttUp_) {
    esp_mqtt_client_publish(cl, tStatus_.c_str(), "offline", 7, 1, 1);
    vTaskDelay(pdMS_TO_TICKS(400));
  }
  esp_mqtt_client_stop(cl);
  esp_mqtt_client_destroy(cl);
  client_ = nullptr;
  mqttUp_ = false;
}

bool EdgeNode::pub(const std::string& topic, const std::string& payload, int qos, bool retain) {
  auto cl = static_cast<esp_mqtt_client_handle_t>(client_);
  if (!cl || !mqttUp_) return false;
  // Never called while disconnected (mqttUp_), and the client is destroyed on every teardown, so nothing is
  // queued across an outage; the outbox only serves QoS 1 retransmission inside one live session.
  return esp_mqtt_client_enqueue(cl, topic.c_str(), payload.data(), (int)payload.size(), qos, retain ? 1 : 0, true) >= 0;
}

void EdgeNode::publishState() {
  std::string js;
  bool ok;
  {
    Lock l(mu_);
    ok = core_.stateJson((int64_t)time(nullptr), js);
    core_.takeDirty();
  }
  if (ok) pub(tState_, js, 1, true);
}

void EdgeNode::drainAcks(bool publish) {
  std::string* a = nullptr;
  while (xQueueReceive(ackQ_, &a, 0) == pdTRUE) {
    if (a && publish) pub(tAck_, *a, 1, false);
    delete a;  // acks are never kept for later: a stale ack after a reconnect is worse than none
  }
}

bool EdgeNode::mqttSetup() {
  drainAcks(false);  // acks from before this connection are dropped, never replayed
  auto cl = static_cast<esp_mqtt_client_handle_t>(client_);
  size_t n = 0;
  const ConnectStep* seq = connectSequence(n);  // status online, announce, subscribe cmd, state (host-tested order)
  for (size_t k = 0; k < n; k++) {
    switch (seq[k]) {
      case ConnectStep::StatusOnline:
        if (!pub(tStatus_, "online", 1, true)) { logf("status failed"); return false; }
        break;
      case ConnectStep::Announce: {
        std::string ann;
        { Lock l(mu_); ann = core_.announceJson(nodeId_); }
        if (!pub(tAnnounce_, ann, 1, true)) { logf("announce failed"); return false; }
        break;
      }
      case ConnectStep::SubscribeCmd:
        if (esp_mqtt_client_subscribe(cl, tCmd_.c_str(), 1) < 0) { logf("subscribe failed"); return false; }
        break;
      case ConnectStep::State:
        publishState();
        break;
    }
  }
  return true;
}

void EdgeNode::runConnected() {
  uint32_t lastState = nowMs();
  uint32_t lastTimeCheck = nowMs();
  int failures = 0;
  uint32_t hb;
  { Lock l(mu_); hb = core_.effectiveHeartbeatS(); }
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(50));
    uint32_t now = nowMs();
    if (!mqttUp_ || mqttFailed_) { logf("mqtt lost"); break; }
    if (WiFi.status() != WL_CONNECTED) { logf("wifi lost"); break; }
    if (elapsedMs(now, lastTimeCheck) > 10000) {
      lastTimeCheck = now;
      if (!timePlausible((int64_t)time(nullptr), fwBuildTs())) {
        logf("clock no longer plausible: leaving TLS session");
        teardownMqtt(true);
        return;
      }
    }
    bool haveAck = uxQueueMessagesWaiting(ackQ_) > 0;
    bool dirty;
    { Lock l(mu_); dirty = core_.dirty(); }
    bool rateOk = elapsedMs(now, lastState) >= 250;
    if ((dirty && (rateOk || haveAck)) || elapsedMs(now, lastState) >= hb * 1000u) {
      publishState();  // state first, then the ack: the ack tells the hub the state is already out
      lastState = now;
    }
    std::string* a = nullptr;
    while (xQueueReceive(ackQ_, &a, 0) == pdTRUE) {
      if (a && !pub(tAck_, *a, 1, false)) failures++;
      delete a;
    }
    if (failures > 5) { logf("publish failing, reconnecting"); break; }
    if (unprovPending_) { unprovisionNow(); return; }
  }
  teardownMqtt(false);
}

void EdgeNode::unprovisionNow() {
  logf("unprovision: wiping stored configuration");
  if (client_ && mqttUp_) {
    vTaskDelay(pdMS_TO_TICKS(300));  // let the ack leave
    teardownMqtt(true);
  }
  static_cast<NvsConfigStore*>(store_)->wipe();
  Serial.flush();
  delay(200);
  ESP.restart();
}

}  // namespace edge
#endif  // ARDUINO