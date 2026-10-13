// DHT11/DHT22 temperature + humidity. One physical read feeds two capabilities.
//   DhtSensor dht(4, 22);
//   .floatSensor("temperature", dht.temperature(), 30).floatSensor("humidity", dht.humidity(), 30)
#pragma once
#include <functional>
#include <memory>
#include "../Drivers.h"

namespace edge {

const int kDht11 = 11;
const int kDht22 = 22;

namespace detail {
struct DhtCore {
  int pin = -1;
  int type = kDht22;
  uint32_t lastMs = 0;
  bool haveLast = false;
  bool lastOk = false;
  float t = 0, h = 0;
  void* impl = nullptr;                          // DHT* on the board, created lazily
  std::function<bool(float&, float&)> reader;  // injected in tests; real reader is in Dht.cpp
  bool refresh(uint32_t now);
};
}  // namespace detail

class DhtSensor {
 public:
  DhtSensor(int pin, int type = kDht22) : c_(std::make_shared<detail::DhtCore>()) { c_->pin = pin; c_->type = type; }
  class Channel : public SensorDriver {
   public:
    Channel(std::shared_ptr<detail::DhtCore> c, bool humidity) : c_(std::move(c)), hum_(humidity) {}
    bool validate(std::string& err) const override {
      if (!pinValidInput(c_->pin) || !pinValidOutput(c_->pin)) { err = "DhtSensor: pin must be a bidirectional GPIO (not 6-11, not 34-39)"; return false; }
      if (c_->type != kDht11 && c_->type != kDht22) { err = "DhtSensor: type must be 11 or 22"; return false; }
      return true;
    }
    bool read(uint32_t now, Value& out) override {
      if (!c_->refresh(now)) return false;
      out = Value::makeFloat(hum_ ? c_->h : c_->t);
      return true;
    }
    std::shared_ptr<detail::DhtCore> core() const { return c_; }
   private:
    std::shared_ptr<detail::DhtCore> c_;
    bool hum_;
  };
  Channel temperature() const { return Channel(c_, false); }
  Channel humidity() const { return Channel(c_, true); }
  void setReaderForTest(std::function<bool(float&, float&)> r) { c_->reader = std::move(r); }

 private:
  std::shared_ptr<detail::DhtCore> c_;
};

}  // namespace edge
