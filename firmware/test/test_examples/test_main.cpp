// Builds every example sketch's declaration on the host, checks it is valid, and writes the JSON the node
// would publish (announce, state, a good ack, a bad ack, the command used) to $EDGE_CONTRACT_OUT.
// firmware/tools/check_contracts.py then validates those files against contracts/*.schema.json.
#include "../common.h"
#include <stdlib.h>
#include <map>

namespace ex_light {
#include "../../examples/light/main.cpp"
}
namespace ex_fan {
#include "../../examples/fan/main.cpp"
}
namespace ex_curtain {
#include "../../examples/curtain/main.cpp"
}
namespace ex_dht {
#include "../../examples/dht/main.cpp"
}
namespace ex_relay4 {
#include "../../examples/relay4/main.cpp"
}
namespace ex_sensors {
#include "../../examples/sensors/main.cpp"
}

void setUp() {}
void tearDown() { setHal(nullptr); }

static const char* kNodeId = "esp32-a1b2c3d4e5f6";

static void writeFile(const char* name, const char* kind, const std::string& body) {
  const char* dir = getenv("EDGE_CONTRACT_OUT");
  if (!dir || !*dir) return;
  std::string path = std::string(dir) + "/" + name + "." + kind + ".json";
  FILE* f = fopen(path.c_str(), "wb");
  TEST_ASSERT_NOT_NULL_MESSAGE(f, path.c_str());
  fwrite(body.data(), 1, body.size(), f);
  fclose(f);
}

static Value sampleFor(const CapDef& c) {
  switch (c.type) {
    case CapType::Bool: return Value::makeBool(true);
    case CapType::Int: return Value::makeInt(42);
    case CapType::Float: return Value::makeFloat(21.5f);
    default: return Value::makeEnum(1);
  }
}

struct Snapshot {
  std::string announce;
  std::map<int, bool> level;  // pin levels right after begin()
};
static std::map<std::string, Snapshot> snaps;

static void dump(const char* name, EdgeNode& node, void (*declare)()) {
  FakeHal hal;
  setHal(&hal);
  declare();
  NodeModel& m = node.model();
  bool ok = node.core().begin(1000);
  if (!ok) for (auto& e : m.errors()) TEST_MESSAGE(e.c_str());
  TEST_ASSERT_TRUE_MESSAGE(ok, name);
  for (size_t k = 0; k < node.core().sensorCount(); k++) {
    // find the sensor cap type to submit a believable reading
    size_t seen = 0;
    for (auto& ep : m.endpoints)
      for (auto& c : ep->caps)
        if (c.kind == CapKind::Sensor && seen++ == k) node.core().submitSensor(k, sampleFor(c), 1000);
  }
  std::string ann = node.announce(kNodeId);
  TEST_ASSERT_TRUE(ann.size() > 50 && ann.size() < 16384);
  writeFile(name, "announce", ann);
  snaps[name].announce = ann;
  snaps[name].level = hal.level;

  std::string st;
  TEST_ASSERT_TRUE(node.core().stateJson(1760000000, st));
  writeFile(name, "state", st);

  // a valid command for the first actuator capability (its own boot value), and one for a missing endpoint
  for (auto& ep : m.endpoints)
    for (auto& c : ep->caps) {
      if (c.kind != CapKind::Actuator) continue;
      JsonDocument v;
      switch (c.type) {
        case CapType::Bool: v["x"] = true; break;
        case CapType::Int: v["x"] = (int)c.boot.i; break;
        case CapType::Float: v["x"] = c.boot.f; break;
        default: v["x"] = c.values[(size_t)c.boot.i]; break;
      }
      std::string set;
      JsonDocument s;
      s[c.id] = v["x"];
      serializeJson(s, set);
      std::string good = cmdSet(7, ep->localId.c_str(), set.c_str());
      writeFile(name, "cmd", good);
      auto o = send(node.core(), good, 2000);
      TEST_ASSERT_TRUE(o.hasAck);
      TEST_ASSERT_TRUE_MESSAGE(parse(o.ack)["ok"].as<bool>(), name);
      writeFile(name, "ack-ok", o.ack);
      goto bad;
    }
bad:
  {
    auto o = send(node.core(), cmdSet(8, "nope", "{\"power\":true}"), 2001);
    TEST_ASSERT_FALSE(parse(o.ack)["ok"].as<bool>());
    writeFile(name, "ack-err", o.ack);
    auto id = send(node.core(), cmdRaw(9, "\"cmd\":\"identify\",\"duration_s\":5"), 2002);
    writeFile(name, "ack-identify", id.ack);
  }
  setHal(nullptr);
}

void test_light() { dump("light", ex_light::node, ex_light::declareDevice); }
void test_fan() { dump("fan", ex_fan::node, ex_fan::declareDevice); }
void test_curtain() { dump("curtain", ex_curtain::node, ex_curtain::declareDevice); }
void test_dht() {
  // the real reader needs the sensor; give the shared core a fake one for the host
  dump("dht", ex_dht::node, ex_dht::declareDevice);
}
void test_relay4() { dump("relay4", ex_relay4::node, ex_relay4::declareDevice); }
void test_sensors() { dump("sensors", ex_sensors::node, ex_sensors::declareDevice); }

void test_four_relay_example_matches_the_documented_behavior() {
  const Snapshot& sn = snaps["relay4"];
  JsonDocument a = parse(sn.announce);
  TEST_ASSERT_EQUAL(4, (int)a["endpoints"].size());
  TEST_ASSERT_EQUAL(3600, a["endpoints"][0]["capabilities"][0]["max_on_s"].as<int>());
  TEST_ASSERT_TRUE(a["endpoints"][2]["capabilities"][0]["boot_value"].as<bool>());
  TEST_ASSERT_FALSE(sn.level.at(18));  // r3 is on at boot (active low)
  TEST_ASSERT_TRUE(sn.level.at(16));   // r1 is off
}

void test_fan_announce_has_the_speed_range() {
  JsonDocument a = parse(snaps["fan"].announce);
  JsonObject speed = a["endpoints"][0]["capabilities"][1];
  TEST_ASSERT_EQUAL_STRING("speed", speed["id"].as<const char*>());
  TEST_ASSERT_EQUAL(1, speed["min"].as<int>());
  TEST_ASSERT_EQUAL(5, speed["max"].as<int>());
  TEST_ASSERT_EQUAL_STRING("fan", a["endpoints"][0]["type"].as<const char*>());
  TEST_ASSERT_EQUAL(2, a["schema"].as<int>());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_light);
  RUN_TEST(test_fan);
  RUN_TEST(test_curtain);
  RUN_TEST(test_dht);
  RUN_TEST(test_relay4);
  RUN_TEST(test_sensors);
  RUN_TEST(test_four_relay_example_matches_the_documented_behavior);
  RUN_TEST(test_fan_announce_has_the_speed_range);
  return UNITY_END();
}
