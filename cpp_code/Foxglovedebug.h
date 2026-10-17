// FoxgloveDebug.h
//
// Tiny telemetry helper for Teensy 4.1 (works on any Arduino core with a Stream).
// Declare topics + fields once, then publish values every loop. A PC-side Python
// bridge (foxglove_bridge.py) decodes the frames and serves them to Foxglove.
//
// Wire protocol (all little-endian):
//   frame   = COBS( type:u8 | body | crc16:u16 ) , 0x00
//   crc16   = CRC-16/CCITT-FALSE over (type | body)
//   DECLARE (type 0x01): topic_id:u8, name_len:u8, name, n_fields:u8,
//                        { field_type:u8, name_len:u8, name } * n_fields
//   DATA    (type 0x02): topic_id:u8, micros:u32, field values in declared order
//   SET     (type 0x10, PC -> MCU): name_len:u8, name, value:f64
//
// Receiving: bind variables / register callbacks with bind() / subscribe(), then call
// fg.update() every loop to process incoming signals from the PC control UI.
//
// Declarations are re-sent every second so the bridge can be started at any time.
// IMPORTANT: don't Serial.print() normal text on the same port you use for this.

#pragma once

#include <Arduino.h>
#include <stdint.h>
#include <string.h>

class FoxgloveDebug {
public:
  enum FieldType : uint8_t {
    BOOL = 0, INT8, UINT8, INT16, UINT16, INT32, UINT32, FLOAT32, FLOAT64
  };

  static constexpr size_t   MAX_TOPICS        = 16;
  static constexpr size_t   MAX_FIELDS        = 16;  // per topic
  static constexpr size_t   MAX_NAME_LEN      = 24;  // topic and field names
  static constexpr uint32_t DECLARE_PERIOD_MS = 1000;
  static constexpr size_t   MAX_SUBS          = 32;  // incoming signal subscriptions

  using Callback = void (*)(double value);

  // ------------------------------------------------------------------ Topic
  class Topic {
  public:
    // Field declaration (chainable). Declare all fields BEFORE publishing.
    Topic& addField(const char* name, FieldType type) {
      if (nFields_ < MAX_FIELDS) {
        strncpy(fieldNames_[nFields_], name, MAX_NAME_LEN);
        fieldNames_[nFields_][MAX_NAME_LEN] = '\0';
        fieldTypes_[nFields_] = type;
        nFields_++;
      }
      return *this;
    }
    Topic& addBool(const char* n)   { return addField(n, BOOL); }
    Topic& addInt8(const char* n)   { return addField(n, INT8); }
    Topic& addUInt8(const char* n)  { return addField(n, UINT8); }
    Topic& addInt16(const char* n)  { return addField(n, INT16); }
    Topic& addUInt16(const char* n) { return addField(n, UINT16); }
    Topic& addInt32(const char* n)  { return addField(n, INT32); }
    Topic& addUInt32(const char* n) { return addField(n, UINT32); }
    Topic& addFloat(const char* n)  { return addField(n, FLOAT32); }
    Topic& addDouble(const char* n) { return addField(n, FLOAT64); }

    // Publish one sample. Pass one value per declared field, in order.
    // Values are converted to the declared field type.
    //   imu.publish(ax, ay, az);
    template <typename... Args>
    bool publish(Args... args) {
      static_assert(sizeof...(Args) > 0, "publish() needs at least one value");
      const double vals[sizeof...(Args)] = {static_cast<double>(args)...};
      return publishArray(vals, sizeof...(Args));
    }

    bool publishArray(const double* values, size_t n);

  private:
    friend class FoxgloveDebug;
    FoxgloveDebug* owner_ = nullptr;
    uint8_t  id_ = 0;
    char     name_[MAX_NAME_LEN + 1] = {0};
    uint8_t  nFields_ = 0;
    char     fieldNames_[MAX_FIELDS][MAX_NAME_LEN + 1] = {{0}};
    FieldType fieldTypes_[MAX_FIELDS] = {BOOL};
  };

  // -------------------------------------------------------------- Main class

  // Call in setup() after all topics are declared. Baud is ignored on USB serial.
  void begin(Stream& stream) {
    stream_ = &stream;
    declareAll();
  }

  // Get (or create) a topic by name, e.g. "/imu". Returns a reference so you can chain
  // field declarations. If the topic table is full, a no-op dummy topic is returned.
  Topic& topic(const char* name) {
    for (size_t i = 0; i < nTopics_; i++) {
      if (strncmp(topics_[i].name_, name, MAX_NAME_LEN) == 0) return topics_[i];
    }
    if (nTopics_ >= MAX_TOPICS) return dummy_;
    Topic& t = topics_[nTopics_];
    t.owner_ = this;
    t.id_ = nTopics_;
    strncpy(t.name_, name, MAX_NAME_LEN);
    t.name_[MAX_NAME_LEN] = '\0';
    nTopics_++;
    return t;
  }

  // Send all topic declarations now (called automatically every second).
  void declareAll() {
    for (size_t i = 0; i < nTopics_; i++) sendDeclare(topics_[i]);
    lastDeclareMs_ = millis();
  }

  // ------------------------------------------------- Incoming signals (PC -> MCU)

  // Run `cb(value)` whenever the PC sends the signal `name`.
  //   fg.subscribe("reset", [](double v) { if (v != 0) doReset(); });
  bool subscribe(const char* name, Callback cb) {
    return cb ? addSub(name, cb, nullptr, nullptr) : false;
  }

  // Keep a variable in sync with the signal `name` (any arithmetic type, incl. bool).
  //   float kp; fg.bind("kp", kp);
  template <typename T>
  bool bind(const char* name, T& variable) {
    return addSub(name, nullptr, &variable, &FoxgloveDebug::setter<T>);
  }

  // Process incoming bytes. Call once per loop(); callbacks run inside this call.
  void update() {
    if (!stream_) return;
    while (stream_->available() > 0) {
      int c = stream_->read();
      if (c < 0) break;
      if (c == 0) {  // frame delimiter
        if (!rxOverflow_ && rxLen_ > 0) handleRx(rxBuf_, rxLen_);
        rxLen_ = 0;
        rxOverflow_ = false;
      } else if (rxLen_ < RX_BUF) {
        rxBuf_[rxLen_++] = static_cast<uint8_t>(c);
      } else {
        rxOverflow_ = true;  // frame too long: discard until the next delimiter
      }
    }
  }

private:
  friend class Topic;

  static constexpr size_t RAW_BUF = 512;
  static constexpr size_t ENC_BUF = RAW_BUF + RAW_BUF / 254 + 4;
  static constexpr size_t RX_BUF  = 64;  // max incoming frame: 1+1+24+8+2 raw bytes + COBS overhead

  enum : uint8_t { MSG_DECLARE = 0x01, MSG_DATA = 0x02, MSG_SET = 0x10 };

  struct Sub {
    char     name[MAX_NAME_LEN + 1];
    Callback cb;                        // either a callback...
    void*    var;                       // ...or a bound variable + its setter
    void   (*setter)(void*, double);
  };

  Stream*  stream_ = nullptr;
  Topic    topics_[MAX_TOPICS];
  Topic    dummy_;              // owner_ == nullptr -> publish() is a no-op
  size_t   nTopics_ = 0;
  uint32_t lastDeclareMs_ = 0;
  uint8_t  raw_[RAW_BUF];
  uint8_t  enc_[ENC_BUF];

  Sub      subs_[MAX_SUBS];
  size_t   nSubs_ = 0;
  uint8_t  rxBuf_[RX_BUF];
  size_t   rxLen_ = 0;
  bool     rxOverflow_ = false;

  template <typename T>
  static void setter(void* p, double v) { *static_cast<T*>(p) = static_cast<T>(v); }

  bool addSub(const char* name, Callback cb, void* var, void (*set)(void*, double)) {
    if (nSubs_ >= MAX_SUBS || !name || strlen(name) == 0 || strlen(name) > MAX_NAME_LEN) return false;
    Sub& s = subs_[nSubs_++];
    strncpy(s.name, name, MAX_NAME_LEN);
    s.name[MAX_NAME_LEN] = '\0';
    s.cb = cb;
    s.var = var;
    s.setter = set;
    return true;
  }

  // Inverse of cobsEncode. Returns decoded length, or 0 on error.
  static size_t cobsDecode(const uint8_t* in, size_t len, uint8_t* out, size_t cap) {
    size_t r = 0, w = 0;
    while (r < len) {
      uint8_t code = in[r++];
      for (uint8_t i = 1; i < code; i++) {
        if (r >= len || w >= cap) return 0;
        out[w++] = in[r++];
      }
      if (code < 0xFF && r < len) {
        if (w >= cap) return 0;
        out[w++] = 0;
      }
    }
    return w;
  }

  void handleRx(const uint8_t* enc, size_t len) {
    uint8_t dec[RX_BUF];
    size_t n = cobsDecode(enc, len, dec, sizeof(dec));
    if (n < 3) return;
    uint16_t crc = static_cast<uint16_t>(dec[n - 2] | (dec[n - 1] << 8));
    n -= 2;
    if (crc16(dec, n) != crc) return;
    if (dec[0] != MSG_SET || n < 2) return;

    size_t nl = dec[1];
    if (nl == 0 || nl > MAX_NAME_LEN || n != 2 + nl + sizeof(double)) return;
    char name[MAX_NAME_LEN + 1];
    memcpy(name, &dec[2], nl);
    name[nl] = '\0';
    double value;
    memcpy(&value, &dec[2 + nl], sizeof(double));

    for (size_t i = 0; i < nSubs_; i++) {
      Sub& s = subs_[i];
      if (strcmp(s.name, name) != 0) continue;
      if (s.setter && s.var) s.setter(s.var, value);
      if (s.cb) s.cb(value);
    }
  }

  static size_t typeSize(FieldType t) {
    switch (t) {
      case BOOL: case INT8: case UINT8: return 1;
      case INT16: case UINT16:          return 2;
      case INT32: case UINT32: case FLOAT32: return 4;
      case FLOAT64:                     return 8;
    }
    return 0;
  }

  static uint16_t crc16(const uint8_t* d, size_t n) {
    uint16_t c = 0xFFFF;
    while (n--) {
      c ^= static_cast<uint16_t>(*d++) << 8;
      for (int i = 0; i < 8; i++) c = (c & 0x8000) ? (c << 1) ^ 0x1021 : (c << 1);
    }
    return c;
  }

  static size_t cobsEncode(const uint8_t* in, size_t len, uint8_t* out) {
    size_t read = 0, write = 1, codeIdx = 0;
    uint8_t code = 1;
    while (read < len) {
      if (in[read] == 0) {
        out[codeIdx] = code;
        code = 1;
        codeIdx = write++;
        read++;
      } else {
        out[write++] = in[read++];
        code++;
        if (code == 0xFF) {
          out[codeIdx] = code;
          code = 1;
          codeIdx = write++;
        }
      }
    }
    out[codeIdx] = code;
    return write;
  }

  // raw_[0..len) already holds type|body. Appends CRC, COBS-encodes, writes the frame.
  bool sendRaw(size_t len) {
    if (!stream_ || len + 2 > RAW_BUF) return false;
    uint16_t crc = crc16(raw_, len);
    raw_[len++] = crc & 0xFF;
    raw_[len++] = crc >> 8;
    size_t n = cobsEncode(raw_, len, enc_);
    enc_[n++] = 0x00;
    stream_->write(enc_, n);
    return true;
  }

  void sendDeclare(const Topic& t) {
    size_t p = 0;
    raw_[p++] = MSG_DECLARE;
    raw_[p++] = t.id_;
    uint8_t nl = strlen(t.name_);
    raw_[p++] = nl;
    memcpy(&raw_[p], t.name_, nl);
    p += nl;
    raw_[p++] = t.nFields_;
    for (uint8_t i = 0; i < t.nFields_; i++) {
      raw_[p++] = t.fieldTypes_[i];
      uint8_t fl = strlen(t.fieldNames_[i]);
      raw_[p++] = fl;
      memcpy(&raw_[p], t.fieldNames_[i], fl);
      p += fl;
    }
    sendRaw(p);
  }

  bool sendData(const Topic& t, const double* v, size_t n) {
    if (n != t.nFields_) return false;
    if (millis() - lastDeclareMs_ >= DECLARE_PERIOD_MS) declareAll();

    size_t p = 0;
    raw_[p++] = MSG_DATA;
    raw_[p++] = t.id_;
    uint32_t us = micros();
    memcpy(&raw_[p], &us, 4);
    p += 4;

    for (size_t i = 0; i < n; i++) {
      size_t sz = typeSize(t.fieldTypes_[i]);
      if (p + sz + 2 > RAW_BUF) return false;
      switch (t.fieldTypes_[i]) {
        case BOOL:    { uint8_t  x = (v[i] != 0.0);      memcpy(&raw_[p], &x, sz); break; }
        case INT8:    { int8_t   x = (int8_t)v[i];       memcpy(&raw_[p], &x, sz); break; }
        case UINT8:   { uint8_t  x = (uint8_t)v[i];      memcpy(&raw_[p], &x, sz); break; }
        case INT16:   { int16_t  x = (int16_t)v[i];      memcpy(&raw_[p], &x, sz); break; }
        case UINT16:  { uint16_t x = (uint16_t)v[i];     memcpy(&raw_[p], &x, sz); break; }
        case INT32:   { int32_t  x = (int32_t)v[i];      memcpy(&raw_[p], &x, sz); break; }
        case UINT32:  { uint32_t x = (uint32_t)v[i];     memcpy(&raw_[p], &x, sz); break; }
        case FLOAT32: { float    x = (float)v[i];        memcpy(&raw_[p], &x, sz); break; }
        case FLOAT64: { double   x = v[i];               memcpy(&raw_[p], &x, sz); break; }
      }
      p += sz;
    }
    return sendRaw(p);
  }
};

inline bool FoxgloveDebug::Topic::publishArray(const double* values, size_t n) {
  if (!owner_) return false;
  return owner_->sendData(*this, values, n);
}