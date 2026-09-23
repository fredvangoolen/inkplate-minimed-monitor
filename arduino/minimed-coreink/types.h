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
#include <string.h>
#include <stdio.h>

// Up to three WiFi networks: home, a phone hotspot for demos on location,
// and one spare. Slot 0 keeps the original NVS key names (wifissid/wifipass)
// so a device configured before multi-SSID existed needs no reconfiguration.
#define WIFI_SLOTS 3

struct WifiNet {
  String ssid, pass;
  bool usable() const { return ssid.length() && pass.length(); }
};

struct Config {
  WifiNet wifi[WIFI_SLOTS];
  String proxyaddr, ntpserver, patient;
  uint16_t proxyport = 8081;
  int timezone = 0;
};

// Mirrors new_state() in main_m5coreink.py. Plain values only, so the whole
// struct can later sit in RTC memory across a deep sleep without the JSON
// round-trip the MicroPython build needs.
// Deliberately a plain POD with NO default member initializers.
//
// This struct is stored in RTC memory across deep sleep. A member with an
// NSDMI makes the enclosing struct non-trivially-constructible, and the
// compiler then emits a dynamic initializer that runs at startup on EVERY
// boot - including a deep-sleep wake. Observed exactly that: the RtcState
// fields around it (magic, cycle, next_poll) persisted correctly while the
// snapshot was silently re-initialised on every wake, so the secondary
// screens always drew "--". Initialise with state_init() instead.
struct State {
  bool haveData;
  int sg;                    // mg/dL, 0 = no reading
  char trend[16];
  float activeInsulin;
  int batteryPct;
  float reservoirUnits;
  int reservoirPct;
  int sageHours;             // 255 = the pump's "no sensor yet" sentinel
  char sensorState[32];
  char patient[32];
  time_t lastUpdate;         // epoch UTC of the reading itself
  char banner[64];
  // Alarm message, and the alarm's OWN occurrence time as LOCAL wall clock
  // (not "now", and not UTC - see resolve_alarm()). Empty/0 = no alarm.
  char alarm_text[80];
  time_t alarm_local;
  char ip[16];
  char ssid[33];             // the network actually in use, for the info screen
  int dstDelta;
  // 24h distribution, for the stats screen
  int timeInRange, aboveHyper, belowHypo, averageSG;
};

inline void state_init(State &s) {
  memset(&s, 0, sizeof(s));
  snprintf(s.trend, sizeof(s.trend), "NONE");
  s.activeInsulin = -1.0f;
  s.batteryPct = -1;
  s.reservoirUnits = -1.0f;
  s.reservoirPct = -1;
  s.sageHours = 255;
  s.timeInRange = s.aboveHyper = s.belowHypo = s.averageSG = -1;
}
