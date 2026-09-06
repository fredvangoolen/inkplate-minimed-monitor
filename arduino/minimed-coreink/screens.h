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

// Multi-line status text, used by the setup portal. Wraps rather than
// clipping: these strings are longer than this narrow panel fits, and
// M5GFX clips silently.
void draw_status_screen(LovyanGFX &g, const char *msg);

void draw_main_screen(LovyanGFX &g, const State &s, const Config &c);
void draw_current_screen(LovyanGFX &g, int screen, const State &s,
                         const Config &c, time_t session_start);
