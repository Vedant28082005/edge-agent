// FW_BUILD_TS (Unix seconds, UTC) comes from a header that scripts/fw_build_ts.py regenerates on every
// build, so only this file is recompiled. The host build gets it from a test hook instead.
#include "../EdgeNode.h"
#if defined(__has_include)
#if __has_include("fw_build_ts.h")
#include "fw_build_ts.h"
#endif
#endif

namespace edge {

#ifdef ARDUINO
#ifndef FW_BUILD_TS
#error "FW_BUILD_TS must be defined by the build: add scripts/fw_build_ts.py to extra_scripts"
#endif
static int64_t g_buildTs = (int64_t)FW_BUILD_TS;
#else
static int64_t g_buildTs = 0;
#endif

int64_t fwBuildTs() { return g_buildTs; }
void setFwBuildTsForTest(int64_t ts) { g_buildTs = ts; }

std::string nodeIdFromMac(const uint8_t mac[6]) {
  static const char* hex = "0123456789abcdef";
  std::string s = "esp32-";
  for (int k = 0; k < 6; k++) { s += hex[mac[k] >> 4]; s += hex[mac[k] & 15]; }
  return s;
}

EdgeNode::EdgeNode() : core_(model_) {
#ifdef FW_VERSION
  model_.fw = FW_VERSION;
#endif
}

EndpointBuilder EdgeNode::endpoint(const char* localId, EpType type) {
  auto ep = std::unique_ptr<EndpointDef>(new EndpointDef());
  ep->localId = localId ? localId : "";
  ep->type = type;
  EndpointDef* raw = ep.get();
  model_.endpoints.push_back(std::move(ep));
  return EndpointBuilder(model_, raw);
}

}  // namespace edge
