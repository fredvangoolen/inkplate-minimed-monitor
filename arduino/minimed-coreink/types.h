// Shared types for the Core Ink Arduino port.
//
// These live in a header rather than the .ino on purpose: arduino-cli
// auto-generates function prototypes and inserts them ABOVE everything
// defined in the sketch body, so a struct declared in the .ino is not yet
// visible at the point where a prototype mentioning it is injected. Types
// pulled in by #include are.

#pragma once

#include <Arduino.h>
#include <time.h>

struct Config {
  String wifissid, wifipass, proxyaddr, ntpserver, patient;
  uint16_t proxyport = 8081;
  int timezone = 0;
};

// Mirrors new_state() in main_m5coreink.py. Plain values only, so the whole
// struct can later sit in RTC memory across a deep sleep without the JSON
// round-trip the MicroPython build needs.
struct State {
  bool haveData = false;
  int sg = 0;                    // mg/dL, 0 = no reading
  char trend[16] = "NONE";
  float activeInsulin = -1.0f;
  int batteryPct = -1;
  float reservoirUnits = -1.0f;
  int reservoirPct = -1;
  int sageHours = 255;           // 255 = the pump's "no sensor yet" sentinel
  char sensorState[32] = "";
  char patient[32] = "";
  time_t lastUpdate = 0;         // epoch UTC of the reading itself
  char banner[64] = "";
  // Alarm message, and the alarm's OWN occurrence time as LOCAL wall clock
  // (not "now", and not UTC - see get_alarm_text()). Empty/0 = no alarm.
  char alarm_text[80] = "";
  time_t alarm_local = 0;
  char ip[16] = "";
  int dstDelta = 0;
  // 24h distribution, for the stats screen
  int timeInRange = -1, aboveHyper = -1, belowHypo = -1, averageSG = -1;
};
