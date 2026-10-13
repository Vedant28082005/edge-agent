// Time plausibility, backoff, serial config shell, declaration validation, small helpers.
#include "../common.h"

void setUp() {}
void tearDown() { setHal(nullptr); }

// ---- time plausibility (contracts/mqtt.md) -------------------------------------------------------------

void test_time_before_build_timestamp_is_rejected() {
  const int64_t build = 1760000000;
  TEST_ASSERT_FALSE(timePlausible(0, build));              // no RTC / SNTP not yet synced (1970)
  TEST_ASSERT_FALSE(timePlausible(build - 1, build));
  TEST_ASSERT_FALSE(timePlausible(946684800, build));      // 2000-01-01
  TEST_ASSERT_TRUE(timePlausible(build, build));
  TEST_ASSERT_TRUE(timePlausible(build + 3600, build));
}

void test_time_far_in_the_future_is_rejected() {
  const int64_t build = 1760000000;
  TEST_ASSERT_TRUE(timePlausible(build + kFiftyYearsS - 1, build));
  TEST_ASSERT_FALSE(timePlausible(build + kFiftyYearsS, build));
  TEST_ASSERT_FALSE(timePlausible(4102444800LL * 2, build));
}

void test_a_clock_that_moves_back_past_the_build_time_is_rejected_again() {
  const int64_t build = 1760000000;
  int64_t t = build + 100;
  TEST_ASSERT_TRUE(timePlausible(t, build));
  t = build - 5;  // a bad NTP answer winds the clock back
  TEST_ASSERT_FALSE(timePlausible(t, build));
}

void test_unset_build_timestamp_never_validates_anything() {
  TEST_ASSERT_FALSE(timePlausible(1760000000, 0));  // a build without FW_BUILD_TS must not accept every time
}

// ---- backoff -----------------------------------------------------------------------------------------------

void test_backoff_grows_and_is_capped_with_bounded_jitter() {
  Backoff b(1000, 60000);
  uint32_t prevCenter = 0;
  for (int k = 0; k < 12; k++) {
    uint32_t d = b.nextDelayMs(0xdeadbeef + k * 7919);
    uint32_t center = 1000u << (k < 6 ? k : 6);
    if (center > 60000) center = 60000;
    TEST_ASSERT_TRUE(d >= center * 8 / 10);
    TEST_ASSERT_TRUE(d <= center * 12 / 10);
    TEST_ASSERT_TRUE(center >= prevCenter);
    prevCenter = center;
  }
}

void test_backoff_resets_only_after_a_stable_connection() {
  Backoff b(1000, 60000, 30000);
  for (int k = 0; k < 5; k++) b.nextDelayMs(1);
  b.onConnected(1000);
  b.onDisconnected(1000 + 5000);  // flapped after 5 s: keep the long delays
  TEST_ASSERT_EQUAL_UINT32(5, b.attempts());
  b.onConnected(100000);
  b.onDisconnected(100000 + 31000);  // a real connection
  TEST_ASSERT_EQUAL_UINT32(0, b.attempts());
}

// ---- node config + serial shell ---------------------------------------------------------------------------------

static std::string fakeCa() {
  std::string pem = "-----BEGIN CERTIFICATE-----\n";
  for (int k = 0; k < 6; k++) pem += "MIIBszCCAVmgAwIBAgIUQ0FfVGVzdE9ubHlOb3RBUmVhbENlcnRpZmljYXRlMAoG\n";
  return pem + "-----END CERTIFICATE-----\n";
}

static std::string sh(ConfigShell& s, const std::string& line, bool* reboot = nullptr) {
  bool rb = false;
  std::string r = s.handleLine(line, rb);
  if (reboot) *reboot = rb;
  return r;
}

static void fillShell(ConfigShell& s) {
  sh(s, "set wifi_ssid Home Net");
  sh(s, "set wifi_pass correct horse battery");
  sh(s, "set mqtt_host 192.168.1.10");
  sh(s, "set mqtt_port 8883");
  sh(s, "set mqtt_user esp32-a1b2c3d4e5f6");
  sh(s, "set mqtt_pass s3cretMqttPassw0rd");
  sh(s, "ca clear");
  std::string pem = fakeCa(), line;
  for (char c : pem) {
    if (c == '\n') { sh(s, "ca line " + line); line.clear(); } else line += c;
  }
  TEST_ASSERT_EQUAL_STRING("ok", sh(s, "ca end").c_str());
}

void test_shell_collects_and_saves_a_complete_config() {
  MemConfigStore store;
  ConfigShell s(store);
  fillShell(s);
  bool reboot = false;
  TEST_ASSERT_EQUAL_STRING("ok saved, rebooting", sh(s, "save", &reboot).c_str());
  TEST_ASSERT_TRUE(reboot);
  TEST_ASSERT_TRUE(store.has);
  TEST_ASSERT_EQUAL_STRING("Home Net", store.cfg.wifiSsid.c_str());       // spaces survive
  TEST_ASSERT_EQUAL_STRING("192.168.1.10", store.cfg.mqttHost.c_str());
  TEST_ASSERT_EQUAL(8883, store.cfg.mqttPort);
  TEST_ASSERT_EQUAL_STRING(fakeCa().c_str(), store.cfg.caPem.c_str());
  TEST_ASSERT_TRUE(store.cfg.complete());
}

void test_shell_never_echoes_secrets() {
  MemConfigStore store;
  ConfigShell s(store);
  fillShell(s);
  std::string all = sh(s, "status") + sh(s, "help") + sh(s, "set mqtt_pass anotherSecret") + sh(s, "bogus");
  TEST_ASSERT_NULL(strstr(all.c_str(), "s3cret"));
  TEST_ASSERT_NULL(strstr(all.c_str(), "anotherSecret"));
  TEST_ASSERT_NULL(strstr(all.c_str(), "correct horse"));
  TEST_ASSERT_NULL(strstr(all.c_str(), "BEGIN CERT"));
  TEST_ASSERT_NOT_NULL(strstr(all.c_str(), "mqtt_pass=set"));
}

void test_shell_rejects_incomplete_or_bad_config_and_saves_nothing() {
  MemConfigStore store;
  ConfigShell s(store);
  bool reboot = true;
  TEST_ASSERT_EQUAL_STRING("err invalid wifi_ssid", sh(s, "save", &reboot).c_str());
  TEST_ASSERT_FALSE(reboot);
  TEST_ASSERT_FALSE(store.has);
  TEST_ASSERT_EQUAL_STRING("err invalid mqtt_port", sh(s, "set mqtt_port 99999").c_str());
  TEST_ASSERT_EQUAL_STRING("err invalid mqtt_port", sh(s, "set mqtt_port abc").c_str());
  TEST_ASSERT_EQUAL_STRING("err bad_key", sh(s, "set colour red").c_str());
  TEST_ASSERT_EQUAL_STRING("err ca_not_started", sh(s, "ca line x").c_str());
  sh(s, "ca clear");
  sh(s, "ca line not a certificate");
  TEST_ASSERT_EQUAL_STRING("err invalid ca", sh(s, "ca end").c_str());
  TEST_ASSERT_EQUAL_STRING("err too_long", sh(s, std::string(500, 'a')).c_str());
  TEST_ASSERT_EQUAL_STRING("err unknown_command", sh(s, "format c:").c_str());
  fillShell(s);
  sh(s, "set mqtt_host bad host!");
  TEST_ASSERT_EQUAL_STRING("err invalid mqtt_host", sh(s, "save").c_str());
  sh(s, "set mqtt_host broker.lan");
  sh(s, "set wifi_pass short");
  TEST_ASSERT_EQUAL_STRING("err invalid wifi_pass", sh(s, "save").c_str());
}

void test_shell_wipe_clears_storage() {
  MemConfigStore store;
  ConfigShell s(store);
  fillShell(s);
  sh(s, "save");
  bool reboot = false;
  sh(s, "wipe", &reboot);
  TEST_ASSERT_TRUE(reboot);
  TEST_ASSERT_FALSE(store.has);
  TEST_ASSERT_FALSE(store.cfg.complete());
}

void test_second_shell_starts_from_the_stored_config() {
  MemConfigStore store;
  { ConfigShell s(store); fillShell(s); sh(s, "save"); }
  ConfigShell again(store);
  TEST_ASSERT_TRUE(again.draft().complete());
  sh(again, "set mqtt_port 8884");  // change one field, keep the rest
  TEST_ASSERT_EQUAL_STRING("ok saved, rebooting", sh(again, "save").c_str());
  TEST_ASSERT_EQUAL(8884, store.cfg.mqttPort);
  TEST_ASSERT_EQUAL_STRING("192.168.1.10", store.cfg.mqttHost.c_str());
}

// ---- declaration validation ----------------------------------------------------------------------------------------

static bool declOk(EdgeNode& n, FakeHal& h) {
  setHal(&h);
  return n.model().validate();
}

static bool hasError(EdgeNode& n, const char* needle) {
  for (const std::string& e : n.model().errors()) if (e.find(needle) != std::string::npos) return true;
  return false;
}

void test_unknown_capability_is_rejected() {
  FakeHal h; EdgeNode n;
  n.endpoint("a", GENERIC).boolCap("warp_drive", RelayOut(26));
  TEST_ASSERT_FALSE(declOk(n, h));
  TEST_ASSERT_TRUE(hasError(n, "vocabulary"));
  FakeHal h2; EdgeNode n2;
  n2.endpoint("a", GENERIC).boolCap("temperature", RelayOut(26));  // sensor capability as actuator
  TEST_ASSERT_FALSE(declOk(n2, h2));
}

void test_range_and_default_checks() {
  { FakeHal h; EdgeNode n; n.endpoint("l", LIGHT).intCap("brightness", 0, 200, PwmOut(25, 1000)); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("f", FAN).intCap("speed", 5, 1, PwmOut(25, 1000)); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("f", FAN).intCap("speed", 1, 5, PwmOut(25, 1000)).bootValue(9); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("f", FAN).intCap("speed", 1, 5, PwmOut(25, 1000)).failsafeValue(0); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("f", FAN).intCap("speed", 1, 5, PwmOut(25, 1000)).maxOnS(5); TEST_ASSERT_FALSE(declOk(n, h)); }  // max_on is for bools
  { FakeHal h; EdgeNode n; n.endpoint("f", FAN).boolCap("power", RelayOut(26)).maxOnS(90000); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("f", FAN).boolCap("power", RelayOut(26)).bootValue(3); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("f", FAN).boolCap("power", RelayOut(26)).unit("parsec"); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("f", FAN).boolCap("power", RelayOut(26)).bootValue(false); TEST_ASSERT_TRUE(declOk(n, h)); }
}

void test_structure_checks() {
  { FakeHal h; EdgeNode n; TEST_ASSERT_FALSE(declOk(n, h)); }  // no endpoints
  { FakeHal h; EdgeNode n; n.endpoint("Bad-Id", FAN).boolCap("power", RelayOut(26)); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("a", FAN).boolCap("power", RelayOut(26)); n.endpoint("a", FAN).boolCap("power", RelayOut(27)); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("a", FAN).boolCap("power", RelayOut(26)).boolCap("power", RelayOut(27)); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("a", FAN); TEST_ASSERT_FALSE(declOk(n, h)); }  // no capabilities
  { FakeHal h; EdgeNode n; n.hw("bad hw name"); n.endpoint("a", FAN).boolCap("power", RelayOut(26)); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("a", FAN).boolCap("power", RelayOut(26)).localToggle("speed", 33); TEST_ASSERT_FALSE(declOk(n, h)); }  // unknown cap
  { FakeHal h; EdgeNode n; n.endpoint("a", FAN).intCap("speed", 1, 5, PwmOut(27, 1000)).localToggle("speed", 33); TEST_ASSERT_FALSE(declOk(n, h)); }  // toggle needs bool
}

void test_sensor_declaration_checks() {
  { FakeHal h; EdgeNode n; n.endpoint("s", SENSOR).floatSensor("temperature", FakeSensor(), 0); TEST_ASSERT_FALSE(declOk(n, h)); }   // interval 0
  { FakeHal h; EdgeNode n; n.endpoint("s", SENSOR).floatSensor("temperature", FakeSensor(), 90000); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("s", SENSOR).boolSensor("temperature", FakeSensor(), 30); TEST_ASSERT_FALSE(declOk(n, h)); }  // wrong type
  { FakeHal h; EdgeNode n; n.endpoint("s", SENSOR).enumSensor("contact", {"on", "off"}, FakeSensor(), 30); TEST_ASSERT_FALSE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("s", SENSOR).floatSensor("temperature", FakeSensor(), 30).bootValue(1); TEST_ASSERT_FALSE(declOk(n, h)); }  // sensors have no boot value
  { FakeHal h; EdgeNode n; n.endpoint("s", SENSOR).floatSensor("temperature", FakeSensor(), 30); TEST_ASSERT_TRUE(declOk(n, h)); }
}

void test_pin_checks() {
  { FakeHal h; EdgeNode n; n.endpoint("a", FAN).boolCap("power", RelayOut(6)); TEST_ASSERT_FALSE(declOk(n, h)); }    // flash pin
  { FakeHal h; EdgeNode n; n.endpoint("a", FAN).boolCap("power", RelayOut(35)); TEST_ASSERT_FALSE(declOk(n, h)); }   // input-only
  { FakeHal h; EdgeNode n; n.endpoint("a", FAN).boolCap("power", RelayOut(26)).localToggle("power", 35); TEST_ASSERT_FALSE(declOk(n, h)); }  // no pull-up on 35
  { FakeHal h; EdgeNode n; n.endpoint("s", SENSOR).floatSensor("illuminance", LdrIn(25), 30); TEST_ASSERT_FALSE(declOk(n, h)); }  // ADC2 pin
  { FakeHal h; EdgeNode n; n.endpoint("s", SENSOR).floatSensor("illuminance", LdrIn(34), 30); TEST_ASSERT_TRUE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("s", SENSOR).floatSensor("temperature", DhtSensor(4).temperature(), 30); TEST_ASSERT_TRUE(declOk(n, h)); }
  { FakeHal h; EdgeNode n; n.endpoint("s", SENSOR).floatSensor("temperature", DhtSensor(35).temperature(), 30); TEST_ASSERT_FALSE(declOk(n, h)); }
}

void test_invalid_declaration_does_not_touch_any_pin() {
  FakeHal h;
  setHal(&h);
  EdgeNode n;
  n.endpoint("a", FAN).boolCap("power", RelayOut(26)).boolCap("power", RelayOut(27));  // duplicate
  TEST_ASSERT_FALSE(n.core().begin(0));
  TEST_ASSERT_EQUAL(0, (int)h.writes.size());
  TEST_ASSERT_EQUAL(0, (int)h.isOutput.size());
}

// ---- drivers -------------------------------------------------------------------------------------------------------

void test_pwm_maps_value_to_duty_and_power_gates_it() {
  FakeHal h; setHal(&h);
  EdgeNode n;
  PwmChannel strip(25, 5000);
  n.endpoint("l", LIGHT).boolCap("power", strip.power()).bootValue(false).intCap("brightness", 0, 100, strip.level()).bootValue(50);
  TEST_ASSERT_TRUE(n.core().begin(0));
  TEST_ASSERT_EQUAL_UINT32(0, h.pwmDuty[0]);  // power off: dark whatever the brightness
  send(n.core(), cmdSet(1, "l", "{\"power\":true}"), 10);
  TEST_ASSERT_EQUAL_UINT32(128, h.pwmDuty[0]);  // 50 % of 255
  send(n.core(), cmdSet(2, "l", "{\"brightness\":100}"), 20);
  TEST_ASSERT_EQUAL_UINT32(255, h.pwmDuty[0]);
  send(n.core(), cmdSet(3, "l", "{\"brightness\":0}"), 30);
  TEST_ASSERT_EQUAL_UINT32(0, h.pwmDuty[0]);
}

void test_reed_pir_and_ldr_drivers() {
  FakeHal h; setHal(&h);
  ReedIn reed(27);
  PirIn pir(14);
  LdrIn ldr(34, 10000.0f, true);
  TEST_ASSERT_TRUE(reed.begin()); TEST_ASSERT_TRUE(pir.begin()); TEST_ASSERT_TRUE(ldr.begin());
  Value v;
  h.level[27] = true;  // pull-up, switch open
  reed.read(0, v); TEST_ASSERT_EQUAL(0, v.i);  // "open"
  h.level[27] = false;  // magnet closes the reed
  reed.read(10, v); TEST_ASSERT_EQUAL(0, v.i);   // not yet debounced
  reed.read(70, v); TEST_ASSERT_EQUAL(1, v.i);   // "closed"
  h.level[14] = false; pir.read(0, v); TEST_ASSERT_FALSE(v.b);
  h.level[14] = true; pir.read(10, v); pir.read(80, v); TEST_ASSERT_TRUE(v.b);
  h.adcValue[34] = 2048;  // mid-scale: LDR == fixed resistor == 10 k -> luxAt10k
  TEST_ASSERT_TRUE(ldr.read(0, v));
  TEST_ASSERT_FLOAT_WITHIN(0.5, 10.0, v.f);
  h.adcValue[34] = 500;  // low voltage across the LDR = low resistance = brighter
  TEST_ASSERT_TRUE(ldr.read(0, v));
  TEST_ASSERT_TRUE(v.f > 10.0f);
  h.adcValue[34] = 4095;  // rail: do not invent a lux value
  TEST_ASSERT_FALSE(ldr.read(0, v));
  h.adcValue[34] = 0;
  TEST_ASSERT_FALSE(ldr.read(0, v));
}

void test_dht_shares_one_read_between_channels_and_rate_limits() {
  FakeHal h; setHal(&h);
  DhtSensor dht(4);
  int reads = 0;
  bool ok = true;
  dht.setReaderForTest([&](float& t, float& hum) { reads++; t = 22.5f; hum = 41.0f; return ok; });
  auto t = dht.temperature();
  auto hu = dht.humidity();
  Value v;
  TEST_ASSERT_TRUE(t.read(1000, v)); TEST_ASSERT_FLOAT_WITHIN(0.01, 22.5, v.f);
  TEST_ASSERT_TRUE(hu.read(1010, v)); TEST_ASSERT_FLOAT_WITHIN(0.01, 41.0, v.f);
  TEST_ASSERT_EQUAL(1, reads);  // one physical read for both
  t.read(2500, v);
  TEST_ASSERT_EQUAL(1, reads);  // < 2 s since the last read: cached
  ok = false;
  TEST_ASSERT_FALSE(t.read(4000, v));  // failed read: reported as no value, not as the old one
  TEST_ASSERT_EQUAL(2, reads);
}

// ---- small helpers --------------------------------------------------------------------------------------------------

void test_node_id_and_string_validators() {
  uint8_t mac[6] = {0x0a, 0x0b, 0x00, 0xff, 0x10, 0x01};
  TEST_ASSERT_EQUAL_STRING("esp32-0a0b00ff1001", nodeIdFromMac(mac).c_str());
  TEST_ASSERT_TRUE(isValidUuid("3f0c1d2e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"));
  TEST_ASSERT_FALSE(isValidUuid("3f0c1d2e4a5b4c6d8e7f0a1b2c3d4e5f"));
  TEST_ASSERT_FALSE(isValidUuid("zf0c1d2e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"));
  TEST_ASSERT_TRUE(isValidLocalId("ch1")); TEST_ASSERT_FALSE(isValidLocalId("Ch1")); TEST_ASSERT_FALSE(isValidLocalId("a-b"));
  TEST_ASSERT_FALSE(isValidLocalId("0123456789abcdefg"));
  TEST_ASSERT_TRUE(isValidHwString("esp32-4relay")); TEST_ASSERT_FALSE(isValidHwString("a b"));
  TEST_ASSERT_TRUE(isValidFwString("1.2.3-rc1+x")); TEST_ASSERT_FALSE(isValidFwString("1_2"));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_time_before_build_timestamp_is_rejected);
  RUN_TEST(test_time_far_in_the_future_is_rejected);
  RUN_TEST(test_a_clock_that_moves_back_past_the_build_time_is_rejected_again);
  RUN_TEST(test_unset_build_timestamp_never_validates_anything);
  RUN_TEST(test_backoff_grows_and_is_capped_with_bounded_jitter);
  RUN_TEST(test_backoff_resets_only_after_a_stable_connection);
  RUN_TEST(test_shell_collects_and_saves_a_complete_config);
  RUN_TEST(test_shell_never_echoes_secrets);
  RUN_TEST(test_shell_rejects_incomplete_or_bad_config_and_saves_nothing);
  RUN_TEST(test_shell_wipe_clears_storage);
  RUN_TEST(test_second_shell_starts_from_the_stored_config);
  RUN_TEST(test_unknown_capability_is_rejected);
  RUN_TEST(test_range_and_default_checks);
  RUN_TEST(test_structure_checks);
  RUN_TEST(test_sensor_declaration_checks);
  RUN_TEST(test_pin_checks);
  RUN_TEST(test_invalid_declaration_does_not_touch_any_pin);
  RUN_TEST(test_pwm_maps_value_to_duty_and_power_gates_it);
  RUN_TEST(test_reed_pir_and_ldr_drivers);
  RUN_TEST(test_dht_shares_one_read_between_channels_and_rate_limits);
  RUN_TEST(test_node_id_and_string_validators);
  return UNITY_END();
}
