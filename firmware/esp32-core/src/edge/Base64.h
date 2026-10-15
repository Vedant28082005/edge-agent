// Minimal standard base64 (RFC 4648, with padding) for the BLE session JSON.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string>

namespace edge {

inline std::string b64encode(const uint8_t* d, size_t n) {
  static const char* t = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string o;
  for (size_t i = 0; i < n; i += 3) {
    uint32_t v = (uint32_t)d[i] << 16 | (i + 1 < n ? (uint32_t)d[i + 1] << 8 : 0) | (i + 2 < n ? d[i + 2] : 0);
    o += t[v >> 18 & 63];
    o += t[v >> 12 & 63];
    o += i + 1 < n ? t[v >> 6 & 63] : '=';
    o += i + 2 < n ? t[v & 63] : '=';
  }
  return o;
}

// Strict: length multiple of 4, canonical padding, alphabet only. Returns false on any violation.
inline bool b64decode(const std::string& s, std::string& out) {
  out.clear();
  if (s.size() % 4 != 0) return false;
  auto val = [](char c) -> int {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
  };
  for (size_t i = 0; i < s.size(); i += 4) {
    int a = val(s[i]), b = val(s[i + 1]);
    if (a < 0 || b < 0) return false;
    bool last = i + 4 == s.size();
    int c = s[i + 2] == '=' ? -2 : val(s[i + 2]);
    int d = s[i + 3] == '=' ? -2 : val(s[i + 3]);
    if (c == -1 || d == -1) return false;
    if ((c == -2 || d == -2) && !last) return false;
    if (c == -2 && d != -2) return false;
    uint32_t v = (uint32_t)a << 18 | (uint32_t)b << 12 | (c >= 0 ? (uint32_t)c << 6 : 0) | (d >= 0 ? (uint32_t)d : 0);
    out += (char)(v >> 16 & 255);
    if (c >= 0) out += (char)(v >> 8 & 255);
    if (d >= 0) out += (char)(v & 255);
  }
  return true;
}

}  // namespace edge
