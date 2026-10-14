#include "Dht.h"
#ifdef ARDUINO
#include <DHT.h>
#endif

namespace edge {
namespace detail {

bool DhtCore::refresh(uint32_t now) {
  // The sensor cannot be read more than once every ~2 s; both channels share this read.
  if (haveLast && elapsedMs(now, lastMs) < 2000) return lastOk;
  haveLast = true;
  lastMs = now;
  float tt = 0, hh = 0;
  bool ok = false;
  if (reader) {
    ok = reader(tt, hh);
  } else {
#ifdef ARDUINO
    if (!impl) { DHT* d = new DHT((uint8_t)pin, (uint8_t)type); d->begin(); impl = d; }
    DHT* dht = static_cast<DHT*>(impl);
    tt = dht->readTemperature();
    hh = dht->readHumidity();
    ok = !isnan(tt) && !isnan(hh);
#endif
  }
  lastOk = ok && !isnan(tt) && !isnan(hh);
  if (lastOk) { t = tt; h = hh; }
  return lastOk;
}

}  // namespace detail
}  // namespace edge
