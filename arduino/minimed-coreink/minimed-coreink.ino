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
static void compose(int screen, const State &s, const Config &c, bool full_refresh) {
  M5.Display.setEpdMode(full_refresh ? epd_mode_t::epd_quality
                                     : epd_mode_t::epd_fast);
  M5Canvas canvas(&M5.Display);
  canvas.setColorDepth(CANVAS_BPP);
  if (canvas.createSprite(PANEL_W, PANEL_H)) {
    draw_current_screen(canvas, screen, s, c);
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
    draw_current_screen(M5.Display, screen, s, c);
  }
}

// ---------------------------------------------------------------- toggle
//
// Three-position switch: G37 up, G39 down, G38 press (unused). Both
// directions advance a screen, which needs BOTH of the ESP32's GPIO wake
// sources - ext0 takes a single pin, ext1 a mask, and one alone cannot cover
// two pins that must each wake on a low level.

static const gpio_num_t BTN_UP_PIN   = GPIO_NUM_37;
static const gpio_num_t BTN_DOWN_PIN = GPIO_NUM_39;

// How long to stay awake after a press, watching for another. Waking costs
// ~1.2s of boot before any of this runs, so a run of flicks should redraw at
// panel speed rather than pay that each time. 15s is really "how long may
// someone think before the device gives up on them".
static const uint32_t TOGGLE_AWAKE_MS = 15000;

// There is deliberately NO equivalent window after a scheduled refresh. A
// poll runs ~283 times a day whether or not anybody is there; measured on
// the MicroPython build, such a window was over half the awake time in every
// cycle. Nothing is lost but latency on the first flick after a refresh,
// because the toggle still wakes the board out of deep sleep.

// Bounds that exist purely so no switch fault can keep the device awake. A
// held, wedged or chattering contact must degrade to "the screens stop
// responding until the next scheduled wake", never to "the monitor stops
// polling", which is indistinguishable from a dead device.
static const uint32_t TOGGLE_SESSION_MAX_MS = 180000;
static const uint32_t TOGGLE_RELEASE_MAX_MS = 3000;
static const uint32_t TOGGLE_DEBOUNCE_MS    = 120;

// Presses are latched by an interrupt rather than discovered by polling: a
// panel redraw blocks for a noticeable fraction of a second, and a flick
// that landed during one would simply be lost - the device feeling like it
// ignores the switch exactly when someone is clicking through it fastest.
static volatile bool toggle_latch = false;

static void IRAM_ATTR toggle_isr() { toggle_latch = true; }

static void install_toggle_irq() {
  pinMode(BTN_UP_PIN, INPUT);
  pinMode(BTN_DOWN_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(BTN_UP_PIN), toggle_isr, FALLING);
  attachInterrupt(digitalPinToInterrupt(BTN_DOWN_PIN), toggle_isr, FALLING);
}

// Active low; every pin reads 1 at rest, confirmed by probing.
static bool toggle_pressed() {
  return digitalRead(BTN_UP_PIN) == LOW || digitalRead(BTN_DOWN_PIN) == LOW;
}

// True if the switch has been operated since the last call, held or not.
static bool toggle_take() {
  if (!toggle_latch) return false;
  toggle_latch = false;
  return true;
}

static void arm_toggle_wake() {
  // A pin that is ALREADY low must not be armed. These wake sources are
  // level-triggered, not edge-triggered, so arming a pin held down makes
  // deep sleep return immediately, every time: the device would spin through
  // wake/redraw/sleep as fast as it can boot, never reaching its next poll
  // and flattening the battery in hours. That is what a switch resting
  // off-centre, or a contact failed to ground, looks like. Skipping the
  // stuck pin costs that one direction until it is released; the other
  // direction and the timer keep working.
  if (digitalRead(BTN_UP_PIN) == HIGH) {
    esp_sleep_enable_ext0_wakeup(BTN_UP_PIN, 0);
  } else {
    Serial.printf("GPIO%d held low, not arming ext0\n", (int)BTN_UP_PIN);
  }
  if (digitalRead(BTN_DOWN_PIN) == HIGH) {
    esp_sleep_enable_ext1_wakeup(1ULL << BTN_DOWN_PIN, ESP_EXT1_WAKEUP_ALL_LOW);
  } else {
    Serial.printf("GPIO%d held low, not arming ext1\n", (int)BTN_DOWN_PIN);
  }
}

// ------------------------------------------------------------- rtc state
//
// RTC_DATA_ATTR survives deep sleep and is re-initialised on every other
// reset - which is exactly the semantics wanted here, and the same as the
// MicroPython build's RTC memory: a cold boot must start with no snapshot
// rather than a stale one. (It is NOT the place for anything that has to
// outlive a crash; see the reboot counter in the phase 0 notes.)
//
// A plain struct, where MicroPython had to serialise JSON in and out of RTC
// memory and re-hydrate it - about 120 lines that simply do not exist here.
#define RTC_MAGIC 0x4D4D0301u   // bump when the layout changes

struct RtcState {
  uint32_t magic;
  int      screen;
  time_t   next_poll;
  time_t   session_start;   // for the runtime counter on the info screen
  uint32_t cycle;
  State    snap;
};

RTC_DATA_ATTR RtcState rtc;

// How often a scheduled refresh uses the slow full-clear waveform. The flash
// is not required on every wake - the fast waveform writes the same image -
// but ghosting is: fast waveforms leave a residue that accumulates into a
// permanent shadow on a display redrawing the same shapes all day. At one
// poll per 5 minutes, 12 cycles is roughly hourly.
static const uint32_t FULL_REFRESH_EVERY = 12;

// -------------------------------------------------------------------- main

static void sleep_until(time_t next_poll) {
  // Sleep only until the next poll is actually due, rather than a fresh full
  // period. Without this every toggle press would push the next reading out
  // by another 5 minutes, so someone idly flicking through the screens could
  // starve the data this device exists to show.
  int32_t remaining = POLL_PERIOD_S;
  if (next_poll) remaining = (int32_t)(next_poll - time(nullptr));
  if (remaining < 5) remaining = 5;
  else if (remaining > (int32_t)POLL_PERIOD_S) remaining = POLL_PERIOD_S;

  arm_toggle_wake();
  // Panel down, pads latched, then sleep - the order matters; see
  // PORTING-M5COREINK.md.
  M5.Display.powerSaveOn();
  gpio_hold_en(POWER_HOLD_PIN);
  gpio_deep_sleep_hold_en();
  Serial.printf("sleeping %d s (awake %d ms)\n",
                (int)remaining, (int)(esp_timer_get_time() / 1000));
  Serial.flush();
  esp_deep_sleep((uint64_t)remaining * 1000000ULL);
}

// Returns the screen left on display. Each press advances one screen and
// restarts the window, so a conversation with the device never drops back to
// sleep mid-flick.
static int run_toggle_session(int screen, const State &s, const Config &c) {
  install_toggle_irq();
  toggle_take();   // discard the press that woke us; already accounted for
  uint32_t deadline  = millis() + TOGGLE_AWAKE_MS;
  uint32_t hard_stop = millis() + TOGGLE_SESSION_MAX_MS;

  while ((int32_t)(deadline - millis()) > 0 &&
         (int32_t)(hard_stop - millis()) > 0) {
    if (toggle_take() || toggle_pressed()) {
      screen = (screen + 1) % SCREEN_COUNT;

      // Wait for release BEFORE drawing, so holding the switch does not
      // queue a second advance. BOUNDED: a stuck-low pin must not park the
      // device here forever.
      uint32_t release_by = millis() + TOGGLE_RELEASE_MAX_MS;
      while (toggle_pressed() && (int32_t)(release_by - millis()) > 0) delay(10);
      toggle_take();

      compose(screen, s, c, false);

      // Settle time: these contacts bounce, and without it one physical
      // flick can register several times and race through the screens.
      delay(TOGGLE_DEBOUNCE_MS);
      toggle_take();

      if (toggle_pressed()) {
        // Still down: held, resting off-centre, or a failed contact.
        // Advancing on a level rather than an edge would spin through the
        // screens for as long as it stays there, so end the session.
        // arm_toggle_wake() will also decline to arm this pin.
        Serial.printf("toggle still held after %u ms - ending session\n",
                      TOGGLE_RELEASE_MAX_MS);
        break;
      }
      deadline = millis() + TOGGLE_AWAKE_MS;
    }
    delay(10);
  }
  return screen;
}

void setup() {
  release_pad_hold();
  Serial.begin(115200);

  // Pin the runtime to UTC so mktime()/localtime() are UTC conversions; the
  // configured offset is applied only when drawing, as main.py does.
  setenv("TZ", "UTC0", 1);
  tzset();

  esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
  bool woke_on_toggle = (cause == ESP_SLEEP_WAKEUP_EXT0 ||
                         cause == ESP_SLEEP_WAKEUP_EXT1);
  bool cold_boot = (rtc.magic != RTC_MAGIC);
  if (cold_boot) {
    rtc = RtcState{};
    rtc.magic = RTC_MAGIC;
    rtc.screen = SCREEN_MAIN;
  }

  Serial.printf("\ncoreink: power hold at %lld us, reset %d, wake %d, cold %d, cycle %u\n",
                power_hold_us, (int)esp_reset_reason(), (int)cause,
                (int)cold_boot, rtc.cycle);

  auto mcfg = M5.config();
  mcfg.clear_display = false;
  M5.begin(mcfg);
  M5.Display.powerSaveOff();

  Config cfg;
  if (!config_read(cfg)) {
    Serial.println("config incomplete - AP setup is phase 5, halting here");
    sleep_until(0);
  }

  // --- Toggle wake: redraw from the cached snapshot and go back to sleep.
  // Deliberately does NOT touch the network. Bringing up WiFi and fetching
  // would add seconds between the flick and the screen changing, and none of
  // the screens gains from data a few minutes fresher; the scheduled poll is
  // what keeps it current.
  if (woke_on_toggle && !cold_boot) {
    rtc.screen = (rtc.screen + 1) % SCREEN_COUNT;
    compose(rtc.screen, rtc.snap, cfg, false);
    rtc.screen = run_toggle_session(rtc.screen, rtc.snap, cfg);
    sleep_until(rtc.next_poll);   // does not return
  }

  // --- Scheduled poll. Always returns to the main screen: it is the view
  // this device exists for, and the only one that shows the alarm banner.
  rtc.screen = SCREEN_MAIN;

  int64_t t_wifi = esp_timer_get_time();
  WiFi.mode(WIFI_STA);
  WiFi.begin(cfg.wifissid.c_str(), cfg.wifipass.c_str());
  uint32_t deadline = millis() + WIFI_TIMEOUT_MS;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) delay(50);
  int64_t wifi_ms = (esp_timer_get_time() - t_wifi) / 1000;

  if (WiFi.status() != WL_CONNECTED) {
    // A transient WiFi failure skips this cycle rather than being fatal; the
    // next wake tries again.
    Serial.printf("wifi: failed after %lld ms, skipping cycle\n", wifi_ms);
    sleep_until(rtc.next_poll);
  }
  Serial.printf("wifi: connected in %lld ms, ip %s\n",
                wifi_ms, WiFi.localIP().toString().c_str());

  State s;
  int64_t t_fetch = esp_timer_get_time();
  bool ok = fetch_pump_data(cfg, s);
  Serial.printf("fetch+parse: %lld ms, ok=%d\n",
                (esp_timer_get_time() - t_fetch) / 1000, (int)ok);

  // The radio holds heap the canvas wants and costs current; down it goes
  // before drawing.
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);

  if (ok) {
    dump_state(s, cfg);
    rtc.snap = s;
    // Anchor the cadence on the fetch, not on the wake, so the tail of the
    // cycle never shifts the schedule.
    rtc.next_poll = time(nullptr) + POLL_PERIOD_S;
    if (!rtc.session_start && time(nullptr) > TIME_VALID_EPOCH) {
      rtc.session_start = time(nullptr);
    }
  }

  // Tested before the increment so cycle 0 - the first draw after a cold
  // boot, which has the splash still on the panel to clear - is a full one.
  compose(SCREEN_MAIN, rtc.snap, cfg, (rtc.cycle % FULL_REFRESH_EVERY) == 0);
  rtc.cycle++;

  sleep_until(rtc.next_poll);
}

void loop() {
  // Never reached: esp_deep_sleep() does not return.
}
