// Safety timers enforced in firmware: boot_value, failsafe_value, max_on_s, and local buttons offline.
#include "../common.h"

static FanRig* rig = nullptr;
static void make(uint32_t failsafeAfterS = 60, uint32_t maxOnS = 0) {
  delete rig;
  rig = new FanRig(failsafeAfterS, maxOnS);
}
void setUp() { rig = nullptr; }
void tearDown() { delete rig; rig = nullptr; }

static bool power() { return rig->core().value("ch1", "power")->b; }
static void cmdPower(unsigned id, bool on, uint32_t now) {
  send(rig->core(), cmdSet(id, "ch1", on ? "{\"power\":true}" : "{\"power\":false}"), now);
}

// ---- boot_value --------------------------------------------------------------------------------------

void test_boot_value_true_is_applied_in_begin_before_any_tick() {
  FakeHal hal;
  setHal(&hal);
  EdgeNode node;
  node.endpoint("r3", LIGHT).boolCap("power", RelayOut(18, /*activeHigh=*/false)).bootValue(true).failsafeValue(true);
  TEST_ASSERT_TRUE(node.core().begin(0));
  TEST_ASSERT_FALSE(hal.high(18));  // active-low relay is energised = pin low
  TEST_ASSERT_TRUE(node.core().value("r3", "power")->b);
  setHal(nullptr);
}

void test_relay_pin_is_safe_before_it_is_switched() {
  FakeHal hal;
  setHal(&hal);
  EdgeNode node;
  node.endpoint("r1", OUTLET).boolCap("power", RelayOut(16, false)).bootValue(false);
  node.core().begin(0);
  // first event on the pin must be the inactive level (high for active-low), never a glitch to active
  TEST_ASSERT_TRUE(hal.isOutput[16]);
  TEST_ASSERT_TRUE(hal.high(16));
  for (auto& w : hal.writes) TEST_ASSERT_TRUE(w.pin != 16 || w.high);
  setHal(nullptr);
}

// ---- failsafe -------------------------------------------------------------------------------------------

void test_failsafe_fires_exactly_at_threshold_and_once() {
  make(60);
  rig->begin(0);
  cmdPower(1, true, 10);
  rig->core().tick(20, true);  // hub connected
  TEST_ASSERT_TRUE(power());
  rig->core().tick(1000, false);  // connection lost at t=1000
  rig->core().tick(1000 + 59999, false);
  TEST_ASSERT_TRUE(power());
  TEST_ASSERT_FALSE(rig->core().failsafeApplied());
  rig->core().tick(1000 + 60000, false);
  TEST_ASSERT_FALSE(power());
  TEST_ASSERT_FALSE(rig->hal.high(26));
  TEST_ASSERT_TRUE(rig->core().failsafeApplied());
  // one failsafe per outage: turning it on again (command or button) is not undone a second time
  cmdPower(2, true, 1000 + 61000);
  rig->core().tick(1000 + 200000, false);
  TEST_ASSERT_TRUE(power());
}

void test_failsafe_rearms_after_reconnect() {
  make(60);
  rig->begin(0);
  rig->core().tick(10, true);
  rig->core().tick(100, false);
  rig->core().tick(100 + 60000, false);
  TEST_ASSERT_TRUE(rig->core().failsafeApplied());
  rig->core().tick(70000, true);  // hub is back
  TEST_ASSERT_FALSE(rig->core().failsafeApplied());
  cmdPower(2, true, 70100);
  rig->core().tick(80000, false);  // second outage starts
  rig->core().tick(80000 + 59000, false);
  TEST_ASSERT_TRUE(power());
  rig->core().tick(80000 + 60000, false);
  TEST_ASSERT_FALSE(power());
}

void test_failsafe_counts_from_boot_if_hub_never_connects() {
  make(60);
  rig->begin(5000);
  cmdPower(3, true, 5100);  // e.g. arrived before the link dropped, or set locally
  rig->core().tick(5000 + 59999, false);
  TEST_ASSERT_TRUE(power());
  rig->core().tick(5000 + 60000, false);
  TEST_ASSERT_FALSE(power());
}

void test_failsafe_after_zero_keeps_last_values() {
  make(0);
  rig->begin(0);
  cmdPower(4, true, 10);
  rig->core().tick(10 * 3600 * 1000u, false);
  TEST_ASSERT_TRUE(power());
}

void test_failsafe_only_touches_capabilities_that_declare_one() {
  make(60);
  rig->begin(0);
  send(rig->core(), cmdSet(5, "ch1", "{\"speed\":4}"), 10);
  rig->core().tick(100, false);
  rig->core().tick(100 + 60000, false);
  TEST_ASSERT_EQUAL(4, rig->core().value("ch1", "speed")->i);  // speed has no failsafe_value: keep last
}

void test_failsafe_value_true_for_a_light_that_must_stay_lit() {
  FakeHal hal;
  setHal(&hal);
  EdgeNode node;
  node.failsafeAfter(30);
  node.endpoint("r3", LIGHT).boolCap("power", RelayOut(18, false)).bootValue(true).failsafeValue(true);
  node.core().begin(0);
  send(node.core(), cmdSet(6, "r3", "{\"power\":false}"), 10);
  node.core().tick(30010, false);
  TEST_ASSERT_TRUE(node.core().value("r3", "power")->b);
  setHal(nullptr);
}

// ---- max_on_s ---------------------------------------------------------------------------------------------

void test_max_on_forces_off_at_the_limit() {
  make(0, 100);  // 100 s
  rig->begin(0);
  cmdPower(10, true, 1000);
  rig->core().tick(1000 + 99999, true);
  TEST_ASSERT_TRUE(power());
  rig->core().tick(1000 + 100000, true);
  TEST_ASSERT_FALSE(power());
  TEST_ASSERT_FALSE(rig->hal.high(26));
  TEST_ASSERT_TRUE(rig->core().dirty());  // the forced-off is published
}

void test_repeated_on_does_not_restart_the_on_time() {
  make(0, 100);
  rig->begin(0);
  cmdPower(11, true, 1000);
  cmdPower(12, true, 60000);  // a second "on" 59 s later must not extend the on-time
  rig->core().tick(1000 + 100000, true);
  TEST_ASSERT_FALSE(power());
}

void test_off_then_on_restarts_the_on_time() {
  make(0, 100);
  rig->begin(0);
  cmdPower(13, true, 1000);
  cmdPower(14, false, 50000);
  cmdPower(15, true, 60000);
  rig->core().tick(60000 + 99999, true);
  TEST_ASSERT_TRUE(power());
  rig->core().tick(60000 + 100000, true);
  TEST_ASSERT_FALSE(power());
}

void test_max_on_works_offline_and_across_millis_wrap() {
  make(0, 100);
  uint32_t t0 = 0xFFFFFFFFu - 50000u;  // 50 s before the 32-bit millis counter wraps
  rig->begin(t0);
  cmdPower(16, true, t0 + 10);
  rig->core().tick(t0 + 99000, false);  // past the wrap, hub offline
  TEST_ASSERT_TRUE(power());
  rig->core().tick(t0 + 100010, false);
  TEST_ASSERT_FALSE(power());
}

void test_max_on_zero_means_unlimited() {
  make(0, 0);
  rig->begin(0);
  cmdPower(17, true, 10);
  rig->core().tick(0x7FFFFFFFu, true);
  TEST_ASSERT_TRUE(power());
}

// ---- local buttons ------------------------------------------------------------------------------------------

static void press(uint32_t& t, int pin) {
  rig->hal.level[pin] = false;  // pressed (active low)
  rig->core().tick(t += 10, false);
  rig->core().tick(t += 40, false);  // past the debounce time
  rig->hal.level[pin] = true;        // released
  rig->core().tick(t += 10, false);
  rig->core().tick(t += 40, false);
}

void test_local_buttons_work_with_the_hub_offline() {
  make(0);
  rig->begin(0);
  rig->hal.level[33] = true;
  rig->hal.level[32] = true;
  uint32_t t = 100;
  rig->core().tick(t, false);  // learn the idle level
  press(t, 33);
  TEST_ASSERT_TRUE(power());
  TEST_ASSERT_TRUE(rig->hal.high(26));
  press(t, 33);
  TEST_ASSERT_FALSE(power());
  for (int expected : {2, 3, 4, 5, 1, 2}) {  // cycle wraps max -> min
    press(t, 32);
    TEST_ASSERT_EQUAL(expected, rig->core().value("ch1", "speed")->i);
  }
}

void test_a_contact_bounce_does_not_toggle() {
  make(0);
  rig->begin(0);
  rig->hal.level[33] = true;
  rig->core().tick(100, false);
  rig->hal.level[33] = false;
  rig->core().tick(110, false);
  rig->hal.level[33] = true;  // glitch shorter than the debounce time
  rig->core().tick(120, false);
  rig->core().tick(200, false);
  TEST_ASSERT_FALSE(power());
}

void test_holding_a_button_acts_once() {
  make(0);
  rig->begin(0);
  rig->hal.level[33] = true;
  rig->core().tick(100, false);
  rig->hal.level[33] = false;
  for (uint32_t t = 110; t < 5000; t += 20) rig->core().tick(t, false);
  TEST_ASSERT_TRUE(power());
}

void test_local_press_marks_state_dirty_for_publishing() {
  make(0);
  rig->begin(0);
  rig->hal.level[33] = true;
  rig->core().tick(100, false);
  rig->core().takeDirty();
  uint32_t t = 100;
  press(t, 33);
  TEST_ASSERT_TRUE(rig->core().dirty());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_boot_value_true_is_applied_in_begin_before_any_tick);
  RUN_TEST(test_relay_pin_is_safe_before_it_is_switched);
  RUN_TEST(test_failsafe_fires_exactly_at_threshold_and_once);
  RUN_TEST(test_failsafe_rearms_after_reconnect);
  RUN_TEST(test_failsafe_counts_from_boot_if_hub_never_connects);
  RUN_TEST(test_failsafe_after_zero_keeps_last_values);
  RUN_TEST(test_failsafe_only_touches_capabilities_that_declare_one);
  RUN_TEST(test_failsafe_value_true_for_a_light_that_must_stay_lit);
  RUN_TEST(test_max_on_forces_off_at_the_limit);
  RUN_TEST(test_repeated_on_does_not_restart_the_on_time);
  RUN_TEST(test_off_then_on_restarts_the_on_time);
  RUN_TEST(test_max_on_works_offline_and_across_millis_wrap);
  RUN_TEST(test_max_on_zero_means_unlimited);
  RUN_TEST(test_local_buttons_work_with_the_hub_offline);
  RUN_TEST(test_a_contact_bounce_does_not_toggle);
  RUN_TEST(test_holding_a_button_acts_once);
  RUN_TEST(test_local_press_marks_state_dirty_for_publishing);
  return UNITY_END();
}
