// Shared helpers for the host tests.
#pragma once
#include <ArduinoJson.h>
#include <stdio.h>
#include <string.h>
#include <string>
#include <unity.h>
#include <EdgeNode.h>
#include <edge/FakeHal.h>

using namespace edge;

struct FakeSensor : public SensorDriver {
  Value v;
  bool ok = true;
  uint32_t poll = 0;
  explicit FakeSensor(Value val = Value::makeFloat(20.0f)) : v(val) {}
  uint32_t pollMs() const override { return poll; }
  bool read(uint32_t, Value& o) override { if (!ok) return false; o = v; return true; }
};

inline std::string uuid(unsigned n) {
  char b[48];
  snprintf(b, sizeof b, "%08x-0000-4000-8000-%012x", n, n);
  return b;
}

inline std::string cmdSet(unsigned n, const char* ep, const char* setJson, const char* by = "user:vedant") {
  return std::string("{\"cmd_id\":\"") + uuid(n) + "\",\"endpoint\":\"" + ep + "\",\"set\":" + setJson + ",\"by\":\"" + by + "\"}";
}

inline std::string cmdRaw(unsigned n, const char* rest) {
  return std::string("{\"cmd_id\":\"") + uuid(n) + "\"," + rest + "}";
}

inline JsonDocument parse(const std::string& s) {
  JsonDocument d;
  DeserializationError e = deserializeJson(d, s);
  TEST_ASSERT_FALSE_MESSAGE(e, s.c_str());
  return d;
}

inline NodeCore::Outcome send(NodeCore& c, const std::string& cmd, uint32_t now) {
  return c.handleCommand(cmd.c_str(), cmd.size(), now);
}

// The fan from the plan: relay power + PWM speed 1..5, and a temperature sensor.
struct FanRig {
  FakeHal hal;
  EdgeNode node;
  explicit FanRig(uint32_t failsafeAfterS = 60, uint32_t maxOnS = 0) {
    setHal(&hal);
    node.endpoint("ch1", FAN)
        .boolCap("power", RelayOut(26)).bootValue(false).failsafeValue(false).maxOnS(maxOnS)
        .intCap("speed", 1, 5, PwmOut(27, 25000)).bootValue(1)
        .localToggle("power", 33)
        .localCycle("speed", 32);
    node.endpoint("t1", SENSOR).floatSensor("temperature", FakeSensor(Value::makeFloat(27.4f)), 30);
    node.failsafeAfter(failsafeAfterS);
  }
  ~FanRig() { setHal(nullptr); }
  NodeCore& core() { return node.core(); }
  void begin(uint32_t now = 1000) {
    bool ok = core().begin(now);
    TEST_ASSERT_TRUE_MESSAGE(ok, node.model().errors().empty() ? "begin failed" : node.model().errors()[0].c_str());
  }
};
