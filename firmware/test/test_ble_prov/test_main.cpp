// BLE provisioning state machine (S2b): no credentials -> window, PoP failures -> lockout, success -> BLE off,
// timeout, button re-enable, factory reset. Uses a FAKE crypto (deterministic, NOT secure): these tests cover the
// protocol and state rules; the real mbedtls primitives are only built on the board (see firmware/README.md).
#include "../common.h"
#include <edge/Base64.h>
#include <edge/BleProv.h>

namespace {

// Fake primitives: shared = boardPub ^ clientPub, key = fold(shared, salt, info), "AES-GCM" = xor stream + 16-byte tag.
struct FakeCrypto : public ProvCrypto {
  uint8_t ctr = 1;
  void random(uint8_t* o, size_t n) override { for (size_t i = 0; i < n; i++) o[i] = (uint8_t)(ctr++ * 7 + i); }
  bool x25519Keypair(uint8_t priv[32], uint8_t pub[32]) override {
    random(priv, 32);
    for (int i = 0; i < 32; i++) pub[i] = priv[i] ^ 0x5A;
    return true;
  }
  bool x25519(const uint8_t priv[32], const uint8_t peer[32], uint8_t out[32]) override {
    for (int i = 0; i < 32; i++) out[i] = (priv[i] ^ 0x5A) ^ peer[i];
    return true;
  }
  static void fold(const uint8_t* a, size_t an, const uint8_t* b, size_t bn, const uint8_t* c, size_t cn, uint8_t* out, size_t on) {
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < on; i++) {
      for (size_t k = 0; k < an; k++) h = (h ^ a[k]) * 16777619u;
      for (size_t k = 0; k < bn; k++) h = (h ^ b[k]) * 16777619u;
      for (size_t k = 0; k < cn; k++) h = (h ^ c[k]) * 16777619u;
      h = (h ^ (uint32_t)(i + 1)) * 16777619u;
      out[i] = (uint8_t)(h >> 11);
    }
  }
  bool hkdfSha256(const uint8_t* ikm, size_t il, const uint8_t* salt, size_t sl, const uint8_t* info, size_t nl, uint8_t* out, size_t ol) override {
    fold(ikm, il, salt, sl, info, nl, out, ol);
    return true;
  }
  static void tagOf(const uint8_t key[32], const uint8_t nonce[12], const uint8_t* aad, size_t al, const uint8_t* ct, size_t cl, uint8_t tag[16]) {
    uint8_t t0[32];
    fold(key, 32, nonce, 12, aad, al, t0, 32);
    fold(t0, 32, ct, cl, nonce, 12, tag, 16);
  }
  static void stream(const uint8_t key[32], const uint8_t nonce[12], uint8_t* buf, size_t n) {
    for (size_t i = 0; i < n; i++) {
      uint8_t k[1];
      uint8_t idx[2] = {(uint8_t)(i & 255), (uint8_t)(i >> 8)};
      fold(key, 32, nonce, 12, idx, 2, k, 1);
      buf[i] ^= k[0];
    }
  }
  bool aesGcmDecrypt(const uint8_t key[32], const uint8_t nonce[12], const uint8_t* aad, size_t al, const uint8_t* ct, size_t cl, const uint8_t tag[16], uint8_t* out) override {
    uint8_t want[16];
    tagOf(key, nonce, aad, al, ct, cl, want);
    if (memcmp(want, tag, 16) != 0) return false;
    memcpy(out, ct, cl);
    stream(key, nonce, out, cl);
    return true;
  }
};

const char* kNode = "esp32-a1b2c3d4e5f6";
const char* kPop = "K7QW2M9XH4TD8R3VBNP6ZA5CJE";  // 26 chars = 130 bits

std::string caPem() {
  std::string s = "-----BEGIN CERTIFICATE-----\n";
  for (int i = 0; i < 4; i++) s += "MIIBszCCAVmgAwIBAgIUQ2xyZmFrZWNhZm9ydGVzdHNvbmx5MAoGCCqGSM49BAMC\n";
  return s + "-----END CERTIFICATE-----\n";
}

std::string bundleJson(const char* user = kNode) {
  JsonDocument d;
  d["v"] = 1;
  d["wifi"]["ssid"] = "HomeNet";
  d["wifi"]["password"] = "wifi-secret-pw";
  d["mqtt"]["host"] = "192.168.1.10";
  d["mqtt"]["port"] = 8883;
  d["mqtt"]["username"] = user;
  d["mqtt"]["password"] = "mqtt-secret-pw";
  d["ntp_host"] = "192.168.1.10";
  d["ca_pem"] = caPem();
  std::string o;
  serializeJson(d, o);
  return o;
}

struct Rig {
  FakeCrypto crypto;
  MemConfigStore store;
  ProvPolicy pol;
  BleProv* prov = nullptr;
  uint8_t cpriv[32], cpub[32];
  uint8_t key[32];
  uint32_t t = 1000;
  explicit Rig(const std::string& pop = kPop) { make(pop); }
  void make(const std::string& pop) {
    delete prov;
    prov = new BleProv(crypto, store, kNode, pop, "esp32-devkit", "0.1.0", pol);
    for (int i = 0; i < 32; i++) { cpriv[i] = (uint8_t)(i * 3 + 1); cpub[i] = cpriv[i] ^ 0x5A; }
  }
  ~Rig() { delete prov; }

  // Client side of the handshake with the PoP the client believes in.
  bool handshake(const std::string& clientPop, const char* popField = "") {
    std::string pub = b64encode(cpub, 32);
    std::string js = std::string("{\"proto\":1,\"client_pub\":\"") + pub + "\",\"pop\":\"" + popField + "\"}";
    std::string ind;
    if (!prov->writeSession((const uint8_t*)js.data(), js.size(), t, ind)) return false;
    JsonDocument d;
    TEST_ASSERT_FALSE(deserializeJson(d, ind));
    std::string bpub, salt;
    TEST_ASSERT_TRUE(b64decode(d["board_pub"].as<const char*>(), bpub));
    TEST_ASSERT_TRUE(b64decode(d["salt"].as<const char*>(), salt));
    uint8_t shared[32];
    for (int i = 0; i < 32; i++) shared[i] = cpub[i] ^ (uint8_t)bpub[i];
    std::string info = std::string("edge-agent-ble-v1") + clientPop;
    FakeCrypto::fold(shared, 32, (const uint8_t*)salt.data(), 16, (const uint8_t*)info.data(), info.size(), key, 32);
    return true;
  }
  // Sends the encrypted bundle in chunks of `chunk` bytes. Returns result of the last write.
  bool send(const std::string& plain, size_t chunk = 100) {
    uint8_t nonce[12];
    for (int i = 0; i < 12; i++) nonce[i] = (uint8_t)(0xA0 + i);
    std::string ct = plain;
    FakeCrypto::stream(key, nonce, (uint8_t*)&ct[0], ct.size());
    uint8_t tag[16];
    FakeCrypto::tagOf(key, nonce, (const uint8_t*)kNode, strlen(kNode), (const uint8_t*)ct.data(), ct.size(), tag);
    std::string blob((const char*)nonce, 12);
    blob += ct;
    blob.append((const char*)tag, 16);
    bool ok = true;
    uint16_t seq = 0;
    for (size_t off = 0; off < blob.size(); off += chunk, seq++) {
      size_t n = blob.size() - off < chunk ? blob.size() - off : chunk;
      std::string w;
      w += (char)(seq >> 8);
      w += (char)(seq & 255);
      w += (char)((off == 0 ? 1 : 0) | (off + n >= blob.size() ? 2 : 0));
      w += (char)n;
      w += blob.substr(off, n);
      uint8_t ack[3];
      ok = prov->writeData((const uint8_t*)w.data(), w.size(), t, ack);
      TEST_ASSERT_EQUAL_UINT8_MESSAGE(ok ? 0 : ack[2], ack[2], "chunk ack status");
    }
    return ok;
  }
  // One whole attempt with the given client-side PoP.
  bool attempt(const std::string& clientPop) {
    if (!handshake(clientPop)) return false;
    return send(bundleJson());
  }
};

}  // namespace

void setUp() {}
void tearDown() {}

void test_no_credentials_opens_the_window_and_advertises() {
  Rig r;
  TEST_ASSERT_TRUE(r.prov->boot(r.t, false));
  TEST_ASSERT_TRUE(r.prov->advertising());
  JsonDocument d = parse(r.prov->info(r.t));
  TEST_ASSERT_EQUAL_STRING(kNode, d["node_id"].as<const char*>());
  TEST_ASSERT_TRUE(d["pop_required"].as<bool>());
  TEST_ASSERT_EQUAL_INT(300, d["window_left_s"].as<int>());
  TEST_ASSERT_TRUE(r.prov->info(r.t).find(kPop) == std::string::npos);  // info never carries the PoP
}

void test_with_credentials_ble_stays_off_until_the_button() {
  Rig r;
  TEST_ASSERT_FALSE(r.prov->boot(r.t, true));
  TEST_ASSERT_FALSE(r.prov->advertising());
  std::string ind;
  const char* js = "{\"proto\":1,\"client_pub\":\"AAAA\",\"pop\":\"\"}";
  TEST_ASSERT_FALSE(r.prov->writeSession((const uint8_t*)js, strlen(js), r.t, ind));
  TEST_ASSERT_EQUAL_STRING("window_closed", r.prov->code());
  TEST_ASSERT_TRUE(r.prov->buttonHold(r.t));
  TEST_ASSERT_TRUE(r.prov->advertising());
}

void test_button_hold_detector_fires_once_at_3s_and_once_at_10s() {
  HoldDetector h;
  TEST_ASSERT_EQUAL(HoldDetector::None, h.feed(true, 0));
  TEST_ASSERT_EQUAL(HoldDetector::None, h.feed(true, 2999));
  TEST_ASSERT_EQUAL(HoldDetector::Hold3s, h.feed(true, 3000));
  TEST_ASSERT_EQUAL(HoldDetector::None, h.feed(true, 3500));
  TEST_ASSERT_EQUAL(HoldDetector::Hold10s, h.feed(true, 10000));
  TEST_ASSERT_EQUAL(HoldDetector::None, h.feed(true, 12000));
  h.feed(false, 12100);
  TEST_ASSERT_EQUAL(HoldDetector::None, h.feed(true, 12200));  // a new press starts again
}

void test_correct_pop_stores_config_and_turns_ble_off() {
  Rig r;
  r.prov->boot(r.t, false);
  TEST_ASSERT_TRUE(r.attempt(kPop));
  TEST_ASSERT_TRUE(r.prov->takeStored());
  TEST_ASSERT_FALSE(r.prov->takeStored());  // once
  TEST_ASSERT_EQUAL(ProvState::Stored, r.prov->state());
  TEST_ASSERT_FALSE(r.prov->advertising());
  TEST_ASSERT_TRUE(r.store.has);
  TEST_ASSERT_EQUAL_STRING(kNode, r.store.cfg.mqttUser.c_str());
  TEST_ASSERT_EQUAL_STRING("HomeNet", r.store.cfg.wifiSsid.c_str());
  TEST_ASSERT_TRUE(r.store.cfg.complete());
  JsonDocument d = parse(r.prov->result());
  TEST_ASSERT_EQUAL_STRING("stored", d["state"].as<const char*>());
  // result and info never echo credentials
  TEST_ASSERT_TRUE(r.prov->result().find("secret") == std::string::npos);
  // no more writes are accepted
  std::string ind;
  const char* js = "{\"proto\":1,\"client_pub\":\"AAAA\",\"pop\":\"\"}";
  TEST_ASSERT_FALSE(r.prov->writeSession((const uint8_t*)js, strlen(js), r.t, ind));
}

void test_wrong_pop_is_rejected_and_stores_nothing() {
  Rig r;
  r.prov->boot(r.t, false);
  TEST_ASSERT_FALSE(r.attempt("WRONGWRONGWRONGWRONGWRONG"));
  TEST_ASSERT_EQUAL_STRING("bad_bundle", r.prov->code());
  TEST_ASSERT_FALSE(r.store.has);
  TEST_ASSERT_EQUAL_INT(1, r.prov->failures());
  TEST_ASSERT_TRUE(r.prov->advertising());  // still open for the real admin
}

void test_n_wrong_attempts_lock_ble_and_the_cooldown_ends() {
  Rig r;
  r.prov->boot(r.t, false);
  for (int i = 0; i < r.pol.maxFailures; i++) {
    TEST_ASSERT_FALSE(r.attempt("WRONGWRONGWRONGWRONGWRONG"));
    r.t += 1000;
  }
  TEST_ASSERT_EQUAL(ProvState::Locked, r.prov->state());
  TEST_ASSERT_FALSE(r.prov->advertising());
  // even the correct PoP is refused while locked, and the button cannot skip the cool-down
  TEST_ASSERT_FALSE(r.attempt(kPop));
  TEST_ASSERT_FALSE(r.prov->buttonHold(r.t));
  TEST_ASSERT_FALSE(r.store.has);
  r.t += r.pol.lockMs - 5000;
  r.prov->tick(r.t);
  TEST_ASSERT_EQUAL(ProvState::Locked, r.prov->state());
  r.t += 6000;
  r.prov->tick(r.t);
  TEST_ASSERT_EQUAL(ProvState::Open, r.prov->state());  // unprovisioned board: fresh window
  TEST_ASSERT_EQUAL_INT(0, r.prov->failures());
  TEST_ASSERT_TRUE(r.attempt(kPop));
  TEST_ASSERT_TRUE(r.store.has);
}

void test_lockout_on_a_configured_node_needs_the_button_again() {
  Rig r;
  r.prov->boot(r.t, true);
  r.prov->buttonHold(r.t);
  for (int i = 0; i < r.pol.maxFailures; i++) r.attempt("WRONGWRONGWRONGWRONGWRONG");
  TEST_ASSERT_EQUAL(ProvState::Locked, r.prov->state());
  r.t += r.pol.lockMs + 1;
  r.prov->tick(r.t);
  TEST_ASSERT_EQUAL(ProvState::Closed, r.prov->state());
  TEST_ASSERT_TRUE(r.prov->buttonHold(r.t));
}

void test_a_success_between_failures_is_not_needed_but_good_attempt_after_some_failures_works() {
  Rig r;
  r.prov->boot(r.t, false);
  for (int i = 0; i < r.pol.maxFailures - 1; i++) r.attempt("WRONGWRONGWRONGWRONGWRONG");
  TEST_ASSERT_TRUE(r.attempt(kPop));
  TEST_ASSERT_TRUE(r.store.has);
}

void test_window_times_out_after_5_minutes_and_the_button_reopens_it() {
  Rig r;
  r.prov->boot(r.t, false);
  r.t += 299000;
  r.prov->tick(r.t);
  TEST_ASSERT_TRUE(r.prov->advertising());
  r.t += 2000;
  r.prov->tick(r.t);
  TEST_ASSERT_FALSE(r.prov->advertising());
  TEST_ASSERT_EQUAL_STRING("window_closed", r.prov->code());
  TEST_ASSERT_FALSE(r.attempt(kPop));
  TEST_ASSERT_FALSE(r.store.has);
  TEST_ASSERT_TRUE(r.prov->buttonHold(r.t));
  TEST_ASSERT_TRUE(r.attempt(kPop));
  TEST_ASSERT_TRUE(r.store.has);
}

void test_window_expiring_mid_transfer_drops_the_partial_data() {
  Rig r;
  r.prov->boot(r.t, false);
  TEST_ASSERT_TRUE(r.handshake(kPop));
  uint8_t w[8] = {0, 0, 1, 4, 1, 2, 3, 4};
  uint8_t ack[3];
  TEST_ASSERT_TRUE(r.prov->writeData(w, 8, r.t, ack));
  r.t += 301000;
  uint8_t w2[8] = {0, 1, 2, 4, 1, 2, 3, 4};
  TEST_ASSERT_FALSE(r.prov->writeData(w2, 8, r.t, ack));
  TEST_ASSERT_FALSE(r.store.has);
}

void test_idle_transfer_is_aborted_after_30s() {
  Rig r;
  r.prov->boot(r.t, false);
  TEST_ASSERT_TRUE(r.handshake(kPop));
  uint8_t w[8] = {0, 0, 1, 4, 1, 2, 3, 4};
  uint8_t ack[3];
  TEST_ASSERT_TRUE(r.prov->writeData(w, 8, r.t, ack));
  TEST_ASSERT_EQUAL(ProvState::Receiving, r.prov->state());
  r.t += 31000;
  r.prov->tick(r.t);
  TEST_ASSERT_EQUAL(ProvState::Open, r.prov->state());
  TEST_ASSERT_EQUAL_STRING("bad_chunk", r.prov->code());
}

void test_chunk_ordering_and_size_are_enforced() {
  Rig r;
  r.prov->boot(r.t, false);
  TEST_ASSERT_TRUE(r.handshake(kPop));
  uint8_t ack[3];
  uint8_t skip[8] = {0, 1, 0, 4, 1, 2, 3, 4};  // not first, nothing received
  TEST_ASSERT_FALSE(r.prov->writeData(skip, 8, r.t, ack));
  TEST_ASSERT_EQUAL_UINT8(1, ack[2]);
  uint8_t first[8] = {0, 0, 1, 4, 1, 2, 3, 4};
  TEST_ASSERT_TRUE(r.prov->writeData(first, 8, r.t, ack));
  uint8_t gap[8] = {0, 5, 0, 4, 1, 2, 3, 4};  // seq jump
  TEST_ASSERT_FALSE(r.prov->writeData(gap, 8, r.t, ack));
  TEST_ASSERT_EQUAL_UINT8(1, ack[2]);
  // oversize: keep sending 200-byte chunks until the 4124 byte cap trips
  uint8_t big[204];
  big[2] = 0; big[3] = 200;
  memset(big + 4, 0x55, 200);
  bool tripped = false;
  for (uint16_t s = 1; s < 40 && !tripped; s++) {
    big[0] = (uint8_t)(s >> 8); big[1] = (uint8_t)s;
    if (!r.prov->writeData(big, 204, r.t, ack)) { tripped = true; TEST_ASSERT_EQUAL_UINT8(2, ack[2]); }
  }
  TEST_ASSERT_TRUE(tripped);
  TEST_ASSERT_FALSE(r.store.has);
}

void test_data_before_session_is_refused() {
  Rig r;
  r.prov->boot(r.t, false);
  uint8_t ack[3];
  uint8_t first[8] = {0, 0, 1, 4, 1, 2, 3, 4};
  TEST_ASSERT_FALSE(r.prov->writeData(first, 8, r.t, ack));
  TEST_ASSERT_EQUAL_STRING("bad_session", r.prov->code());
}

void test_pop_is_never_accepted_over_the_air() {
  Rig r;
  r.prov->boot(r.t, false);
  TEST_ASSERT_FALSE(r.handshake(kPop, kPop));  // client put the PoP into the session write
  TEST_ASSERT_EQUAL_STRING("bad_session", r.prov->code());
}

void test_bundle_for_another_node_is_rejected_without_counting_as_a_guess() {
  Rig r;
  r.prov->boot(r.t, false);
  TEST_ASSERT_TRUE(r.handshake(kPop));
  TEST_ASSERT_FALSE(r.send(bundleJson("esp32-ffffffffffff")));
  TEST_ASSERT_EQUAL_STRING("bad_bundle", r.prov->code());
  TEST_ASSERT_FALSE(r.store.has);
  TEST_ASSERT_EQUAL_INT(0, r.prov->failures());  // authenticated sender, content error
}

void test_bundle_with_a_private_key_or_bad_fields_is_rejected() {
  NodeConfig c;
  std::string why;
  TEST_ASSERT_FALSE(parseBundle("{}", kNode, c, why));
  TEST_ASSERT_FALSE(parseBundle("not json", kNode, c, why));
  std::string b = bundleJson();
  TEST_ASSERT_TRUE(parseBundle(b, kNode, c, why));
  JsonDocument d = parse(b);
  d["ca_pem"] = "-----BEGIN PRIVATE KEY-----\nAAAA";
  std::string o;
  serializeJson(d, o);
  TEST_ASSERT_FALSE(parseBundle(o, kNode, c, why));
  d = parse(b);
  d["mqtt"]["port"] = 70000;
  o.clear();
  serializeJson(d, o);
  TEST_ASSERT_FALSE(parseBundle(o, kNode, c, why));
  TEST_ASSERT_TRUE(why.find("secret") == std::string::npos);  // reason names a field, never a value
}

void test_new_session_discards_the_previous_key_and_partial_data() {
  Rig r;
  r.prov->boot(r.t, false);
  TEST_ASSERT_TRUE(r.handshake(kPop));
  uint8_t ack[3];
  uint8_t first[8] = {0, 0, 1, 4, 1, 2, 3, 4};
  TEST_ASSERT_TRUE(r.prov->writeData(first, 8, r.t, ack));
  TEST_ASSERT_TRUE(r.attempt(kPop));  // fresh handshake + full transfer works
  TEST_ASSERT_TRUE(r.store.has);
}

void test_client_disconnect_drops_the_session() {
  Rig r;
  r.prov->boot(r.t, false);
  TEST_ASSERT_TRUE(r.handshake(kPop));
  r.prov->clientDisconnected(r.t);
  uint8_t ack[3];
  uint8_t first[8] = {0, 0, 1, 4, 1, 2, 3, 4};
  TEST_ASSERT_FALSE(r.prov->writeData(first, 8, r.t, ack));
  TEST_ASSERT_EQUAL_STRING("bad_session", r.prov->code());
}

void test_board_without_pop_refuses_ble_when_required_but_a_dev_policy_allows_it() {
  Rig r("");
  TEST_ASSERT_FALSE(r.prov->boot(r.t, false));
  TEST_ASSERT_FALSE(r.prov->advertising());
  TEST_ASSERT_FALSE(r.prov->buttonHold(r.t));
  r.pol.requirePop = false;
  r.make("");
  TEST_ASSERT_TRUE(r.prov->boot(r.t, false));
  JsonDocument d = parse(r.prov->info(r.t));
  TEST_ASSERT_FALSE(d["pop_required"].as<bool>());
  TEST_ASSERT_TRUE(r.attempt(""));  // contract v1 without PoP: confidentiality against passive sniffing only
  TEST_ASSERT_TRUE(r.store.has);
}

void test_factory_reset_wipes_config_keeps_pop_and_reopens_the_window() {
  Rig r;
  r.prov->boot(r.t, false);
  TEST_ASSERT_TRUE(r.attempt(kPop));
  r.prov->takeStored();
  r.prov->boot(r.t + 1000, true);  // reboot with credentials: BLE off
  TEST_ASSERT_FALSE(r.prov->advertising());
  TEST_ASSERT_TRUE(r.prov->factoryReset(r.t + 2000));  // BOOT held 10 s
  TEST_ASSERT_FALSE(r.store.has);
  TEST_ASSERT_TRUE(r.prov->advertising());
  JsonDocument d = parse(r.prov->info(r.t + 2000));
  TEST_ASSERT_TRUE(d["pop_required"].as<bool>());  // PoP survived the wipe
  r.t += 3000;
  TEST_ASSERT_TRUE(r.attempt(kPop));
}

void test_base64_roundtrip_and_strictness() {
  std::string out;
  uint8_t d[5] = {1, 2, 3, 250, 255};
  std::string e = b64encode(d, 5);
  TEST_ASSERT_TRUE(b64decode(e, out));
  TEST_ASSERT_EQUAL_UINT32(5, out.size());
  TEST_ASSERT_EQUAL_MEMORY(d, out.data(), 5);
  TEST_ASSERT_FALSE(b64decode("AAA", out));
  TEST_ASSERT_FALSE(b64decode("AA=A", out));
  TEST_ASSERT_FALSE(b64decode("AA!A", out));
  TEST_ASSERT_EQUAL_STRING("Zm9vYg==", b64encode((const uint8_t*)"foob", 4).c_str());
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_no_credentials_opens_the_window_and_advertises);
  RUN_TEST(test_with_credentials_ble_stays_off_until_the_button);
  RUN_TEST(test_button_hold_detector_fires_once_at_3s_and_once_at_10s);
  RUN_TEST(test_correct_pop_stores_config_and_turns_ble_off);
  RUN_TEST(test_wrong_pop_is_rejected_and_stores_nothing);
  RUN_TEST(test_n_wrong_attempts_lock_ble_and_the_cooldown_ends);
  RUN_TEST(test_lockout_on_a_configured_node_needs_the_button_again);
  RUN_TEST(test_a_success_between_failures_is_not_needed_but_good_attempt_after_some_failures_works);
  RUN_TEST(test_window_times_out_after_5_minutes_and_the_button_reopens_it);
  RUN_TEST(test_window_expiring_mid_transfer_drops_the_partial_data);
  RUN_TEST(test_idle_transfer_is_aborted_after_30s);
  RUN_TEST(test_chunk_ordering_and_size_are_enforced);
  RUN_TEST(test_data_before_session_is_refused);
  RUN_TEST(test_pop_is_never_accepted_over_the_air);
  RUN_TEST(test_bundle_for_another_node_is_rejected_without_counting_as_a_guess);
  RUN_TEST(test_bundle_with_a_private_key_or_bad_fields_is_rejected);
  RUN_TEST(test_new_session_discards_the_previous_key_and_partial_data);
  RUN_TEST(test_client_disconnect_drops_the_session);
  RUN_TEST(test_board_without_pop_refuses_ble_when_required_but_a_dev_policy_allows_it);
  RUN_TEST(test_factory_reset_wipes_config_keeps_pop_and_reopens_the_window);
  RUN_TEST(test_base64_roundtrip_and_strictness);
  return UNITY_END();
}
