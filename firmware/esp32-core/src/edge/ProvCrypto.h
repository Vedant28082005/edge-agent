// Crypto primitives the BLE provisioning core needs (contracts/ble-provisioning.md: X25519, HKDF-SHA256,
// AES-256-GCM). The ESP32 implementation (mbedtls) is in BleProvEsp.cpp; host tests use a fake.
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace edge {

class ProvCrypto {
 public:
  virtual ~ProvCrypto() {}
  virtual void random(uint8_t* out, size_t n) = 0;  // hardware RNG on the board
  virtual bool x25519Keypair(uint8_t priv[32], uint8_t pub[32]) = 0;
  virtual bool x25519(const uint8_t priv[32], const uint8_t peerPub[32], uint8_t out[32]) = 0;
  virtual bool hkdfSha256(const uint8_t* ikm, size_t ikmLen, const uint8_t* salt, size_t saltLen, const uint8_t* info,
                          size_t infoLen, uint8_t* out, size_t outLen) = 0;
  // false = authentication (tag) failure.
  virtual bool aesGcmDecrypt(const uint8_t key[32], const uint8_t nonce[12], const uint8_t* aad, size_t aadLen,
                             const uint8_t* ct, size_t ctLen, const uint8_t tag[16], uint8_t* out) = 0;
};

}  // namespace edge
