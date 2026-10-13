#include "../common.h"

static FanRig* rig;
void setUp() { rig = new FanRig(); }
void tearDown() { delete rig; rig = nullptr; }

static NodeCore& core() { return rig->core(); }

void test_boot_values_applied_at_begin() {
  rig->begin();
  TEST_ASSERT_FALSE(rig->hal.high(26));  // relay off
  TEST_ASSERT_EQUAL(1, core().value("ch1", "speed")->i);
  TEST_ASSERT_EQUAL_UINT32(51, rig->hal.pwmDuty[0]);  // 1/5 of 255
}

void test_valid_set_applies_acks_and_marks_state() {
  rig->begin();
  core().takeDirty();
  auto o = send(core(), cmdSet(1, "ch1", "{\"power\":true,\"speed\":3}"), 2000);
  TEST_ASSERT_TRUE(o.hasAck);
  JsonDocument a = parse(o.ack);
  TEST_ASSERT_TRUE(a["ok"].as<bool>());
  TEST_ASSERT_EQUAL_STRING(uuid(1).c_str(), a["cmd_id"].as<const char*>());
  TEST_ASSERT_EQUAL_STRING("ch1", a["endpoint"].as<const char*>());
  TEST_ASSERT_TRUE(a["error"].isNull());
  TEST_ASSERT_TRUE(a["values"]["power"].as<bool>());
  TEST_ASSERT_EQUAL(3, a["values"]["speed"].as<int>());
  TEST_ASSERT_TRUE(rig->hal.high(26));
  TEST_ASSERT_EQUAL_UINT32(153, rig->hal.pwmDuty[0]);  // 3/5
  TEST_ASSERT_TRUE(o.stateChanged);
  TEST_ASSERT_TRUE(core().dirty());
  std::string st;
  TEST_ASSERT_TRUE(core().stateJson(1760000000, st));
  JsonDocument s = parse(st);
  TEST_ASSERT_EQUAL(3, s["endpoints"]["ch1"]["speed"].as<int>());
  TEST_ASSERT_TRUE(s["endpoints"]["ch1"]["power"].as<bool>());
}

void test_integral_float_is_accepted_for_int_capability() {
  rig->begin();
  JsonDocument a = parse(send(core(), cmdSet(2, "ch1", "{\"speed\":4.0}"), 2000).ack);
  TEST_ASSERT_TRUE(a["ok"].as<bool>());
  TEST_ASSERT_EQUAL(4, core().value("ch1", "speed")->i);
}

void test_duplicate_cmd_id_is_not_applied_twice() {
  rig->begin();
  std::string on = cmdSet(1, "ch1", "{\"power\":true}"), off = cmdSet(2, "ch1", "{\"power\":false}");
  auto first = send(core(), on, 2000);
  send(core(), off, 2100);
  TEST_ASSERT_FALSE(rig->hal.high(26));
  size_t writes = rig->hal.writes.size();
  auto again = send(core(), on, 2200);  // the hub retries the first command
  TEST_ASSERT_TRUE(again.replay);
  TEST_ASSERT_TRUE(again.hasAck);
  TEST_ASSERT_EQUAL_STRING(first.ack.c_str(), again.ack.c_str());
  TEST_ASSERT_FALSE(rig->hal.high(26));  // still off: the duplicate did nothing
  TEST_ASSERT_EQUAL(writes, rig->hal.writes.size());
}

void test_dedup_remembers_exactly_the_last_16() {
  rig->begin();
  for (unsigned n = 1; n <= 17; n++) send(core(), cmdSet(n, "ch1", "{\"speed\":2}"), 2000 + n);  // id 1 falls out
  TEST_ASSERT_TRUE(send(core(), cmdSet(2, "ch1", "{\"speed\":2}"), 3000).replay);
  TEST_ASSERT_FALSE(send(core(), cmdSet(1, "ch1", "{\"speed\":2}"), 3001).replay);
}

static std::string errOf(const std::string& cmd) {
  auto o = send(core(), cmd, 5000);
  TEST_ASSERT_TRUE_MESSAGE(o.hasAck, cmd.c_str());
  JsonDocument a = parse(o.ack);
  TEST_ASSERT_FALSE_MESSAGE(a["ok"].as<bool>(), cmd.c_str());
  TEST_ASSERT_TRUE(a["values"].is<JsonObject>());
  TEST_ASSERT_EQUAL(0, (int)a["values"].size());
  return a["error"].as<const char*>();
}

void test_validation_against_own_ranges() {
  rig->begin();
  TEST_ASSERT_EQUAL_STRING("out_of_range", errOf(cmdSet(10, "ch1", "{\"speed\":6}")).c_str());
  TEST_ASSERT_EQUAL_STRING("out_of_range", errOf(cmdSet(11, "ch1", "{\"speed\":0}")).c_str());
  TEST_ASSERT_EQUAL_STRING("out_of_range", errOf(cmdSet(12, "ch1", "{\"speed\":-1}")).c_str());
  TEST_ASSERT_EQUAL_STRING("bad_type", errOf(cmdSet(13, "ch1", "{\"speed\":2.5}")).c_str());
  TEST_ASSERT_EQUAL_STRING("bad_type", errOf(cmdSet(14, "ch1", "{\"speed\":\"3\"}")).c_str());
  TEST_ASSERT_EQUAL_STRING("bad_type", errOf(cmdSet(15, "ch1", "{\"power\":\"yes\"}")).c_str());
  TEST_ASSERT_EQUAL_STRING("bad_type", errOf(cmdSet(16, "ch1", "{\"power\":1}")).c_str());
  TEST_ASSERT_EQUAL_STRING("unknown_capability", errOf(cmdSet(17, "ch1", "{\"brightness\":5}")).c_str());
  TEST_ASSERT_EQUAL_STRING("unknown_capability", errOf(cmdSet(18, "ch1", "{\"warp\":5}")).c_str());
  TEST_ASSERT_EQUAL_STRING("unknown_endpoint", errOf(cmdSet(19, "nope", "{\"power\":true}")).c_str());
  TEST_ASSERT_EQUAL_STRING("unsupported", errOf(cmdSet(20, "t1", "{\"temperature\":5.0}")).c_str());  // sensors are read-only
  TEST_ASSERT_EQUAL_STRING("bad_request", errOf(cmdSet(21, "ch1", "{}")).c_str());
  TEST_ASSERT_EQUAL_STRING("bad_request", errOf(cmdSet(22, "ch1", "{\"power\":true}", "root")).c_str());
  TEST_ASSERT_EQUAL_STRING("bad_request", errOf(cmdRaw(23, "\"endpoint\":\"ch1\",\"set\":{\"power\":true}")).c_str());
  TEST_ASSERT_EQUAL_STRING("bad_request", errOf(cmdRaw(24, "\"endpoint\":\"ch1\",\"set\":[1],\"by\":\"agent\"")).c_str());
  TEST_ASSERT_EQUAL_STRING("unsupported", errOf(cmdRaw(25, "\"cmd\":\"reflash\"")).c_str());
}

void test_invalid_command_applies_nothing_even_for_its_valid_parts() {
  rig->begin();
  size_t writes = rig->hal.writes.size();
  errOf(cmdSet(30, "ch1", "{\"power\":true,\"speed\":9}"));
  TEST_ASSERT_FALSE(rig->hal.high(26));
  TEST_ASSERT_EQUAL(writes, rig->hal.writes.size());
  TEST_ASSERT_FALSE(core().value("ch1", "power")->b);
}

void test_unackable_garbage_is_dropped() {
  rig->begin();
  const char* junk[] = {"", "not json", "[1,2]", "{}", "{\"cmd_id\":5}",
                        "{\"cmd_id\":\"abc\",\"cmd\":\"identify\",\"duration_s\":5}",
                        "{\"cmd_id\":\"3F0C1D2E-4A5B-4C6D-8E7F-0A1B2C3D4E5F\",\"cmd\":\"unprovision\"}"};  // uppercase is not a valid uuid
  for (const char* j : junk) TEST_ASSERT_FALSE_MESSAGE(core().handleCommand(j, strlen(j), 1).hasAck, j);
  std::string big(2000, 'x');
  TEST_ASSERT_FALSE(core().handleCommand(big.c_str(), big.size(), 1).hasAck);
}

void test_identify_and_unprovision() {
  rig->begin();
  auto o = send(core(), cmdRaw(40, "\"cmd\":\"identify\",\"duration_s\":10"), 100);
  TEST_ASSERT_EQUAL(NodeCore::Outcome::Identify, o.action);
  TEST_ASSERT_EQUAL_UINT32(10, o.identifyS);
  JsonDocument a = parse(o.ack);
  TEST_ASSERT_TRUE(a["ok"].as<bool>());
  TEST_ASSERT_TRUE(a["endpoint"].isNull());
  JsonDocument b = parse(send(core(), cmdRaw(41, "\"cmd\":\"identify\",\"duration_s\":61"), 100).ack);
  TEST_ASSERT_EQUAL_STRING("bad_request", b["error"].as<const char*>());
  std::string un = cmdRaw(42, "\"cmd\":\"unprovision\"");
  auto u = send(core(), un, 100);
  TEST_ASSERT_EQUAL(NodeCore::Outcome::Unprovision, u.action);
  TEST_ASSERT_TRUE(parse(u.ack)["ok"].as<bool>());
  auto replay = send(core(), un, 200);  // a retry must not trigger a second wipe
  TEST_ASSERT_TRUE(replay.replay);
  TEST_ASSERT_EQUAL(NodeCore::Outcome::None, replay.action);
}

void test_by_field_is_validated_not_trusted() {
  rig->begin();
  TEST_ASSERT_EQUAL_STRING("bad_request", errOf(cmdSet(50, "ch1", "{\"power\":true}", "user:<script>")).c_str());
  TEST_ASSERT_TRUE(isValidBy("user:vedant"));
  TEST_ASSERT_TRUE(isValidBy("agent"));
  TEST_ASSERT_TRUE(isValidBy("scene:good_night"));
  TEST_ASSERT_TRUE(isValidBy("rule:r-1"));
  TEST_ASSERT_FALSE(isValidBy("user:"));
  TEST_ASSERT_FALSE(isValidBy("scene:a.b"));
}

void test_sensor_reporting_deadband_and_failed_reads() {
  rig->begin();
  core().takeDirty();
  std::string st;
  TEST_ASSERT_NULL(core().value("t1", "temperature"));  // nothing read yet: nothing invented
  TEST_ASSERT_TRUE(core().sensorDue(0, 1000));
  TEST_ASSERT_TRUE(core().submitSensor(0, Value::makeFloat(27.4f), 1000));
  TEST_ASSERT_FALSE(core().submitSensor(0, Value::makeFloat(27.5f), 2000));  // inside the 0.2 deadband
  TEST_ASSERT_TRUE(core().submitSensor(0, Value::makeFloat(27.7f), 3000));
  TEST_ASSERT_FALSE(core().submitSensor(0, Value::makeFloat(NAN), 4000));  // junk is never published
  TEST_ASSERT_FALSE(core().sensorDue(0, 4000 + 29000));
  TEST_ASSERT_TRUE(core().sensorDue(0, 4000 + 30000));
  TEST_ASSERT_TRUE(core().stateJson(1760000000, st));
  JsonDocument s = parse(st);
  TEST_ASSERT_FLOAT_WITHIN(0.001, 27.7, s["endpoints"]["t1"]["temperature"].as<float>());
}

void test_effective_heartbeat_follows_fastest_sensor() {
  rig->begin();
  TEST_ASSERT_EQUAL_UINT32(30, core().effectiveHeartbeatS());  // sensor interval 30 s < heartbeat 60 s
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_boot_values_applied_at_begin);
  RUN_TEST(test_valid_set_applies_acks_and_marks_state);
  RUN_TEST(test_integral_float_is_accepted_for_int_capability);
  RUN_TEST(test_duplicate_cmd_id_is_not_applied_twice);
  RUN_TEST(test_dedup_remembers_exactly_the_last_16);
  RUN_TEST(test_validation_against_own_ranges);
  RUN_TEST(test_invalid_command_applies_nothing_even_for_its_valid_parts);
  RUN_TEST(test_unackable_garbage_is_dropped);
  RUN_TEST(test_identify_and_unprovision);
  RUN_TEST(test_by_field_is_validated_not_trusted);
  RUN_TEST(test_sensor_reporting_deadband_and_failed_reads);
  RUN_TEST(test_effective_heartbeat_follows_fastest_sensor);
  return UNITY_END();
}
