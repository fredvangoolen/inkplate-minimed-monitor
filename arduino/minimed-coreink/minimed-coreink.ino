// Minimed monitor, M5Stack Core Ink, Arduino port
//
// A port of main_m5coreink.py. Board-independent logic is duplicated across
// the two, not shared, and the hardware facts both depend on live in
// PORTING-M5COREINK.md - read that before changing timing, power, panel or
// wake behaviour. Nothing here is a guess; every constant with a number in
// it was measured on this board.
//
// PHASES 0-2: power hold, config, WiFi, fetch, clock, parse, and the main
// screen. Still to come: deep-sleep scheduling and the toggle (3), the other
// three screens and alarms (4), the AP config portal (5).
//
// The power-hold gate (see below) is settled: verified on battery that a
// software reset recovers by itself, so a crash at 3am does not leave a
// dead monitor.

#include <M5Unified.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <WiFi.h>
#include <time.h>
#include "driver/gpio.h"
#include "esp_timer.h"
#include "esp_sleep.h"
#include "types.h"
#include "screens.h"

#define VERSION "0.1-arduino"

// ---------------------------------------------------------------- hardware

static const gpio_num_t POWER_HOLD_PIN = GPIO_NUM_12;

// The board's 3.3V rail is latched on by GPIO12. Out of reset that pad is an
// input, and the rail coasts only briefly with it floating - so whoever
// takes the latch must do it fast or, on battery, the board simply switches
// off. M5.begin() is far too late; a global constructor runs before
// app_main() and is the earliest hook a sketch can reach.
//
// Measured wall time from reset to this assert: 147ms on a software reset
// (the crash case), 238ms on a cold boot - the difference is the stock
// bootloader validating the image, which our patched MicroPython firmware
// skips. Verified on battery: a restart loop keeps running, so 147ms is
// inside the rail's coast.
static int64_t power_hold_us = -1;

static void assert_power_hold() {
  gpio_config_t cfg = {};
  cfg.pin_bit_mask = 1ULL << POWER_HOLD_PIN;
  cfg.mode = GPIO_MODE_OUTPUT;
  cfg.pull_up_en = GPIO_PULLUP_DISABLE;
  cfg.pull_down_en = GPIO_PULLDOWN_DISABLE;
  cfg.intr_type = GPIO_INTR_DISABLE;
  gpio_config(&cfg);
  gpio_set_level(POWER_HOLD_PIN, 1);
  power_hold_us = esp_timer_get_time();
}

__attribute__((constructor)) static void early_power_hold() {
  assert_power_hold();
}

static void release_pad_hold() {
  // Held pads cannot be re-driven, so this must happen before anything
  // reconfigures GPIO.
  gpio_deep_sleep_hold_dis();
  gpio_hold_dis(POWER_HOLD_PIN);
}

// ------------------------------------------------------------------ timing

static const uint32_t POLL_PERIOD_S = 300;   // matches the CGM cadence
static const uint32_t WIFI_TIMEOUT_MS = 25000;
static const uint32_t HTTP_TIMEOUT_MS = 30000;

// Below this, time() has never been set (the clock starts at 1970 on a cold
// boot). 2020-09-13.
static const time_t TIME_VALID_EPOCH = 1600000000;

// ------------------------------------------------------------------ config


static bool config_read(Config &c) {
  Preferences p;
  if (!p.begin("minimed", true)) {   // read-only
    Serial.println("config: no NVS namespace");
    return false;
  }
  c.wifissid  = p.getString("wifissid", "");
  c.wifipass  = p.getString("wifipass", "");
  c.proxyaddr = p.getString("proxyaddr", "");
  c.ntpserver = p.getString("ntpserver", "pool.ntp.org");
  c.patient   = p.getString("patient", "");
  c.proxyport = p.getUShort("proxyport", 8081);
  c.timezone  = p.getInt("timezone", 0);
  p.end();
  // Patient name is deliberately NOT required: the proxy reports firstName,
  // so a device never told a name still shows the right one.
  return c.wifissid.length() && c.wifipass.length() && c.proxyaddr.length();
}

// ------------------------------------------------------------------- state


// --------------------------------------------------------------- http/time

static const char *HTTP_MONTHS = "JanFebMarAprMayJunJulAugSepOctNovDec";

// "Fri, 04 Sep 2026 06:14:14 GMT" -> epoch UTC, or 0.
//
// This is where the clock comes from. RFC 7231 fixes the format and the
// proxy is a Python BaseHTTP server which emits exactly it, but anything
// unexpected returns 0 rather than a guess: a confidently wrong clock would
// silently corrupt "Updated N min ago", the number that tells a caregiver
// whether to trust what is on screen.
static time_t parse_http_date(const String &value) {
  char mon[4] = {0};
  int day, year, hh, mm, ss;
  char wd[8] = {0};
  // e.g. "Fri, 04 Sep 2026 06:14:14 GMT"
  if (sscanf(value.c_str(), "%7[^,], %d %3s %d %d:%d:%d",
             wd, &day, mon, &year, &hh, &mm, &ss) != 7) {
    return 0;
  }
  const char *pos = strstr(HTTP_MONTHS, mon);
  if (!pos) return 0;
  struct tm t = {};
  t.tm_mday = day;
  t.tm_mon  = (pos - HTTP_MONTHS) / 3;
  t.tm_year = year - 1900;
  t.tm_hour = hh;
  t.tm_min  = mm;
  t.tm_sec  = ss;
  // TZ is pinned to UTC in setup(), so mktime() is a UTC conversion. The
  // display applies the configured offset separately, as main.py does.
  return mktime(&t);
}

static void set_clock(time_t epoch) {
  struct timeval tv = { .tv_sec = epoch, .tv_usec = 0 };
  settimeofday(&tv, nullptr);
}

// Raw client rather than HTTPClient, for the same reason main_m5coreink.py
// hand-rolls the request: an unattended monitor needs a hard upper bound on
// how long a cycle can block, and the Date header has to be readable.
static bool http_get(const Config &c, const char *path,
                     String &body, int &status, time_t &server_epoch) {
  status = 0;
  server_epoch = 0;
  WiFiClient client;
  client.setTimeout(HTTP_TIMEOUT_MS / 1000);
  if (!client.connect(c.proxyaddr.c_str(), c.proxyport)) {
    Serial.println("fetch: connect failed");
    return false;
  }
  client.printf("GET /%s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n",
                path, c.proxyaddr.c_str());

  uint32_t deadline = millis() + HTTP_TIMEOUT_MS;
  // Status line
  String line = client.readStringUntil('\n');
  if (sscanf(line.c_str(), "HTTP/%*d.%*d %d", &status) != 1) status = 0;
  // Headers, until the blank line
  while (client.connected() && millis() < deadline) {
    line = client.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) break;
    if (line.startsWith("Date:") || line.startsWith("date:")) {
      server_epoch = parse_http_date(line.substring(5));
    }
  }
  body = "";
  body.reserve(4096);
  while (client.connected() && millis() < deadline) {
    while (client.available()) body += (char)client.read();
    if (!client.available() && !client.connected()) break;
    delay(1);
  }
  while (client.available()) body += (char)client.read();
  client.stop();
  return status == 200;
}

// ----------------------------------------------------------------- polling

// Mirrors handle_pumpdataupdate(). Returns false if there is no usable data;
// the caller draws "no data" rather than treating it as fatal.
static bool fetch_pump_data(const Config &c, State &s) {
  String body;
  int status = 0;
  time_t server_epoch = 0;
  if (!http_get(c, "carelink/nohistory", body, status, server_epoch)) {
    Serial.printf("fetch: status %d\n", status);
    return false;
  }

  // Set the clock BEFORE parsing: alarm handling compares alarm times
  // against now, and on a cold boot this is the first correct clock the
  // device has had.
  if (server_epoch) {
    long drift = (long)(server_epoch - time(nullptr));
    set_clock(server_epoch);
    if (labs(drift) >= 2) Serial.printf("clock corrected from proxy by %+ld s\n", drift);
  }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    Serial.printf("fetch: bad JSON (%s)\n", err.c_str());
    return false;
  }

  s.haveData = doc["conduitInRange"] | false;
  s.haveData = s.haveData && (doc["conduitMedicalDeviceInRange"] | false);

  if (s.haveData) {
    s.batteryPct     = doc["pumpBatteryLevelPercent"] | -1;
    s.reservoirUnits = doc["reservoirRemainingUnits"] | -1.0f;
    s.reservoirPct   = doc["reservoirLevelPercent"]   | -1;
    s.sageHours      = doc["sensorDurationHours"]     | 255;
    strlcpy(s.sensorState, doc["sensorState"] | "", sizeof(s.sensorState));
    s.activeInsulin  = doc["activeInsulin"]["amount"] | -1.0f;
    const char *trend = doc["lastSGTrend"] | "NONE";
    // Trend is only meaningful with auto mode on, same rule as main.py.
    const char *shield = doc["therapyAlgorithmState"]["autoModeShieldState"] | "FEATURE_OFF";
    strlcpy(s.trend, strcmp(shield, "FEATURE_OFF") ? trend : "NONE", sizeof(s.trend));
  }
  strlcpy(s.patient, doc["firstName"] | "", sizeof(s.patient));

  int sg = doc["lastSG"]["sg"] | 0;
  s.sg = sg > 0 ? sg : 0;

  // INTEGER division, not floating point. This bit the MicroPython builds on
  // both boards: the value is Unix ms (~1.79e12) and a float32 mantissa only
  // resolves ~64s there, so int(ms/1000) decoded 11:51:00 as 11:50:56 and
  // reported "5 min ago" for a 4-minute-old reading. C++ doubles would cope,
  // but integer division is what the value actually means.
  int64_t ms = doc["lastConduitUpdateServerDateTime"] | (int64_t)0;
  if (ms > 0) s.lastUpdate = (time_t)(ms / 1000);

  const char *tzname = doc["clientTimeZoneName"] | "";
  s.dstDelta = strcasestr(tzname, "summer") ? 1 : 0;

  // systemStatusMessage is the generic line; a pump delivery banner
  // (suspend / bg-required / etc.) overrides it when both are set.
  const char *sysmsg = doc["systemStatusMessage"] | "";
  if (sysmsg[0] && strcmp(sysmsg, "NO_ERROR_MESSAGE") != 0) {
    strlcpy(s.banner, sysmsg, sizeof(s.banner));
  }
  const char *pump_banner = doc["pumpBannerState"][0]["type"] | "";
  if (pump_banner[0]) {
    strlcpy(s.banner, pump_banner, sizeof(s.banner));
  }
  for (char *p = s.banner; *p; p++) if (*p == '_') *p = ' ';

  s.timeInRange = doc["timeInRange"]      | -1;
  s.aboveHyper  = doc["aboveHyperLimit"]  | -1;
  s.belowHypo   = doc["belowHypoLimit"]   | -1;
  s.averageSG   = doc["averageSG"]        | -1;

  return true;
}

static void dump_state(const State &s, const Config &c) {
  char when[32] = "-";
  if (s.lastUpdate) {
    struct tm tm;
    gmtime_r(&s.lastUpdate, &tm);
    strftime(when, sizeof(when), "%Y-%m-%d %H:%M:%S", &tm);
  }
  Serial.println("---- state ----");
  Serial.printf("  haveData      %d\n", (int)s.haveData);
  Serial.printf("  sg            %d mg/dL   trend %s\n", s.sg, s.trend);
  Serial.printf("  activeInsulin %.1f U\n", s.activeInsulin);
  Serial.printf("  reservoir     %.1f U (%d%%)\n", s.reservoirUnits, s.reservoirPct);
  Serial.printf("  sensor        %d h   state %s\n", s.sageHours, s.sensorState);
  Serial.printf("  pump battery  %d%%\n", s.batteryPct);
  Serial.printf("  patient       %s (config: %s)\n", s.patient, c.patient.c_str());
  Serial.printf("  lastUpdate    %s UTC\n", when);
  Serial.printf("  banner        '%s'\n", s.banner);
  Serial.printf("  dstDelta      %d\n", s.dstDelta);
  Serial.printf("  stats         inRange %d  above %d  below %d  avgSG %d\n",
                s.timeInRange, s.aboveHyper, s.belowHypo, s.averageSG);
  Serial.println("---------------");
}

// ----------------------------------------------------------------- display

// 1 = overwrite the fetched state with synthetic values, to exercise the
// layout deterministically instead of waiting on whatever the pump happens
// to be doing. Leave at 0 for real use.
#define LAYOUT_TEST 0

// 1 = dump the composed canvas to serial as ASCII, 4x4 pixels per character.
// This board has no REPL and no screenshots, so without it the only way to
// check a layout is to ask a human to look at the panel. Costs nothing when
// off.
#define DEBUG_ASCII 0

#if DEBUG_ASCII
// The MicroPython build's layout constants were tuned against ITS font
// metrics, recorded in comments there (DejaVu72 50px, DejaVu24 26px,
// DejaVu18 20px, DejaVu12 16px, DejaVu40 at 2x measuring 86px tall and 140px
// wide for three digits). If M5GFX reports different numbers to C++, every
// screen coordinate derived from them moves.
static void dump_font_metrics(M5Canvas &c) {
  struct { const char *name; const lgfx::IFont *f; float size; } fs[] = {
    {"DejaVu72   ", &fonts::DejaVu72, 1.0f},
    {"DejaVu40x2 ", &fonts::DejaVu40, 2.0f},
    {"DejaVu24   ", &fonts::DejaVu24, 1.0f},
    {"DejaVu18   ", &fonts::DejaVu18, 1.0f},
    {"DejaVu12   ", &fonts::DejaVu12, 1.0f},
  };
  Serial.println("---- font metrics ----");
  for (auto &e : fs) {
    c.setFont(e.f);
    c.setTextSize(e.size);
    Serial.printf("  %s height %3d   w(\"160\") %3d   w(\"Suspend\") %3d\n",
                  e.name, c.fontHeight(), c.textWidth("160"), c.textWidth("Suspend"));
  }
  c.setTextSize(1.0f);
  Serial.println("----------------------");
}

static void dump_canvas_ascii(M5Canvas &canvas) {
  Serial.println("---- canvas 200x200, 4x4 px per char ----");
  for (int y = 0; y < PANEL_H; y += 4) {
    char row[PANEL_W / 4 + 1];
    int n = 0;
    for (int x = 0; x < PANEL_W; x += 4) {
      // 4bpp greyscale: 0 is black, 15 white. MAJORITY of the 4x4 block, not
      // "any dark pixel": on a reverse-video bar every block contains black,
      // so an any-dark rule renders the whole bar solid and hides the white
      // text inside it - which had me hunting a drawing bug that was really
      // a bug in this dump.
      int dark = 0;
      for (int dy = 0; dy < 4; dy++)
        for (int dx = 0; dx < 4; dx++)
          if (canvas.readPixel(x + dx, y + dy) < 8) dark++;
      row[n++] = (dark >= 8) ? '#' : '.';
    }
    row[n] = 0;
    Serial.println(row);
  }
  Serial.println("----------------------------------------");
  // Per-row white-pixel counts for the bottom strip. ASCII thresholding is
  // hopeless for 1-2px strokes on a reverse-video bar; a count is not.
  Serial.println("bottom strip, white px per row:");
  for (int y = PANEL_H - 40; y < PANEL_H; y++) {
    int white = 0;
    for (int x = 0; x < PANEL_W; x++) if (canvas.readPixel(x, y) >= 8) white++;
    Serial.printf("  y=%3d  %3d\n", y, white);
  }
}
#endif

// Every screen is composed off-screen and pushed in ONE operation. This is
// not an optimisation: M5GFX drives this e-paper panel on each individual
// drawing call, so a screen built from a few dozen primitives triggers a few
// dozen panel updates. Measured on the MicroPython build, drawing straight
// to the panel took 7.7s for this screen against ~30ms composed.
static void compose(const State &s, const Config &c, bool full_refresh) {
  M5.Display.setEpdMode(full_refresh ? epd_mode_t::epd_quality
                                     : epd_mode_t::epd_fast);
  M5Canvas canvas(&M5.Display);
  canvas.setColorDepth(CANVAS_BPP);
  if (canvas.createSprite(PANEL_W, PANEL_H)) {
    draw_main_screen(canvas, s, c);
#if DEBUG_ASCII
    dump_font_metrics(canvas);
    dump_canvas_ascii(canvas);
#endif
    canvas.pushSprite(0, 0);
    canvas.deleteSprite();
  } else {
    // Should not happen here - this build has ~275KB free where MicroPython
    // had a ~55KB largest block - but a slow screen beats a blank one.
    Serial.println("canvas allocation failed, drawing direct");
    draw_main_screen(M5.Display, s, c);
  }
}

// -------------------------------------------------------------------- main

static void sleep_now(uint32_t seconds) {
  M5.Display.powerSaveOn();
  gpio_hold_en(POWER_HOLD_PIN);
  gpio_deep_sleep_hold_en();
  Serial.printf("sleeping %u s (awake %d ms)\n",
                seconds, (int)(esp_timer_get_time() / 1000));
  Serial.flush();
  esp_deep_sleep((uint64_t)seconds * 1000000ULL);
}

void setup() {
  release_pad_hold();
  Serial.begin(115200);

  // Pin the runtime to UTC so mktime()/localtime() are UTC conversions; the
  // configured offset is applied only when drawing, exactly as main.py does.
  setenv("TZ", "UTC0", 1);
  tzset();

  Serial.printf("\ncoreink: power hold asserted at %lld us app-time, reset reason %d\n",
                power_hold_us, (int)esp_reset_reason());

  auto mcfg = M5.config();
  mcfg.clear_display = false;
  M5.begin(mcfg);
  M5.Display.powerSaveOff();

  Config cfg;
  if (!config_read(cfg)) {
    Serial.println("config incomplete - AP setup is phase 5, halting here");
    sleep_now(POLL_PERIOD_S);
  }
  Serial.printf("config: ssid %s proxy %s:%u tz %d patient '%s'\n",
                cfg.wifissid.c_str(), cfg.proxyaddr.c_str(), cfg.proxyport,
                cfg.timezone, cfg.patient.c_str());

  int64_t t_wifi = esp_timer_get_time();
  WiFi.mode(WIFI_STA);
  WiFi.begin(cfg.wifissid.c_str(), cfg.wifipass.c_str());
  uint32_t deadline = millis() + WIFI_TIMEOUT_MS;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) delay(50);
  int64_t wifi_ms = (esp_timer_get_time() - t_wifi) / 1000;

  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("wifi: failed after %lld ms, skipping this cycle\n", wifi_ms);
    sleep_now(POLL_PERIOD_S);
  }
  Serial.printf("wifi: connected in %lld ms, ip %s\n",
                wifi_ms, WiFi.localIP().toString().c_str());

  State s;
  int64_t t_fetch = esp_timer_get_time();
  bool ok = fetch_pump_data(cfg, s);
  int64_t fetch_ms = (esp_timer_get_time() - t_fetch) / 1000;
  Serial.printf("fetch+parse: %lld ms, ok=%d\n", fetch_ms, (int)ok);

  // The radio holds heap the canvas wants and costs current; down it goes
  // before drawing, same as the MicroPython build.
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);

#if LAYOUT_TEST
  // Synthetic: a normal in-range reading with a rising trend, so the large
  // figure, the arrows and both bottom rows are all exercised.
  s.sg = 160; strlcpy(s.trend, "UP_DOUBLE", sizeof(s.trend));
  s.activeInsulin = 0.4f;
  s.lastUpdate = time(nullptr) - 4 * 60;
  s.dstDelta = 1;
  Serial.println("LAYOUT_TEST: state overridden");
#endif
  if (ok) dump_state(s, cfg);
  int64_t t_draw = esp_timer_get_time();
  compose(s, cfg, true);   // phase 2: always full refresh; scheduling is phase 3
  Serial.printf("draw: %lld ms\n", (esp_timer_get_time() - t_draw) / 1000);
  Serial.printf("drawn at %d ms\n", (int)(esp_timer_get_time() / 1000));

  sleep_now(POLL_PERIOD_S);
}

void loop() {
  // Never reached: esp_deep_sleep() does not return.
}
