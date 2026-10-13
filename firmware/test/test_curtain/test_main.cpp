// Curtain driver: open/close interlock, dead time, travel-time position, limit switches, max run time.
#include "../common.h"

static const int OPEN = 26, CLOSE = 27, LIM_OPEN = 32, LIM_CLOSED = 33;

static FakeHal* fh;
static int violations;

void setUp() {
  fh = new FakeHal();
  setHal(fh);
  violations = 0;
  // The interlock invariant, checked after EVERY pin write: both outputs are never active at once.
  fh->afterWrite = [](FakeHal& h) { if (h.high(OPEN) && h.high(CLOSE)) violations++; };
}
void tearDown() { setHal(nullptr); delete fh; }

static CurtainMotor calibrated() { return CurtainMotor(OPEN, CLOSE).travelTime(10000, 10000).maxRun(15000).deadTime(500); }

// Bring a motor to begin() state at an assumed position.
static void start(CurtainMotor& m, int assumed) {
  TEST_ASSERT_TRUE(m.begin());
  m.applyBoot(Value::makeInt(assumed), 0);
}

static uint32_t run(CurtainMotor& m, uint32_t from, uint32_t ms, uint32_t step = 20) {
  uint32_t t = from;
  for (; t < from + ms; t += step) m.tick(t);
  return t;
}

void test_outputs_idle_and_safe_at_begin_and_boot_does_not_move() {
  CurtainMotor m = calibrated();
  start(m, 100);
  TEST_ASSERT_FALSE(fh->high(OPEN));
  TEST_ASSERT_FALSE(fh->high(CLOSE));
  Value v;
  TEST_ASSERT_FALSE(m.pollChanged(v));
  for (auto& w : fh->writes) TEST_ASSERT_FALSE(w.high);  // never energised
}

void test_open_runs_for_the_calibrated_time_and_reports_position() {
  CurtainMotor m = calibrated();
  start(m, 0);
  TEST_ASSERT_EQUAL(ApplyResult::Ok, m.apply(Value::makeInt(50), 1000));
  TEST_ASSERT_TRUE(fh->high(OPEN));
  TEST_ASSERT_FALSE(fh->high(CLOSE));
  uint32_t t = run(m, 1000, 4900);
  TEST_ASSERT_TRUE(fh->high(OPEN));  // not yet at 50 % (5000 ms)
  run(m, t, 400);
  TEST_ASSERT_FALSE(fh->high(OPEN));
  Value v;
  TEST_ASSERT_TRUE(m.pollChanged(v));
  TEST_ASSERT_INT_WITHIN(1, 50, v.i);
  TEST_ASSERT_EQUAL(0, violations);
}

void test_reversal_waits_the_dead_time_with_both_outputs_off() {
  CurtainMotor m = calibrated();
  start(m, 0);
  m.apply(Value::makeInt(100), 1000);
  run(m, 1000, 2000);
  TEST_ASSERT_TRUE(fh->high(OPEN));
  uint32_t t = 3000;
  m.apply(Value::makeInt(0), t);  // reverse while opening
  TEST_ASSERT_FALSE(fh->high(OPEN));
  TEST_ASSERT_FALSE(fh->high(CLOSE));  // dead time
  for (; t < 3480; t += 20) { m.tick(t); TEST_ASSERT_FALSE(fh->high(CLOSE)); TEST_ASSERT_FALSE(fh->high(OPEN)); }
  for (; t < 3600; t += 20) m.tick(t);
  TEST_ASSERT_TRUE(fh->high(CLOSE));
  TEST_ASSERT_FALSE(fh->high(OPEN));
  TEST_ASSERT_EQUAL(0, violations);
}

void test_interlock_holds_under_random_commands() {
  CurtainMotor m = CurtainMotor(OPEN, CLOSE).travelTime(8000, 9000).maxRun(12000).deadTime(300);
  start(m, 40);
  uint32_t x = 12345, t = 0;
  for (int i = 0; i < 40000; i++) {
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    t += 10 + (x % 40);
    if (x % 97 == 0) m.apply(Value::makeInt((int)(x >> 8) % 101), t);
    m.tick(t);
    TEST_ASSERT_FALSE(fh->high(OPEN) && fh->high(CLOSE));
  }
  TEST_ASSERT_EQUAL(0, violations);
  TEST_ASSERT_TRUE(fh->writes.size() > 100);  // the fuzz actually moved the motor
}

void test_active_low_wiring_is_respected() {
  CurtainMotor m = CurtainMotor(OPEN, CLOSE, /*activeHigh=*/false).travelTime(10000, 10000).maxRun(15000);
  TEST_ASSERT_TRUE(m.begin());
  TEST_ASSERT_TRUE(fh->high(OPEN));  // idle = high for active-low
  TEST_ASSERT_TRUE(fh->high(CLOSE));
  m.applyBoot(Value::makeInt(0), 0);
  m.apply(Value::makeInt(100), 100);
  TEST_ASSERT_FALSE(fh->high(OPEN));  // energised
  TEST_ASSERT_TRUE(fh->high(CLOSE));
}

void test_limit_switch_stops_the_motor_and_learns_the_travel_time() {
  CurtainMotor m = CurtainMotor(OPEN, CLOSE).limitSwitches(LIM_OPEN, LIM_CLOSED).maxRun(30000);
  { std::string e; TEST_ASSERT_TRUE(m.validate(e)); }
  start(m, 0);
  fh->level[LIM_OPEN] = true;     // released (pull-up)
  fh->level[LIM_CLOSED] = false;  // closed limit is pressed: we really are at 0
  // first close to the limit so the position is KNOWN
  m.apply(Value::makeInt(0), 100);
  TEST_ASSERT_FALSE(fh->high(CLOSE));  // already at the closed limit: no motion
  TEST_ASSERT_EQUAL(ApplyResult::Unsupported, m.apply(Value::makeInt(50), 200));  // times unknown yet
  TEST_ASSERT_EQUAL(ApplyResult::Ok, m.apply(Value::makeInt(100), 1000));
  TEST_ASSERT_TRUE(fh->high(OPEN));
  fh->level[LIM_CLOSED] = true;
  uint32_t t = run(m, 1000, 12000);
  TEST_ASSERT_TRUE(fh->high(OPEN));  // keeps running until the switch, whatever the estimate says
  fh->level[LIM_OPEN] = false;       // open limit reached
  m.tick(t);
  TEST_ASSERT_FALSE(fh->high(OPEN));
  Value v;
  TEST_ASSERT_TRUE(m.pollChanged(v));
  TEST_ASSERT_EQUAL(100, v.i);
  TEST_ASSERT_UINT32_WITHIN(60, 12000, m.controller().config().openMs);  // learned ~12 s
  TEST_ASSERT_EQUAL(0, violations);
}

void test_max_run_time_stops_a_motor_that_never_reaches_the_limit() {
  CurtainMotor m = CurtainMotor(OPEN, CLOSE).limitSwitches(LIM_OPEN, LIM_CLOSED).maxRun(5000);
  start(m, 0);
  fh->level[LIM_OPEN] = true;
  fh->level[LIM_CLOSED] = true;  // limit switches never fire (broken)
  m.apply(Value::makeInt(100), 1000);
  TEST_ASSERT_TRUE(fh->high(OPEN));
  run(m, 1000, 4900);
  TEST_ASSERT_TRUE(fh->high(OPEN));
  run(m, 5900, 200);
  TEST_ASSERT_FALSE(fh->high(OPEN));
  TEST_ASSERT_TRUE(m.controller().faultTimeout());
}

void test_max_run_time_applies_to_calibrated_motors_too() {
  CurtainMotor m = CurtainMotor(OPEN, CLOSE).travelTime(10000, 10000).maxRun(4000);  // max shorter than the travel
  start(m, 0);
  m.apply(Value::makeInt(100), 0);
  run(m, 0, 4200);
  TEST_ASSERT_FALSE(fh->high(OPEN));
}

void test_default_max_run_is_150_percent_of_travel() {
  CurtainMotor m = CurtainMotor(OPEN, CLOSE).travelTime(10000, 8000);
  start(m, 0);
  TEST_ASSERT_EQUAL_UINT32(15000, m.controller().config().maxRunMs);
}

void test_new_target_while_moving_the_same_way_retargets_without_stopping() {
  CurtainMotor m = calibrated();
  start(m, 0);
  m.apply(Value::makeInt(30), 0);
  uint32_t t = run(m, 0, 1000);
  size_t w = fh->writes.size();
  m.apply(Value::makeInt(80), t);
  TEST_ASSERT_EQUAL(w, fh->writes.size());  // no pin change, still opening
  TEST_ASSERT_TRUE(fh->high(OPEN));
  run(m, t, 8000);
  Value v;
  TEST_ASSERT_TRUE(m.pollChanged(v));
  TEST_ASSERT_INT_WITHIN(2, 80, v.i);
}

void test_unsupported_midway_target_without_calibration_is_an_error_code() {
  EdgeNode node;
  node.endpoint("c1", COVER).intCap("position", 0, 100, CurtainMotor(OPEN, CLOSE).limitSwitches(LIM_OPEN, LIM_CLOSED).maxRun(20000)).bootValue(0).settleS(30);
  TEST_ASSERT_TRUE(node.core().begin(0));
  fh->level[LIM_OPEN] = true;
  fh->level[LIM_CLOSED] = true;
  JsonDocument a = parse(send(node.core(), cmdSet(1, "c1", "{\"position\":40}"), 100).ack);
  TEST_ASSERT_FALSE(a["ok"].as<bool>());
  TEST_ASSERT_EQUAL_STRING("unsupported", a["error"].as<const char*>());
  TEST_ASSERT_FALSE(fh->high(OPEN));
  TEST_ASSERT_FALSE(fh->high(CLOSE));
}

void test_declaration_errors() {
  std::string err;
  TEST_ASSERT_FALSE(CurtainMotor(OPEN, CLOSE).validate(err));                                   // no times, no limits
  TEST_ASSERT_FALSE(CurtainMotor(OPEN, OPEN).travelTime(1000, 1000).validate(err));              // same pin
  TEST_ASSERT_FALSE(CurtainMotor(6, 7).travelTime(1000, 1000).validate(err));                    // flash pins
  TEST_ASSERT_FALSE(CurtainMotor(OPEN, CLOSE).limitSwitches(LIM_OPEN, LIM_OPEN).maxRun(1000).validate(err));
  TEST_ASSERT_FALSE(CurtainMotor(OPEN, CLOSE).limitSwitches(LIM_OPEN, LIM_CLOSED).validate(err));  // limits but no max run
  TEST_ASSERT_TRUE(CurtainMotor(OPEN, CLOSE).travelTime(1000, 1000).validate(err));
}

void test_position_reported_through_the_node_state() {
  EdgeNode node;
  node.endpoint("c1", COVER).intCap("position", 0, 100, calibrated()).bootValue(0).settleS(30);
  TEST_ASSERT_TRUE(node.core().begin(0));
  send(node.core(), cmdSet(1, "c1", "{\"position\":100}"), 100);
  for (uint32_t t = 100; t < 11000; t += 20) node.core().tick(t, true);
  TEST_ASSERT_EQUAL(100, node.core().value("c1", "position")->i);
  TEST_ASSERT_FALSE(fh->high(OPEN));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_outputs_idle_and_safe_at_begin_and_boot_does_not_move);
  RUN_TEST(test_open_runs_for_the_calibrated_time_and_reports_position);
  RUN_TEST(test_reversal_waits_the_dead_time_with_both_outputs_off);
  RUN_TEST(test_interlock_holds_under_random_commands);
  RUN_TEST(test_active_low_wiring_is_respected);
  RUN_TEST(test_limit_switch_stops_the_motor_and_learns_the_travel_time);
  RUN_TEST(test_max_run_time_stops_a_motor_that_never_reaches_the_limit);
  RUN_TEST(test_max_run_time_applies_to_calibrated_motors_too);
  RUN_TEST(test_default_max_run_is_150_percent_of_travel);
  RUN_TEST(test_new_target_while_moving_the_same_way_retargets_without_stopping);
  RUN_TEST(test_unsupported_midway_target_without_calibration_is_an_error_code);
  RUN_TEST(test_declaration_errors);
  RUN_TEST(test_position_reported_through_the_node_state);
  return UNITY_END();
}
