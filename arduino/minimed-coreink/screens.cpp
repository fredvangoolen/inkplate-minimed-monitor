#include "screens.h"
#include <time.h>
#include <math.h>

// ------------------------------------------------------------------- fonts
//
// NOTE the fonts are chosen differently from the MicroPython build, because
// M5GFX reports different metrics to C++ than its MicroPython binding does.
// Measured here: DejaVu72 75px tall / 141px wide for "160", DejaVu40 at 2x
// 84px tall / 156px wide, DejaVu24 25, DejaVu18 18, DejaVu12 13. The Python
// file picked DejaVu40-at-2x for the large reading precisely because ITS
// DejaVu72 measured only 50px; here DejaVu72 is both taller and 15px
// narrower, so it wins twice - and DejaVu40-at-2x would leave only ~30px
// beside a 3-digit reading, shrinking the trend arrows to 10px and losing
// the legibility the layout exists to protect.
static const lgfx::IFont *FONT_GLUCOSE     = &fonts::DejaVu40;   // 42px, alarm layout
static const lgfx::IFont *FONT_GLUCOSE_BIG = &fonts::DejaVu72;   // 75px, normal layout
static const float        GLUCOSE_BIG_SIZE = 1.0f;
static const lgfx::IFont *FONT_UNIT        = &fonts::DejaVu18;   // 20px
static const lgfx::IFont *FONT_VALUE       = &fonts::DejaVu24;   // 26px
static const lgfx::IFont *FONT_LABEL       = &fonts::DejaVu12;   // 16px
static const lgfx::IFont *FONT_ALARM_L     = &fonts::DejaVu18;   // short alarms
static const lgfx::IFont *FONT_ALARM_S     = &fonts::DejaVu12;   // long alarms, in full

// ----------------------------------------------------------------- helpers

static int text_width(LovyanGFX &g, const char *s, const lgfx::IFont *font, float size = 1.0f) {
  g.setFont(font);
  g.setTextSize(size);
  int w = g.textWidth(s);
  g.setTextSize(1.0f);
  return w;
}

static int font_height(LovyanGFX &g, const lgfx::IFont *font, float size = 1.0f) {
  g.setFont(font);
  g.setTextSize(size);
  int h = g.fontHeight();
  g.setTextSize(1.0f);
  return h;
}

static void draw_text(LovyanGFX &g, const char *s, int x, int y,
                      const lgfx::IFont *font, uint32_t color,
                      uint32_t bg = COLOR_WHITE, float size = 1.0f) {
  g.setFont(font);
  g.setTextSize(size);
  g.setTextColor(color, bg);
  g.drawString(s, x, y);
  g.setTextSize(1.0f);
}

// Label left, value right-aligned to the right margin, bottoms aligned so a
// small label sits on the same baseline as a larger value. The value is
// placed first and the label truncated to what is left, because if something
// has to be clipped it should be the constant label, not the number that
// actually changes. Returns the value's height.
static int draw_kv_row(LovyanGFX &g, int y,
                       const char *label, const lgfx::IFont *label_font,
                       const char *value, const lgfx::IFont *value_font,
                       uint32_t color, uint32_t bg = COLOR_WHITE) {
  int vh = font_height(g, value_font);
  int lh = font_height(g, label_font);
  int value_w = text_width(g, value, value_font);
  int value_x = PANEL_W - MARGIN - value_w;
  draw_text(g, value, value_x, y, value_font, color, bg);

  int label_max_w = value_x - MARGIN - 4;
  String lab(label);
  if (text_width(g, lab.c_str(), label_font) > label_max_w) {
    int ell = text_width(g, "...", label_font);
    String out;
    for (unsigned i = 0; i < lab.length(); i++) {
      String cand = out + lab[i];
      if (text_width(g, cand.c_str(), label_font) + ell > label_max_w) break;
      out = cand;
    }
    lab = out + "...";
  }
  draw_text(g, lab.c_str(), MARGIN, y + (vh - lh), label_font, color, bg);
  return vh;
}

// M5GFX has no auto-wrap and clips silently, so long text must be broken up
// by hand. Measures with real font metrics, these fonts being proportional.
static int wrap_text(LovyanGFX &g, const char *s, int max_w,
                     const lgfx::IFont *font, String *lines, int max_lines) {
  int n = 0;
  String cur;
  String rest(s);
  int start = 0;
  while (start <= (int)rest.length() && n < max_lines) {
    int sp = rest.indexOf(' ', start);
    String word = (sp < 0) ? rest.substring(start) : rest.substring(start, sp);
    String cand = cur.length() ? cur + " " + word : word;
    if (cur.length() && text_width(g, cand.c_str(), font) > max_w) {
      lines[n++] = cur;
      cur = word;
    } else {
      cur = cand;
    }
    if (sp < 0) break;
    start = sp + 1;
  }
  if (cur.length() && n < max_lines) lines[n++] = cur;
  return n;
}

static String truncate_to_width(LovyanGFX &g, const String &s, int max_w,
                                const lgfx::IFont *font) {
  if (text_width(g, s.c_str(), font) <= max_w) return s;
  int ell = text_width(g, "...", font);
  String out;
  for (unsigned i = 0; i < s.length(); i++) {
    String cand = out + s[i];
    if (text_width(g, cand.c_str(), font) + ell > max_w) break;
    out = cand;
  }
  return out + "...";
}

// Real arrow shapes rather than caret characters: M5GFX gives fillTriangle,
// so the head is one call and the shaft another. main.py had to print
// glyphs because the Inkplate driver could only do that; here the trend is
// what turns a number into a direction, so it is drawn properly.
enum ArrowDir { ARROW_UP, ARROW_FLAT, ARROW_DOWN };

static void draw_arrows(LovyanGFX &g, int x, int y, int w, int h,
                        ArrowDir dir, int count, uint32_t color) {
  for (int i = 0; i < count; i++) {
    int ax = x + i * (w + 3);
    int cx = ax + w / 2;
    int shaft_w = max(3, w / 3);
    if (dir == ARROW_FLAT) {
      int head_w = w / 2;
      int cy = y + h / 2;
      g.fillRect(ax, cy - shaft_w / 2, w - head_w, shaft_w, color);
      g.fillTriangle(ax + w - head_w, cy - h / 4,
                     ax + w - head_w, cy + h / 4,
                     ax + w,          cy, color);
    } else if (dir == ARROW_UP) {
      int head_h = h / 2;
      g.fillTriangle(ax, y + head_h, ax + w, y + head_h, cx, y, color);
      g.fillRect(cx - shaft_w / 2, y + head_h, shaft_w, h - head_h, color);
    } else {
      int head_h = h / 2;
      g.fillTriangle(ax, y + h - head_h, ax + w, y + h - head_h, cx, y + h, color);
      g.fillRect(cx - shaft_w / 2, y, shaft_w, h - head_h, color);
    }
  }
}

static void trend_arrows(const char *trend, ArrowDir &dir, int &count) {
  struct { const char *name; ArrowDir dir; int count; } table[] = {
    {"UP_TRIPLE",   ARROW_UP,   3},
    {"UP_DOUBLE",   ARROW_UP,   2},
    {"UP",          ARROW_UP,   1},
    {"NONE",        ARROW_FLAT, 1},
    {"DOWN",        ARROW_DOWN, 1},
    {"DOWN_DOUBLE", ARROW_DOWN, 2},
    {"DOWN_TRIPLE", ARROW_DOWN, 3},
  };
  for (auto &e : table) {
    if (!strcmp(trend, e.name)) { dir = e.dir; count = e.count; return; }
  }
  dir = ARROW_FLAT;
  count = 1;
}

// ------------------------------------------------------------------- time

static void local_tm(time_t utc, int tz_hours, int dst, struct tm &out) {
  time_t t = utc + (time_t)(tz_hours + dst) * 3600;
  gmtime_r(&t, &out);
}

// Same rule as main.py: minute/hour fields only, and anything past 15
// minutes or an hour is reported as no data rather than a large number.
static void time_delta_txt(const struct tm &upd, const struct tm &now,
                           char *out, size_t n) {
  int dmin = now.tm_min - upd.tm_min;
  if (dmin < 0) dmin += 60;
  int dhour = now.tm_hour - upd.tm_hour;
  if (dhour < 0) dhour += 24;
  if (dmin == 0 && dhour == 0)      snprintf(out, n, "Now");
  else if (dmin > 15 || dhour > 1)  snprintf(out, n, "No data");
  else                              snprintf(out, n, "%d min ago", dmin);
}

// ------------------------------------------------------------- main screen

void draw_main_screen(LovyanGFX &g, const State &s, const Config &c) {
  g.fillScreen(COLOR_WHITE);

  // An alarm outranks the generic pump banner.
  char banner_buf[110];
  if (s.alarm_text[0]) {
    if (s.alarm_local) {
      // For a genuine alarm (not the generic banner, which carries no
      // comparable timestamp) append when it actually occurred - that is how
      // a caregiver tells a fresh alarm from one still showing because it is
      // within its re-announce window, and a repeat beep from a new one.
      struct tm at;
      gmtime_r(&s.alarm_local, &at);   // already local wall clock
      snprintf(banner_buf, sizeof(banner_buf), "%s (%02d:%02d)",
               s.alarm_text, at.tm_hour, at.tm_min);
    } else {
      snprintf(banner_buf, sizeof(banner_buf), "%s", s.alarm_text);
    }
  } else {
    snprintf(banner_buf, sizeof(banner_buf), "%s", s.banner);
  }
  const char *banner_src = banner_buf;
  bool has_banner = banner_src[0] != '\0';

  // The reading is drawn large whenever the bottom strip is not needed for
  // an alarm, and shrinks only to make room for one: nothing else on this
  // screen competes for attention at a distance, so there is no reason to
  // render the one number the device exists for at half the available size.
  // When an alarm IS showing the trade reverses - the message matters more
  // than another 40px of digit, and the smaller figure is still legible.
  char sg_txt[8];
  if (s.sg > 0) snprintf(sg_txt, sizeof(sg_txt), "%d", s.sg);
  else          snprintf(sg_txt, sizeof(sg_txt), "---");
  bool out_of_range = s.sg > 0 &&
                      (s.sg > HYPER_THRESHOLD_MGDL || s.sg < HYPO_THRESHOLD_MGDL);

  const lgfx::IFont *g_font = has_banner ? FONT_GLUCOSE : FONT_GLUCOSE_BIG;
  float g_size = has_banner ? 1.0f : GLUCOSE_BIG_SIZE;

  int gh = font_height(g, g_font, g_size);
  int gy = 6;
  int band_y = gy - 4;
  int band_h = gh + 8;

  // No red ink here (the Inkplate coloured this number red), so out-of-range
  // inverts the whole glucose band. Full panel width rather than hugging the
  // digits, so it reads as a solid alert block from across a room.
  uint32_t fg = COLOR_BLACK, bg = COLOR_WHITE;
  if (out_of_range) {
    g.fillRect(0, band_y, PANEL_W, band_h, COLOR_BLACK);
    fg = COLOR_WHITE;
    bg = COLOR_BLACK;
  }

  draw_text(g, sg_txt, MARGIN, gy, g_font, fg, bg, g_size);

  int arrow_x = MARGIN + text_width(g, sg_txt, g_font, g_size) + 10;
  ArrowDir dir; int count;
  trend_arrows(s.trend, dir, count);
  int avail = PANEL_W - MARGIN - arrow_x;
  int arrow_w = 22;
  // Shrink rather than let a 3-arrow trend beside a 3-digit reading run off
  // the right edge, which M5GFX would clip silently.
  while (count * (arrow_w + 3) > avail && arrow_w > 7) arrow_w--;
#if DEBUG_LAYOUT
  Serial.printf("arrows: sg_w=%d arrow_x=%d avail=%d count=%d w=%d  (band %d..%d)\n",
                text_width(g, sg_txt, g_font, g_size), arrow_x, avail, count,
                arrow_w, band_y, band_y + band_h);
#endif
  draw_arrows(g, arrow_x, gy + 4, arrow_w, gh - 8, dir, count, fg);

  int unit_y = band_y + band_h + 2;
  draw_text(g, "mg/dL", MARGIN, unit_y, FONT_UNIT, COLOR_BLACK);

  int sep_y = unit_y + font_height(g, FONT_UNIT) + 6;
  g.drawLine(MARGIN, sep_y, PANEL_W - MARGIN, sep_y, COLOR_BLACK);

  // With no alarm the bottom strip is free, so these two rows sit against
  // the bottom edge rather than floating under the separator - balancing the
  // taller figure above, and keeping "Updated" in the same place it occupies
  // on the alarm layout so the eye does not have to hunt for it.
  int row_a_h = font_height(g, FONT_VALUE);
  int row_b_h = font_height(g, FONT_LABEL);
  int row_a_y = has_banner ? sep_y + 8
                           : PANEL_H - MARGIN - row_b_h - 6 - row_a_h;

  char insulin_txt[16];
  if (s.activeInsulin >= 0) snprintf(insulin_txt, sizeof(insulin_txt), "%.1f U", s.activeInsulin);
  else                      snprintf(insulin_txt, sizeof(insulin_txt), "-- U");
  draw_kv_row(g, row_a_y, "Act. insulin", FONT_LABEL, insulin_txt, FONT_VALUE, COLOR_BLACK);

  // The clock here is the moment THIS REFRESH happened, not the reading's
  // own timestamp: "Updated 4 min ago | 09:55" means the reading was from
  // 9:51 and the panel last redrew at 9:55. A timestamp that stops
  // advancing is how you notice the device died.
  int row_b_y = row_a_y + row_a_h + 6;
  if (s.lastUpdate) {
    struct tm tm_now, tm_upd;
    local_tm(time(nullptr), c.timezone, s.dstDelta, tm_now);
    local_tm(s.lastUpdate, c.timezone, s.dstDelta, tm_upd);
    char delta[24], clock[8], label[40];
    time_delta_txt(tm_upd, tm_now, delta, sizeof(delta));
    snprintf(clock, sizeof(clock), "%02d:%02d", tm_now.tm_hour, tm_now.tm_min);
    snprintf(label, sizeof(label), "Updated %s", delta);
    // Both halves in the small font: at the larger size the clock is wide
    // enough to squeeze the label past its truncation point, and the label
    // is where the staleness lives.
    draw_kv_row(g, row_b_y, label, FONT_LABEL, clock, FONT_LABEL, COLOR_BLACK);
  } else {
    draw_kv_row(g, row_b_y, "Updated", FONT_LABEL, "No data", FONT_LABEL, COLOR_BLACK);
  }

  // --- Alarm / pump banner: the two bottom lines, in reverse -------------
  if (has_banner) {
    String text(banner_src);
    // Two lines were asked for, and two lines is what the fault strings
    // need: the longest run past 50 characters, which no single line of this
    // panel holds legibly. Prefer the larger font when it fits in two lines
    // of it, and drop to the smaller one otherwise so a long message is
    // shown IN FULL rather than truncated - the tail of these strings is
    // the actionable half.
    int inner_w = PANEL_W - 2 * MARGIN - 4;
    const lgfx::IFont *font = FONT_ALARM_L;
    String lines[4];
    int n = wrap_text(g, text.c_str(), inner_w, font, lines, 4);
    if (n > 2) {
      font = FONT_ALARM_S;
      n = wrap_text(g, text.c_str(), inner_w, font, lines, 4);
    }
    if (n > 2) {
      // Still too long even small (only the very longest faults): truncate
      // the second line rather than silently lose the third.
      String tail = lines[1];
      for (int i = 2; i < n; i++) tail += " " + lines[i];
      lines[1] = truncate_to_width(g, tail, inner_w, font);
      n = 2;
    }

#if DEBUG_LAYOUT
    Serial.printf("banner: %d line(s) at %s\n", n,
                  font == FONT_ALARM_L ? "DejaVu18" : "DejaVu12");
    for (int i = 0; i < n; i++) Serial.printf("  [%d] '%s'\n", i, lines[i].c_str());
#endif

    int line_h = font_height(g, font);
    int bar_h = 4 + 2 * line_h + 2 + 4;
    int bar_y = PANEL_H - bar_h;
    g.fillRect(0, bar_y, PANEL_W, bar_h, COLOR_BLACK);
    int ty = bar_y + 4;
    for (int i = 0; i < n; i++) {
      draw_text(g, lines[i].c_str(), MARGIN + 2, ty, font, COLOR_WHITE, COLOR_BLACK);
      ty += line_h + 2;
    }
  }
}

// ------------------------------------------------------- secondary screens

// Shared chrome for the secondary screens. The main screen has no header on
// purpose: there every pixel of the top band belongs to the glucose figure,
// and a caregiver glancing over should recognise that view instantly rather
// than read a label to find out which screen is up.
static int draw_screen_header(LovyanGFX &g, const char *title) {
  draw_text(g, title, MARGIN, 4, FONT_LABEL, COLOR_BLACK);
  int y = 4 + font_height(g, FONT_LABEL) + 3;
  g.drawLine(MARGIN, y, PANEL_W - MARGIN, y, COLOR_BLACK);
  return y + 7;
}

static String fmt_runtime(time_t start) {
  // Hours, because the question this answers is "how many hours does a
  // charge last" - days would round away exactly the resolution wanted.
  if (!start) return "--";
  long secs = (long)(time(nullptr) - start);
  if (secs < 0) return "--";
  float hours = secs / 3600.0f;
  char buf[16];
  if (hours < 100) snprintf(buf, sizeof(buf), "%.1f h", hours);
  else             snprintf(buf, sizeof(buf), "%d h", (int)hours);
  return String(buf);
}

static void pct_txt(int v, char *buf, size_t n) {
  if (v < 0) snprintf(buf, n, "--");
  else       snprintf(buf, n, "%d%%", v);
}

void draw_stats_screen(LovyanGFX &g, const State &s) {
  g.fillScreen(COLOR_WHITE);
  int y = draw_screen_header(g, "GLUCOSE, LAST 24 H");

  // A stacked bar makes the split readable without reading any numbers,
  // which is the whole point of a summary screen. With no colour to work
  // with the segments are distinguished by fill: below-target solid black,
  // in-target hatched, above-target open. That ordering is deliberate - low
  // glucose is the dangerous end, so it gets the heaviest ink and is
  // impossible to miss in peripheral vision.
  int bar_h = 20;
  int bar_w = PANEL_W - 2 * MARGIN;
  int below = s.belowHypo, inrange = s.timeInRange, above = s.aboveHyper;
  if (below >= 0 && inrange >= 0 && above >= 0 && (below + inrange + above) > 0) {
    int total = below + inrange + above;
    int x = MARGIN;
    // The last segment takes the rounding remainder so the three always
    // exactly fill the bar; computing each independently leaves a 1-2px gap
    // that reads as a rendering fault.
    int w_below = bar_w * below / total;
    int w_in    = bar_w * inrange / total;
    int w_above = bar_w - w_below - w_in;
    g.fillRect(x, y, w_below, bar_h, COLOR_BLACK);
    x += w_below;
    for (int hx = x; hx < x + w_in; hx += 3) {
      g.drawLine(hx, y, hx, y + bar_h - 1, COLOR_BLACK);
    }
    x += w_in;
    g.fillRect(x, y, w_above, bar_h, COLOR_WHITE);
    g.drawRect(MARGIN, y, bar_w, bar_h, COLOR_BLACK);
  } else {
    g.drawRect(MARGIN, y, bar_w, bar_h, COLOR_BLACK);
  }
  y += bar_h + 8;

  char v[12], label[24];
  pct_txt(inrange, v, sizeof(v));
  y += draw_kv_row(g, y, "In target", FONT_LABEL, v, FONT_VALUE, COLOR_BLACK) + 4;
  pct_txt(above, v, sizeof(v));
  snprintf(label, sizeof(label), "Above %d", HYPER_THRESHOLD_MGDL);
  y += draw_kv_row(g, y, label, FONT_LABEL, v, FONT_VALUE, COLOR_BLACK) + 4;
  pct_txt(below, v, sizeof(v));
  snprintf(label, sizeof(label), "Below %d", HYPO_THRESHOLD_MGDL);
  y += draw_kv_row(g, y, label, FONT_LABEL, v, FONT_VALUE, COLOR_BLACK) + 4;

  char avg[16];
  if (s.averageSG < 0) snprintf(avg, sizeof(avg), "-- mg/dL");
  else                 snprintf(avg, sizeof(avg), "%d mg/dL", s.averageSG);
  y += 2;
  draw_kv_row(g, y, "Average", FONT_LABEL, avg, FONT_UNIT, COLOR_BLACK);
}

void draw_pump_screen(LovyanGFX &g, const State &s, const Config &c) {
  // Between glucose and the technical screen: what a caregiver plans around
  // rather than reacts to. None of it is urgent enough for the main screen,
  // all of it is what you want to know before leaving the house.
  g.fillScreen(COLOR_WHITE);
  int y = draw_screen_header(g, "PUMP & SENSOR");

  const char *patient = c.patient.length() ? c.patient.c_str()
                      : (s.patient[0] ? s.patient : "--");

  char insulin[24];
  if (s.reservoirUnits < 0)   snprintf(insulin, sizeof(insulin), "--");
  else if (s.reservoirPct < 0) snprintf(insulin, sizeof(insulin), "%d U", (int)lroundf(s.reservoirUnits));
  else snprintf(insulin, sizeof(insulin), "%d U  %d%%",
                (int)lroundf(s.reservoirUnits), s.reservoirPct);

  // 255 is the pump's "no sensor / not settled yet" sentinel, not a life of
  // ten and a half days.
  char sensor[16];
  if (s.sageHours >= 255 || s.sageHours < 0) snprintf(sensor, sizeof(sensor), "--");
  else if (s.sageHours >= 48) snprintf(sensor, sizeof(sensor), "%dd %dh",
                                       s.sageHours / 24, s.sageHours % 24);
  else snprintf(sensor, sizeof(sensor), "%d h", s.sageHours);

  char batt[8];
  pct_txt(s.batteryPct, batt, sizeof(batt));

  // Values in the larger font: four rows where the info screen has eight, so
  // the room is there, and these are numbers read across a room rather than
  // settings leaned in to check.
  struct { const char *label; const char *value; } rows[] = {
    {"Patient",   patient},
    {"Insulin",   insulin},
    {"Sensor",    sensor},
    {"Pump batt", batt},
  };
  for (auto &r : rows) {
    y += draw_kv_row(g, y, r.label, FONT_LABEL, r.value, FONT_VALUE, COLOR_BLACK) + 6;
  }
}

void draw_info_screen(LovyanGFX &g, const State &s, const Config &c,
                      time_t session_start) {
  g.fillScreen(COLOR_WHITE);
  int y = draw_screen_header(g, "DEVICE & NETWORK");

  char batt[8];
  int level = M5.Power.getBatteryLevel();
  if (level < 0) snprintf(batt, sizeof(batt), "--");
  else           snprintf(batt, sizeof(batt), "%d%%", level);

  char clock[8];
  if (time(nullptr) > 1600000000) {
    struct tm now;
    local_tm(time(nullptr), c.timezone, s.dstDelta, now);
    snprintf(clock, sizeof(clock), "%02d:%02d", now.tm_hour, now.tm_min);
  } else {
    snprintf(clock, sizeof(clock), "--");
  }

  char port[8], version[16];
  snprintf(port, sizeof(port), "%u", c.proxyport);
  snprintf(version, sizeof(version), "V%s", VERSION_STR);
  String runtime = fmt_runtime(session_start);

  // Battery first: the only line that changes on its own and the only one
  // that predicts the device silently dying. NTP server and timezone used to
  // sit here and were dropped rather than squeezed - both are write-once
  // settings readable from the config page, where runtime and battery change
  // by themselves and are what someone comes to this screen to find.
  struct { const char *label; const char *value; } rows[] = {
    {"Battery",  batt},
    {"Runtime",  runtime.c_str()},
    {"Time",     clock},
    // The network actually connected, not merely the one configured - with
    // several slots, "which am I on?" is the useful answer.
    {"WiFi",     s.ssid[0] ? s.ssid
                           : (c.wifi[0].ssid.length() ? c.wifi[0].ssid.c_str() : "--")},
    {"IP",       s.ip[0] ? s.ip : "--"},
    {"Proxy",    c.proxyaddr.length() ? c.proxyaddr.c_str() : "--"},
    {"Port",     port},
    {"Version",  version},
  };
  int lh = font_height(g, FONT_LABEL);
  for (auto &r : rows) {
    // An SSID, an IPv4 address, a hostname: the one place on any screen
    // where user-supplied text of unbounded length is rendered, so these are
    // right-aligned and truncated rather than trusted to fit.
    draw_kv_row(g, y, r.label, FONT_LABEL, r.value, FONT_LABEL, COLOR_BLACK);
    y += lh + 5;
  }
}

void draw_current_screen(LovyanGFX &g, int screen, const State &s,
                         const Config &c, time_t session_start) {
  switch (screen) {
    case SCREEN_STATS: draw_stats_screen(g, s);                    break;
    case SCREEN_PUMP:  draw_pump_screen(g, s, c);                  break;
    case SCREEN_INFO:  draw_info_screen(g, s, c, session_start);   break;
    default:           draw_main_screen(g, s, c);                  break;
  }
}

void draw_status_screen(LovyanGFX &g, const char *msg) {
  g.fillScreen(COLOR_WHITE);
  int y = MARGIN;
  int lh = font_height(g, FONT_LABEL);
  String rest(msg);
  while (rest.length()) {
    int nl = rest.indexOf('\n');
    String line = (nl < 0) ? rest : rest.substring(0, nl);
    rest = (nl < 0) ? String() : rest.substring(nl + 1);
    String wrapped[6];
    int n = wrap_text(g, line.c_str(), PANEL_W - 2 * MARGIN, FONT_LABEL, wrapped, 6);
    for (int i = 0; i < n; i++) {
      draw_text(g, wrapped[i].c_str(), MARGIN, y, FONT_LABEL, COLOR_BLACK);
      y += lh + 2;
    }
  }
}
