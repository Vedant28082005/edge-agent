#include "BleProv.h"
#include <ArduinoJson.h>
#include <string.h>
#include "Base64.h"

namespace edge {

namespace {
const size_t kMaxBundle = 4096;
const size_t kMaxBlob = kMaxBundle + 28;
const char* kKdfInfo = "edge-agent-ble-v1";

void wipeMem(void* p, size_t n) {
  volatile uint8_t* v = (volatile uint8_t*)p;
  while (n--) *v++ = 0;
}
void wipeStr(std::string& s) {
  if (!s.empty()) wipeMem(&s[0], s.size());
  s.clear();
}
bool elapsed(uint32_t now, uint32_t deadline) { return (int32_t)(now - deadline) >= 0; }
}  // namespace

BleProv::BleProv(ProvCrypto& crypto, ConfigStore& store, const std::string& nodeId, const std::string& pop,
                 const std::string& hw, const std::string& fw, const ProvPolicy& policy)
    : c_(crypto), store_(store), nodeId_(nodeId), pop_(pop), hw_(hw), fw_(fw), p_(policy) {
  memset(key_, 0, sizeof key_);
}

BleProv::~BleProv() {
  dropSession();
  wipeStr(pop_);
}

void BleProv::open(uint32_t now) {
  dropSession();
  failures_ = 0;
  state_ = ProvState::Open;
  windowEnd_ = now + p_.windowMs;
  code_ = "ok";
}

void BleProv::closeWindow() {
  dropSession();
  state_ = ProvState::Closed;
}

void BleProv::dropSession() {
  wipeMem(key_, sizeof key_);
  haveKey_ = false;
  nextSeq_ = 0;
  wipeStr(blob_);
}

bool BleProv::boot(uint32_t now, bool hasCreds) {
  hasCreds_ = hasCreds;
  if (hasCreds) { state_ = ProvState::Closed; return false; }
  if (p_.requirePop && pop_.empty()) { state_ = ProvState::Closed; return false; }  // never an unauthenticated window
  open(now);
  return true;
}

bool BleProv::buttonHold(uint32_t now) {
  if (state_ == ProvState::Locked) return false;  // the cool-down cannot be skipped
  if (state_ == ProvState::Stored) return false;
  if (p_.requirePop && pop_.empty()) return false;
  open(now);
  return true;
}

bool BleProv::factoryReset(uint32_t now) {
  if (!store_.wipe()) return false;
  hasCreds_ = false;
  if (state_ == ProvState::Locked) return true;  // wiping does not shorten a lock; tick() reopens afterwards
  if (!(p_.requirePop && pop_.empty())) open(now);
  return true;
}

void BleProv::tick(uint32_t now) {
  if (state_ == ProvState::Locked) {
    if (elapsed(now, lockEnd_)) {
      failures_ = 0;
      if (!hasCreds_ && !(p_.requirePop && pop_.empty())) open(now);  // unprovisioned: a fresh window
      else state_ = ProvState::Closed;                                // configured: needs the button again
    }
    return;
  }
  if (state_ == ProvState::Closed || state_ == ProvState::Stored) return;
  if (elapsed(now, windowEnd_)) {  // timeout: BLE off, the node keeps running its local controls
    closeWindow();
    code_ = "window_closed";
    return;
  }
  if (state_ == ProvState::Receiving && elapsed(now, lastChunk_ + p_.idleMs)) {
    dropSession();
    state_ = ProvState::Open;
    code_ = "bad_chunk";
  }
}

void BleProv::fail(const char* code, bool countAttempt, uint32_t now) {
  code_ = code;
  dropSession();
  if (state_ != ProvState::Locked) state_ = ProvState::Open;
  if (countAttempt && ++failures_ >= p_.maxFailures) {
    state_ = ProvState::Locked;
    lockEnd_ = now + p_.lockMs;
    code_ = "window_closed";  // the contract has no `locked` code (gap, see report)
  }
}

std::string BleProv::info(uint32_t now) const {
  JsonDocument d;
  d["proto"] = 1;
  d["node_id"] = nodeId_;
  d["hw"] = hw_;
  d["fw"] = fw_;
  uint32_t left = 0;
  if (advertising() && !elapsed(now, windowEnd_)) left = (windowEnd_ - now) / 1000;
  d["window_left_s"] = left;
  d["pop_required"] = !pop_.empty();
  std::string o;
  serializeJson(d, o);
  return o;
}

std::string BleProv::result() const {
  const char* st = "idle";
  switch (state_) {
    case ProvState::Session: st = "session"; break;
    case ProvState::Receiving: st = "receiving"; break;
    case ProvState::Stored: st = "stored"; break;
    default: st = strcmp(code_, "ok") == 0 ? "idle" : "error"; break;
  }
  JsonDocument d;
  d["state"] = st;
  d["code"] = code_;
  std::string o;
  serializeJson(d, o);
  return o;
}

bool BleProv::writeSession(const uint8_t* d, size_t n, uint32_t now, std::string& indicate) {
  tick(now);
  if (!advertising()) { code_ = "window_closed"; return false; }
  if (n == 0 || n > 256) { fail("bad_session", false, now); return false; }
  JsonDocument doc;
  if (deserializeJson(doc, (const char*)d, n)) { fail("bad_session", false, now); return false; }
  if (doc["proto"].as<int>() != 1 || !doc["client_pub"].is<const char*>() || !doc["pop"].is<const char*>()) {
    fail("bad_session", false, now);
    return false;
  }
  if (strlen(doc["pop"].as<const char*>()) != 0) { fail("bad_session", false, now); return false; }  // PoP never travels over the air
  std::string pub;
  if (!b64decode(doc["client_pub"].as<const char*>(), pub) || pub.size() != 32) { fail("bad_session", false, now); return false; }
  if (p_.requirePop && pop_.empty()) { fail("bad_session", false, now); return false; }

  dropSession();
  uint8_t priv[32], mypub[32], shared[32], salt[16];
  std::string info = kKdfInfo + pop_;
  bool ok = c_.x25519Keypair(priv, mypub) && c_.x25519(priv, (const uint8_t*)pub.data(), shared);
  if (ok) {
    c_.random(salt, sizeof salt);
    ok = c_.hkdfSha256(shared, 32, salt, sizeof salt, (const uint8_t*)info.data(), info.size(), key_, 32);
  }
  wipeMem(priv, sizeof priv);
  wipeMem(shared, sizeof shared);
  wipeStr(info);
  if (!ok) { fail("bad_session", false, now); return false; }
  haveKey_ = true;
  state_ = ProvState::Session;
  code_ = "ok";
  JsonDocument r;
  r["board_pub"] = b64encode(mypub, 32);
  r["salt"] = b64encode(salt, sizeof salt);
  serializeJson(r, indicate);
  return true;
}

bool BleProv::writeData(const uint8_t* d, size_t n, uint32_t now, uint8_t notify[3]) {
  tick(now);
  notify[0] = notify[1] = 0;
  notify[2] = 1;
  if (!advertising()) { code_ = "window_closed"; return false; }
  if (!haveKey_) { code_ = "bad_session"; return false; }
  if (n < 4) { fail("bad_chunk", false, now); return false; }
  uint16_t seq = (uint16_t)(d[0] << 8 | d[1]);
  uint8_t flags = d[2], len = d[3];
  notify[0] = d[0];
  notify[1] = d[1];
  if (len != n - 4) { fail("bad_chunk", false, now); return false; }
  bool first = flags & 1, last = flags & 2;
  if (first) {
    if (seq != 0) { code_ = "bad_chunk"; return false; }
    wipeStr(blob_);
    nextSeq_ = 0;
    state_ = ProvState::Receiving;
  } else if (state_ != ProvState::Receiving) {
    code_ = "bad_chunk";
    return false;
  }
  if (seq != nextSeq_) { code_ = "bad_chunk"; return false; }  // out of order: status 1, the board keeps its position
  if (blob_.size() + len > kMaxBlob) {
    notify[2] = 2;
    fail("bad_chunk", false, now);
    return false;
  }
  blob_.append((const char*)d + 4, len);
  nextSeq_++;
  lastChunk_ = now;
  notify[2] = 0;
  code_ = "ok";
  if (last) return finish(now);
  return true;
}

bool BleProv::finish(uint32_t now) {
  std::string blob;
  blob.swap(blob_);
  if (blob.size() < 12 + 16 + 2) { wipeStr(blob); fail("bad_bundle", true, now); return false; }
  const uint8_t* b = (const uint8_t*)blob.data();
  size_t ctLen = blob.size() - 28;
  std::string plain(ctLen, '\0');
  bool ok = c_.aesGcmDecrypt(key_, b, (const uint8_t*)nodeId_.data(), nodeId_.size(), b + 12, ctLen, b + 12 + ctLen,
                             (uint8_t*)&plain[0]);
  wipeStr(blob);
  if (!ok) { wipeStr(plain); fail("bad_bundle", true, now); return false; }  // wrong PoP, tampering or wrong key
  NodeConfig cfg;
  std::string why;
  bool parsed = plain.size() <= kMaxBundle && parseBundle(plain, nodeId_, cfg, why);
  wipeStr(plain);
  if (!parsed) { fail("bad_bundle", false, now); return false; }  // authenticated sender, bad content: not a guess
  if (!store_.save(cfg)) { fail("storage_failed", false, now); return false; }
  hasCreds_ = true;
  dropSession();
  state_ = ProvState::Stored;
  code_ = "ok";
  storedPending_ = true;
  return true;
}

void BleProv::clientDisconnected(uint32_t) {
  dropSession();
  if (state_ == ProvState::Session || state_ == ProvState::Receiving) state_ = ProvState::Open;
}

bool parseBundle(const std::string& json, const std::string& nodeId, NodeConfig& out, std::string& why) {
  JsonDocument d;
  if (deserializeJson(d, json)) { why = "json"; return false; }
  if (d["v"].as<int>() != 1) { why = "v"; return false; }
  if (!d["wifi"]["ssid"].is<const char*>() || !d["wifi"]["password"].is<const char*>()) { why = "wifi"; return false; }
  if (!d["mqtt"]["host"].is<const char*>() || !d["mqtt"]["username"].is<const char*>() || !d["mqtt"]["password"].is<const char*>()) {
    why = "mqtt";
    return false;
  }
  if (!d["ca_pem"].is<const char*>()) { why = "ca"; return false; }
  NodeConfig c;
  c.wifiSsid = d["wifi"]["ssid"].as<const char*>();
  c.wifiPass = d["wifi"]["password"].as<const char*>();
  c.mqttHost = d["mqtt"]["host"].as<const char*>();
  int port = d["mqtt"]["port"] | 8883;
  if (port < 1 || port > 65535) { why = "mqtt_port"; return false; }
  c.mqttPort = (uint16_t)port;
  c.mqttUser = d["mqtt"]["username"].as<const char*>();
  c.mqttPass = d["mqtt"]["password"].as<const char*>();
  if (d["ntp_host"].is<const char*>()) c.ntpHost = d["ntp_host"].as<const char*>();
  c.caPem = d["ca_pem"].as<const char*>();
  if (c.mqttUser != nodeId) { why = "mqtt_user"; return false; }  // client_id == username == node_id
  if (c.caPem.find("PRIVATE KEY") != std::string::npos) { why = "ca"; return false; }
  why = c.validate();
  if (!why.empty()) return false;
  if (!c.complete()) { why = "incomplete"; return false; }
  out = c;
  return true;
}

}  // namespace edge
