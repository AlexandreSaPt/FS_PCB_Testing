// main.cpp - example usage of FoxgloveDebug on a Teensy 4.1
//
// PlatformIO: put this in src/ together with FoxgloveDebug.h (board = teensy41).
// Arduino IDE: rename to <sketch>.ino.
//
// Publishes: /imu, /motor, /stats      (MCU -> Foxglove)
// Receives:  kp, target_rpm, enable, reset   (control UI -> MCU)

#include <Arduino.h>
#include "FoxgloveDebug.h"

FoxgloveDebug fg;

// ---- Outgoing topics: declare once, publish every loop. Names show up as "/imu.ax" etc.
FoxgloveDebug::Topic& imu = fg.topic("/imu")
                                .addFloat("ax")
                                .addFloat("ay")
                                .addFloat("az");

FoxgloveDebug::Topic& motor = fg.topic("/motor")
                                  .addFloat("rpm")
                                  .addFloat("target_rpm")
                                  .addBool("enabled");

FoxgloveDebug::Topic& stats = fg.topic("/stats")
                                  .addUInt32("loop_count")
                                  .addUInt32("loop_time_us");

// ---- Variables controlled from the PC UI
static float kp           = 2.0f;
static float targetRpm    = 0.0f;
static bool  motorEnabled = false;

// ---- Simulated motor state
static float    rpm       = 0.0f;
static uint32_t loopCount = 0;

// Callback example: called whenever the "reset" signal arrives
static void onReset(double value) {
  if (value == 0) return;  // ignore the "release" edge of a momentary button
  rpm = 0.0f;
  loopCount = 0;
}

void setup() {
  Serial.begin(115200);  // baud is ignored on Teensy USB serial
  pinMode(LED_BUILTIN, OUTPUT);

  fg.begin(Serial);      // use e.g. Serial1 (after Serial1.begin(921600)) for a hardware UART

  // Incoming signals. Names must match the "signal name" of the control in the UI.
  fg.bind("kp", kp);                    // variable kept in sync automatically
  fg.bind("target_rpm", targetRpm);
  fg.bind("enable", motorEnabled);
  fg.subscribe("reset", onReset);       // or run a function
}

void loop() {
  uint32_t start = micros();
  float t = millis() / 1000.0f;

  fg.update();  // process signals coming from the UI (callbacks run here)

  // Simple first-order "motor": rpm chases the target at a speed set by kp
  const float dt = 0.01f;
  float target = motorEnabled ? targetRpm : 0.0f;
  rpm += kp * (target - rpm) * dt;
  digitalWrite(LED_BUILTIN, motorEnabled);

  // Fake IMU data
  float ax = sinf(t * 2.0f);
  float ay = cosf(t * 2.0f);
  float az = 9.81f + 0.1f * sinf(t * 10.0f);

  // Publish: one value per declared field, in the declared order.
  imu.publish(ax, ay, az);
  motor.publish(rpm, target, motorEnabled);
  stats.publish(loopCount++, micros() - start);

  delay(10);  // ~100 Hz
}