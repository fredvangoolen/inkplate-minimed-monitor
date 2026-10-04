// Screen composition for the Core Ink Arduino port.
//
// Ported from draw_screen() and friends in main_m5coreink.py. The layout
// reasoning lives there and in PORTING-M5COREINK.md; this file keeps the
// same geometry, the same fonts and the same decisions, so the two builds
// can be compared side by side on identical data.

#pragma once

#include <M5Unified.h>
#include "types.h"

// Shown on the info screen; kept here so both the screen and the sketch
// agree on one string.
#define VERSION_STR "0.1-arduino"

static const int PANEL_W = 200;
static const int PANEL_H = 200;
static const int MARGIN  = 4;

// The panel is 1bpp but M5GFX wants a greyscale sprite; 4bpp is the sweet
// spot measured on this board - 1bpp needs a format conversion on the way
// out and pushes in ~340ms, while 4bpp and 8bpp both push in ~25ms and 4bpp
// costs half the RAM (20KB against 40KB) on a board with no PSRAM.
static const int CANVAS_BPP = 4;

// RGB888, as the MicroPython build passes them; LovyanGFX converts to the
// sprite's format.
static const uint32_t COLOR_BLACK = 0x000000u;
static const uint32_t COLOR_WHITE = 0xFFFFFFu;

// Fixed local thresholds - the proxy carries no per-reading threshold to
// read instead. Used to invert the glucose band, this panel having no red
// ink to colour it with.
static const int HYPER_THRESHOLD_MGDL = 180;
static const int HYPO_THRESHOLD_MGDL  = 70;

// Fill steps for the battery symbol on the main screen. Three states, not a
// proportional bar: at a glance you want "fine / getting on / do something",
// and this panel is read in passing rather than studied.
//
// MILLIVOLTS, not the percentage. M5Unified computes that percentage as
//     (mv - 3300) * 100 / (4150 - 3350)       Power_Class.cpp:2341
// which subtracts 3300 but divides by 800 - the two ends disagree - and it
// saturates at 100% from 4100mV up, so a full battery and a charging one are
// indistinguishable. It is also a straight line across a Li-ion curve that is
// nearly flat from 3.9V to 3.6V. Keyed to it, "50%" meant 3700mV, which is
// already at the knee: that is why this board appeared to die quickly just
// below half.
//
// Datasheet-nominal until a real discharge curve exists for this cell. The
// voltage is logged every cycle so one can be gathered; see the measurement
// notes in main_m5coreink.py's draw_info_screen().
static const int BATTERY_FULL_MV    = 3900;   // >= this: solid
static const int BATTERY_HALF_MV    = 3700;   // >= this: half; below: empty
// isCharging() returns true on battery on this board (confirmed while the
// pack was visibly discharging), so voltage is the only usable charger test.
static const int BATTERY_CHARGE_MV  = 4250;
// Hysteresis. A step UP needs this much more than the bare threshold, so a
// pack resting near a boundary does not flip the symbol on alternate wakes.
// Not theoretical: the first real reading came in at 3897mV against a 3900mV
// boundary, with the ADC repeatable to about +-15mV.
static const int BATTERY_HYST_MV    = 40;
// Charging is NOT reported by this board. isCharging() has no case for the
// Core Ink and falls through to charge_unknown, which is 2 - truthy, which is
// why it reads as "charging" on battery. So it is inferred instead.
//
// The voltage threshold alone is not enough either: measured on USB, a
// charging pack sat at 3947mV, nowhere near 4250, so the bolt would only ever
// appear at the very end of a charge. The usable signal is the TREND between
// scheduled polls - the same pack climbed 3897 -> 3917 -> 3947 while charging,
// against +-15mV of sampling noise. A fall of the same size clears it; in
// between, the last decision stands, so the constant-voltage plateau at the
// end of a charge does not drop the bolt.
static const int BATTERY_RISE_MV    = 25;

enum { BATT_EMPTY = 0, BATT_HALF = 1, BATT_FULL = 2 };

// Lightning bolt drawn to the LEFT of the body while charging.
static const int BOLT_W = 7, BOLT_H = 13, BOLT_GAP = 3;

// 1 = print computed layout geometry (arrow sizing, banner wrapping) to
// serial. There is no REPL on this board and no screenshots, so these
// numbers are how a layout gets checked without asking a human to look at
// the panel.
#define DEBUG_LAYOUT 0

// Everything draws through LovyanGFX, the common base of both M5GFX (the
// panel) and M5Canvas (the off-screen sprite), so the composed and direct
// paths cannot diverge - the same reason the MicroPython build funnels
// everything through gfx().
// Screen order, cycled endlessly by the toggle in either direction. Pump
// sits between the glucose stats and the technical screen, as in the
// MicroPython build.
enum { SCREEN_MAIN = 0, SCREEN_STATS = 1, SCREEN_PUMP = 2, SCREEN_INFO = 3,
       SCREEN_COUNT = 4 };

// Which way a flick moves through that list. Down walks forward in the order
// above; up walks back the way you came, which is the only reason the second
// direction exists.
static const int SCREEN_FORWARD = +1;
static const int SCREEN_BACK    = -1;

// Multi-line status text, used by the setup portal. Wraps rather than
// clipping: these strings are longer than this narrow panel fits, and
// M5GFX clips silently.
void draw_status_screen(LovyanGFX &g, const char *msg);

int  battery_mv();   // median of several ADC reads, millivolts
int  battery_state(int mv, int prev);   // BATT_*, with hysteresis
bool battery_charging(int mv, int prev_mv, bool was);   // inferred, see above
void draw_main_screen(LovyanGFX &g, const State &s, const Config &c);
void draw_current_screen(LovyanGFX &g, int screen, const State &s,
                         const Config &c, time_t session_start);
