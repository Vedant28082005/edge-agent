// See BleProvEsp.h. Hardware-only code: reviewed against NimBLE-Arduino 1.4.x and ESP-IDF 4.4 mbedtls 2.28 APIs,
// but only built by `pio run` (no host test can exercise it). Nothing in here logs a key, a PoP or a bundle byte.
#ifdef ARDUINO
#include "BleProvEsp.h"
#include <Arduino.h>
#include <NimBLEDevice.h>
#include <Preferences.h>
#include <esp_system.h>
#include <mbedtls/ecdh.h>
#include <mbedtls/ecp.h>
#include <mbedtls/gcm.h>
#include <mbedtls/md.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <string.h>
#include "Hal.h"

namespace edge {

namespace {
const char* kSvc = "4dd57997-6f7e-4f0a-b1d5-e01ef6674efe";
const char* kInfo = "9809777c-4f37-44e6-b054-b3fb7ee9f6db";
const char* kSession = "8583a1b2-4f51-48cc-8270-35f88e7b7260";
const char* kData = "c5a63a5d-8766-4e3b-b2b2-eab7c732361b";
const char* kResult = "43f00218-77e0-4e21-9eb2-56da36658cee";

int rngCb(void*, unsigned char* out, size_t n) {
  esp_fill_random(out, n);  // true random while the BT controller is running
  return 0;
}

void reverse32(const uint8_t* in, uint8_t* out) {
  for (int i = 0; i < 32; i++) out[i] = in[31 - i];
}

uint32_t nowMs() { return (uint32_t)millis(); }

// ---- mbedtls primitives -------------------------------------------------------------------------------
// Curve25519 in mbedtls is big-endian on the mpi API while X25519 byte strings are little-endian: reverse.
class MbedCrypto : public ProvCrypto {
 public:
  void random(uint8_t* out, size_t n) override { esp_fill_random(out, n); }

  bool x25519Keypair(uint8_t priv[32], uint8_t pub[32]) override {
    mbedtls_ecp_group g;
    mbedtls_mpi d;
    mbedtls_ecp_point q;
    mbedtls_ecp_group_init(&g);
    mbedtls_mpi_init(&d);
    mbedtls_ecp_point_init(&q);
    uint8_t be[32];
    bool ok = mbedtls_ecp_group_load(&g, MBEDTLS_ECP_DP_CURVE25519) == 0 &&
              mbedtls_ecdh_gen_public(&g, &d, &q, rngCb, nullptr) == 0 &&
              mbedtls_mpi_write_binary(&d, be, 32) == 0;
    if (ok) {
      reverse32(be, priv);
      ok = mbedtls_mpi_write_binary(&q.X, be, 32) == 0;
      if (ok) reverse32(be, pub);
    }
    memset(be, 0, sizeof be);
    mbedtls_mpi_free(&d);
    mbedtls_ecp_point_free(&q);
    mbedtls_ecp_group_free(&g);
    return ok;
  }

  bool x25519(const uint8_t priv[32], const uint8_t peer[32], uint8_t out[32]) override {
    mbedtls_ecp_group g;
    mbedtls_mpi d, z;
    mbedtls_ecp_point q;
    mbedtls_ecp_group_init(&g);
    mbedtls_mpi_init(&d);
    mbedtls_mpi_init(&z);
    mbedtls_ecp_point_init(&q);
    uint8_t be[32];
    reverse32(priv, be);
    bool ok = mbedtls_ecp_group_load(&g, MBEDTLS_ECP_DP_CURVE25519) == 0 && mbedtls_mpi_read_binary(&d, be, 32) == 0;
    if (ok) {
      uint8_t pbe[32];
      reverse32(peer, pbe);
      pbe[0] &= 0x7F;  // RFC 7748: mask the top bit of the u-coordinate
      ok = mbedtls_mpi_read_binary(&q.X, pbe, 32) == 0 && mbedtls_mpi_lset(&q.Z, 1) == 0;
    }
    ok = ok && mbedtls_ecdh_compute_shared(&g, &z, &q, &d, rngCb, nullptr) == 0 && mbedtls_mpi_write_binary(&z, be, 32) == 0;
    if (ok) {
      reverse32(be, out);
      uint8_t acc = 0;
      for (int i = 0; i < 32; i++) acc |= out[i];
      ok = acc != 0;  // RFC 7748: reject an all-zero shared secret (low-order peer point)
    }
    memset(be, 0, sizeof be);
    mbedtls_mpi_free(&d);
    mbedtls_mpi_free(&z);
    mbedtls_ecp_point_free(&q);
    mbedtls_ecp_group_free(&g);
    return ok;
  }

  bool hkdfSha256(const uint8_t* ikm, size_t il, const uint8_t* salt, size_t sl, const uint8_t* info, size_t nl, uint8_t* out,
                  size_t ol) override {
    // RFC 5869 by hand: the precompiled Arduino mbedtls does not build mbedtls_hkdf().
    const mbedtls_md_info_t* md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!md || ol > 255 * 32) return false;
    uint8_t zeros[32] = {0}, prk[32];
    if (mbedtls_md_hmac(md, sl ? salt : zeros, sl ? sl : 32, ikm, il, prk) != 0) return false;
    uint8_t t[32];
    size_t tl = 0, done = 0;
    for (uint8_t i = 1; done < ol; i++) {
      mbedtls_md_context_t c;
      mbedtls_md_init(&c);
      bool ok = mbedtls_md_setup(&c, md, 1) == 0 && mbedtls_md_hmac_starts(&c, prk, 32) == 0 &&
                (tl == 0 || mbedtls_md_hmac_update(&c, t, tl) == 0) && mbedtls_md_hmac_update(&c, info, nl) == 0 &&
                mbedtls_md_hmac_update(&c, &i, 1) == 0 && mbedtls_md_hmac_finish(&c, t) == 0;
      mbedtls_md_free(&c);
      if (!ok) { memset(prk, 0, sizeof prk); return false; }
      tl = 32;
      size_t n = ol - done < 32 ? ol - done : 32;
      memcpy(out + done, t, n);
      done += n;
    }
    memset(prk, 0, sizeof prk);
    memset(t, 0, sizeof t);
    return true;
  }

  bool aesGcmDecrypt(const uint8_t key[32], const uint8_t nonce[12], const uint8_t* aad, size_t al, const uint8_t* ct, size_t cl,
                     const uint8_t tag[16], uint8_t* out) override {
    mbedtls_gcm_context c;
    mbedtls_gcm_init(&c);
    bool ok = mbedtls_gcm_setkey(&c, MBEDTLS_CIPHER_ID_AES, key, 256) == 0 &&
              mbedtls_gcm_auth_decrypt(&c, cl, nonce, 12, aad, al, tag, 16, ct, out) == 0;
    mbedtls_gcm_free(&c);
    return ok;  // false = tag mismatch (wrong PoP or tampering); out is zeroed by mbedtls on failure
  }
};
MbedCrypto gCrypto;
}  // namespace

ProvCrypto& mbedProvCrypto() { return gCrypto; }

// ---- PoP storage --------------------------------------------------------------------------------------

bool validPopFormat(const std::string& pop) {
  if (pop.size() < 16 || pop.size() > 64) return false;
  for (char c : pop) {
    bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '2' && c <= '7');
    if (!ok) return false;
  }
  return true;
}

std::string loadPop() {
  Preferences p;
  std::string out;
  if (p.begin("edgeid", true)) {
    out = p.getString("pop", "").c_str();
    p.end();
  }
  return validPopFormat(out) ? out : std::string();
}

bool storePop(const std::string& pop) {
  if (!validPopFormat(pop)) return false;
  Preferences p;
  if (!p.begin("edgeid", false)) return false;
  bool ok = p.putString("pop", pop.c_str()) > 0;
  p.end();
  return ok;
}

// ---- GATT ---------------------------------------------------------------------------------------------

struct ProvCallbacks : public NimBLECharacteristicCallbacks, public NimBLEServerCallbacks {
  BleProvEsp* o;
  explicit ProvCallbacks(BleProvEsp* owner) : o(owner) {}

  void onRead(NimBLECharacteristic* ch) override {
    std::string uuid = ch->getUUID().toString();
    o->lock();
    if (uuid == kInfo) ch->setValue(o->prov().info(nowMs()));
    else if (uuid == kResult) ch->setValue(o->prov().result());
    o->unlock();
  }

  void onWrite(NimBLECharacteristic* ch) override {
    std::string uuid = ch->getUUID().toString();
    std::string v = ch->getValue();
    o->lock();
    if (uuid == kSession) {
      std::string reply;
      if (o->prov().writeSession((const uint8_t*)v.data(), v.size(), nowMs(), reply)) {
        ch->setValue(reply);
        ch->notify(false);  // indicate: the board's public key and salt (nothing secret)
      }
    } else if (uuid == kData) {
      uint8_t ack[3];
      o->prov().writeData((const uint8_t*)v.data(), v.size(), nowMs(), ack);
      ch->setValue(ack, 3);
      ch->notify();
    }
    std::string res = o->prov().result();
    o->unlock();
    static_cast<NimBLECharacteristic*>(o->chResult_)->setValue(res);
    static_cast<NimBLECharacteristic*>(o->chResult_)->notify();
    // wipe the copy of the written bytes (chunks of the encrypted bundle, or the handshake)
    if (!v.empty()) memset(&v[0], 0, v.size());
  }

  void onDisconnect(NimBLEServer*) override {
    o->lock();
    o->prov().clientDisconnected(nowMs());
    o->unlock();
    if (o->prov().advertising()) NimBLEDevice::startAdvertising();
  }
};

static ProvCallbacks* gCb = nullptr;

BleProvEsp::BleProvEsp(BleProv& prov, const std::string& nodeId) : prov_(prov), nodeId_(nodeId) {
  mu_ = xSemaphoreCreateRecursiveMutex();
}

void BleProvEsp::lock() { xSemaphoreTakeRecursive((SemaphoreHandle_t)mu_, portMAX_DELAY); }
void BleProvEsp::unlock() { xSemaphoreGiveRecursive((SemaphoreHandle_t)mu_); }

void BleProvEsp::pushResult() {
  if (!running_ || !chResult_) return;
  lock();
  std::string r = prov_.result();
  unlock();
  static_cast<NimBLECharacteristic*>(chResult_)->setValue(r);
  static_cast<NimBLECharacteristic*>(chResult_)->notify();
}

void BleProvEsp::start() {
  if (running_) return;
  // Name "Setup-XXXX" = last 4 hex of the node id; advertising data = name + service UUID only.
  std::string name = "Setup-" + nodeId_.substr(nodeId_.size() - 4);
  NimBLEDevice::init(name);
  NimBLEDevice::setPower(ESP_PWR_LVL_P3);
  NimBLEDevice::setSecurityAuth(false, false, false);  // app-level encryption (X25519 + PoP), no BLE pairing
  NimBLEServer* srv = NimBLEDevice::createServer();
  if (!gCb) gCb = new ProvCallbacks(this);
  gCb->o = this;
  srv->setCallbacks(gCb);
  NimBLEService* svc = srv->createService(kSvc);
  auto* info = svc->createCharacteristic(kInfo, NIMBLE_PROPERTY::READ);
  auto* sess = svc->createCharacteristic(kSession, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::INDICATE);
  auto* data = svc->createCharacteristic(kData, NIMBLE_PROPERTY::WRITE_NR | NIMBLE_PROPERTY::NOTIFY);
  auto* res = svc->createCharacteristic(kResult, NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY);
  info->setCallbacks(gCb);
  sess->setCallbacks(gCb);
  data->setCallbacks(gCb);
  res->setCallbacks(gCb);
  chInfo_ = info;
  chSession_ = sess;
  chData_ = data;
  chResult_ = res;
  svc->start();
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(kSvc);
  adv->setScanResponse(true);
  adv->start();
  running_ = true;
  logf("ble: provisioning window open (advertising as %s)", name.c_str());
}

void BleProvEsp::stop() {
  if (!running_) return;
  NimBLEDevice::getAdvertising()->stop();
  NimBLEDevice::deinit(true);
  running_ = false;
  chInfo_ = chSession_ = chData_ = chResult_ = nullptr;
  logf("ble: off");
}

}  // namespace edge
#endif  // ARDUINO
