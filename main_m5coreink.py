###############################################################################
#
#  M5Core Ink Minimed Monitor
#
#  Description:
#
#  A remote monitor for the Medtronic Minimed 770G/780G insulin pump system,
#  for use by caregivers of a Type-1 Diabetes patient wearing the pump. Runs
#  on an M5Stack Core Ink (1.54" 200x200 monochrome e-paper, ESP32-PICO-D4)
#  under the UIFlow2 MicroPython firmware (uiflow_micropython,
#  https://github.com/m5stack/uiflow_micropython): it wakes every ~5 minutes,
#  polls an external Carelink proxy for the pump's current glucose/insulin/
#  sensor status, redraws a single always-current view, sounds the buzzer if
#  an alarm is active, and goes back to deep sleep.
#
#  Relationship to main.py (the Inkplate 2 version):
#
#  Board-independent logic - the fault-code tables, config storage, the
#  AP-mode config server, WiFi connect, pump polling and the alarm staleness
#  rules - is carried over and the two files should be kept in sync when
#  that logic changes. The display layer, the alarm alerting, the HTTP
#  client and the time epoch are board-specific and deliberately differ.
#  DO NOT copy hardware reasoning between the two files; on this board
#  several of main.py's documented facts are simply false (see below).
#
#  Hardware/firmware notes (all confirmed on real hardware, by probing this
#  device over mpremote - the values in brackets are what was measured):
#
#  * The panel is 200x200 SQUARE and monochrome [M5.Lcd.width()/height() =
#         200x200, isEPD() = True], not 212x104 wide and 3-color. There is
#         no red ink, so "out of range" cannot be signalled by color the way
#         main.py does - draw_screen() inverts the glucose band to
#         white-on-black instead, the same reverse treatment the alarm rows
#         use. The square aspect is why this layout is a vertical stack;
#         main.py had to put active insulin in a side column beside the
#         number because 104px of height left no room under it.
#  * THE TIME EPOCH IS THE STANDARD UNIX 1970 EPOCH ON THIS FIRMWARE, NOT
#         the 2000-01-01 epoch main.py documents for the Inkplate build
#         [time.localtime(0) returned (1970,1,1,0,0,0,3,1) and
#         time.mktime((2026,1,1,0,0,0,0,0)) returned 1767225600, the correct
#         Unix value]. EPOCH_ADJUST_S is therefore 0 here. Carrying
#         main.py's 946684800 across unchanged would silently date every
#         reading 30 years into the past.
#  * There IS a buzzer, on GPIO2, driven through M5.Speaker with
#         spk_cfg.buzzer set [confirmed both in the M5Unified board table
#         and by sounding it on the device]. main.py's header records "no
#         speaker ... a caregiver not looking at the display gets no alert"
#         as an accepted shortcoming of that board; this port resolves it -
#         see "Alarm handling" below.
#  * Fonts are M5GFX bitmap fonts selected by object, not a scalable size
#         parameter, and the largest that exists here is 50px tall
#         [measured fontHeight(): DejaVu9/ASCII7 15, DejaVu12 16, DejaVu18
#         20, DejaVu24 26, DejaVu40 43, DejaVu56 48, DejaVu72 50]. Note
#         DejaVu56/72 are NOT 56/72px - the name is not the height, which
#         is exactly the sort of assumption that produced main.py's
#         documented "collided the mg/dL label into the glucose number"
#         bug. All layout below is derived from measured metrics via
#         text_width()/font_height(), never from a name or a nominal size.
#  * An HTTP client IS available [requests2 imports fine], so unlike
#         main.py this file does not hand-roll HTTP/1.0 over a socket.
#         http_get() below still goes through a raw socket anyway - see the
#         comment there, it is about the unattended-device timeout
#         guarantee, not about module availability.
#  * The board's power-hold latch (GPIO12) is asserted by the firmware, not
#         by this file, so unlike a bare-MicroPython port this file only has
#         to hold it across deep sleep. It must happen fast: see
#         firmware/coreink-power-hold.patch, which moves the assert to 50ms
#         after reset (stock UIFlow: ~2s). That is what lets a crash or a
#         machine.reset() recover on battery instead of switching the board
#         off. See hold_power_rail().
#  * machine.deepsleep() between cycles re-runs this whole script from
#         scratch on every wake, exactly as in main.py - main() is a single
#         cycle, not a loop, and carries no state across a wake by design.
#  * Panel refresh is on the order of a second, not the Inkplate's ~17-23s.
#         main.py's timing reasoning (why the splash is cold-boot-only, why
#         a refresh per AP status message is "a non-issue") is about that
#         17-23s cost and does NOT transfer. The splash is still cold-boot
#         only here, but for tidiness rather than for the power budget.
#
#  Deployment:
#
#  This needs the UIFlow2 firmware built for the M5STACK_CoreInk board, and
#  it must run as /flash/main.py with the NVS key boot_option set to 0
#  ("run main.py directly"). With the stock boot_option of 1 the UIFlow
#  launcher takes over the screen and holds the REPL, and this script never
#  runs. See PORTING-M5COREINK.md for the exact commands.
#
#  Dependencies:
#
#  Polls an external Carelink proxy (a REST API in front of Medtronic's
#  Carelink Cloud) for pump data - see the Carelink Python Client project
#  (https://github.com/ondrej1024/carelink-python-client) for one way to
#  run such a proxy. Point this script at it via the config's proxyaddr/
#  proxyport (see "Access configuration parameters" below).
#
#  Copyright 2021-2026, Ondrej Wisniewski and contributors
#
#  Modified 2026 to target the Soldered Inkplate 2 platform, then ported to
#  the M5Stack Core Ink (this file).
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
###############################################################################

import M5
from M5 import Widgets
import ntptime
import time
import json
import network
import socket
import machine
import gc

VERSION = "0.1"

# Constants. The UIFlow2 firmware chdir()s to /flash at boot and mounts the
# user filesystem there (the Inkplate build's root was "/"), so this is an
# absolute /flash path rather than main.py's "/minimed_config.json".
CONFIG_FILE = "/flash/minimed_config.json"

# Default configuration parameters
DEFAULT_NTP_SERVER = "pool.ntp.org"
DEFAULT_TIME_ZONE  = "1"
DEFAULT_PROXY_PORT = "8081"

# Access point parameters
API_URL     = "carelink/nohistory"
AP_SSID     = "M5INK_MINIMED_MON"
AP_ADDR     = "192.168.4.1"

# Fixed local thresholds - there's no pump-configured per-reading threshold
# in the Carelink proxy's response to read instead. Used to invert the
# glucose band in draw_screen() (this panel has no red ink), and also by
# get_alarm_text() to decide whether a low/high-glucose alarm notification
# still reflects the current reading (see "Alarm handling" below).
HYPER_THRESHOLD_MGDL = 180
HYPO_THRESHOLD_MGDL  = 70

# Timing. As in main.py there is no periodic NTP-resync interval to track
# separately - main() resyncs NTP on every wake, since machine.deepsleep()
# re-runs the whole script from scratch each cycle anyway and there is no
# in-memory state that could track "time since last sync" across a
# deep-sleep restart in the first place.
POLL_PERIOD_S        = 300    # 5 min: matches upstream CGM reading cadence
ALARM_RECENCY_S      = 15*60  # only announce alarms newer than this

# THIS FIRMWARE USES THE STANDARD UNIX 1970 EPOCH - confirmed on this
# device: time.localtime(0) returns (1970,1,1,0,0,0,3,1), and
# time.mktime((2026,1,1,0,0,0,0,0)) returns 1767225600, which is the correct
# Unix timestamp for that date. Carelink's lastConduitUpdateServerDateTime
# is also a Unix-epoch value, so no adjustment is needed and this is 0.
#
# This is the single most important difference from main.py, whose Inkplate
# firmware uses a 2000-01-01 epoch and therefore sets this to 946684800.
# Copying that value here would have shown every reading dated 30 years
# early while looking entirely plausible in the code. Kept as a named
# constant rather than deleted so the contrast with main.py stays visible,
# and so a future firmware change has one obvious place to correct.
EPOCH_ADJUST_S = 0

# Global variables. Note there is deliberately no per-alarm dedup state
# here - see the "Alarm handling" section below for why, and how that keeps
# main()'s machine.deepsleep() cycle stateless across wakes.
dstDelta     = 0


# Fault ID mapping: raw Carelink fault ID -> canonical ID (many-to-one).
# Reverse-engineered from real Carelink pump alarm data - treat this as
# data, not something to restructure; extend by adding entries.
# Kept identical to main.py's copy - keep the two in sync.
faultIdMapping = {
   "002": "002",
   "003": "002",
   "004": "002",
   "013": "002",
   "014": "002",
   "015": "002",
   "016": "002",
   "017": "002",
   "018": "002",
   "019": "002",
   "020": "002",
   "022": "002",
   "023": "002",
   "026": "002",
   "027": "002",
   "028": "002",
   "030": "002",
   "031": "002",
   "033": "002",
   "034": "002",
   "044": "002",
   "045": "002",
   "046": "002",
   "049": "002",
   "053": "002",
   "054": "002",
   "060": "002",
   "063": "002",
   "064": "002",
   "065": "002",
   "067": "002",
   "068": "002",
   "074": "002",
   "075": "002",
   "076": "002",
   "079": "002",
   "080": "002",
   "081": "002",
   "082": "002",
   "117": "117",
   "817": "817",
   "805": "805",
   "819": "819",
   "820": "819",
   "012": "012",
   "807": "807",
   "808": "807",
   "814": "814",
   "103": "103",
   "829": "829",
   "830": "829",
   "831": "829",
   "100": "100",
   "051": "051",
   "775": "775",
   "776": "776",
   "869": "869",
   "832": "832",
   "812": "812",
   "777": "777",
   "778": "777",
   "789": "777",
   "833": "833",
   "024": "024",
   "035": "024",
   "040": "024",
   "047": "024",
   "048": "024",
   "050": "024",
   "055": "024",
   "131": "024",
   "052": "052",
   "007": "007",
   "008": "007",
   "140": "140",
   "801": "801",
   "816": "816",
   "823": "823",
   "824": "823",
   "058": "058",
   "069": "069",
   "780": "780",
   "781": "780",
   "795": "795",
   "815": "815",
   "802": "802",
   "803": "803",
   "822": "822",
   "821": "821",
   "107": "107",
   "066": "066",
   "796": "796",
   "057": "057",
   "006": "006",
   "084": "084",
   "061": "061",
   "037": "037",
   "038": "037",
   "039": "037",
   "041": "037",
   "042": "037",
   "043": "037",
   "025": "025",
   "029": "029",
   "077": "077",
   "779": "779",
   "870": "870",
   "011": "011",
   "073": "011",
   "104": "104",
   "113": "113",
   "105": "105",
   "106": "105",
   "130": "130",
   "797": "797",
   "798": "797",
   "794": "794",
   "109": "109",
   "784": "784",
   "110": "110",
   "810": "810",
   "811": "810",
   "809": "809",
   "062": "062",
   "070": "062",
   "071": "062",
   "072": "062",
   "108": "062",
   "114": "062",
   "786": "062",
   "787": "062",
   "788": "062",
   "799": "062",
   "806": "062",
   "825": "062",
   "828": "062",
   "827": "827",
}

# Fault ID table: canonical ID -> human-readable message
faultIdTable = {
   "002": "Pump Error. Delivery Stopped",
   "006": "Pump Battery Out Limit",
   "007": "Delivery Stopped. Check BG",
   "011": "Replace Pump Battery Now",
   "012": "Auto Suspend Limit Reached. Delivery Stopped",
   "024": "Critical Pump Error. Stop Pump Use. Use Other Treatment",
   "025": "Pump Power Error. Record Settings",
   "029": "Pump Restarted. Delivery Stopped",
   "037": "Pump Motor Error. Delivery Stopped",
   "051": "Bolus Stopped",
   "052": "Delivery Limit Exceeded. Check BG",
   "057": "Pump Battery Not Compatible",
   "058": "Insert A New AA Battery",
   "061": "Pump Button Error. Delivery Stopped",
   "062": "New Notification Received From Pump",
   "066": "No Reservoir Detected During Infusion Set Change",
   "069": "Loading Incomplete During Infusion Set Change",
   "073": "Replace Pump Battery Now",
   "077": "Pump Settings Error. Delivery Stopped",
   "084": "Pump Battery Removed. Replace Battery",
   "100": "Bolus Entry Timed Out Before Delivery",
   "103": "BG Check Reminder",
   "104": "Replace Pump Battery Soon",
   "105": "Reservoir Low. Change Reservoir Soon",
   "107": "Missed Meal Bolus Reminder",
   "109": "Set Change Reminder",
   "110": "Silenced Sensor Alert. Check Alarm History",
   "113": "Reservoir Empty. Change Reservoir Now",
   "117": "Active Insulin Cleared",
   "130": "Rewind Required. Delivery Stopped",
   "140": "Delivery Suspended. Connect Infusion Set",
   "775": "Calibrate Now",
   "776": "Calibration Error",
   "777": "Change Sensor",
   "779": "Recharge Transmitter Now",
   "780": "Lost Sensor Signal",
   "784": "SG Rising Rapidly",
   "794": "Sensor Expired. Change Sensor",
   "795": "Lost Sensor Signal. Check Transmitter",
   "796": "No Sensor Signal",
   "797": "Sensor Connected",
   "801": "Do Not Calibrate. Wait Up To 3 Hours",
   "802": "Low Sensor Glucose",
   "803": "Low Sensor Glucose. Check BG",
   "805": "Alert Before Low. Check BG",
   "807": "Basal Delivery Resumed. Check BG",
   "809": "Suspend On Low. Delivery Stopped. Check BG",
   "810": "Suspend Before Low. Delivery Stopped. Check BG",
   "812": "Call Emergency Assistance",
   "814": "Basal Resumed. SG Still Under Low Limit. Check BG",
   "815": "Low Limit Changed. Basal Manually Resumed. Check BG",
   "816": "High Sensor Glucose",
   "817": "Alert Before High. Check BG",
   "819": "Auto Mode Exit. Basal Delivery Started. BG Required",
   "821": "Minimum Delivery Timeout. BG Required",
   "822": "Maximum Delivery Timeout. BG Required",
   "823": "High Sensor Glucose For Over 1 Hour",
   "827": "Urgent Low Sensor Glucose. Check BG",
   "829": "BG Required",
   "832": "Calibration Required",
   "833": "Correction Bolus Recommended",
   "869": "Calibration Reminder",
   "870": "Recharge Transmitter Soon",
}


#################################################
#
# Panel geometry and fonts
#
# Every font here is chosen by measured height, not by the number in its
# name: on this build DejaVu56 is 48px and DejaVu72 is 50px, so the names
# are not sizes. The layout constants below are all derived from
# font_height() at runtime rather than hard-coded, so swapping a font
# cannot silently overlap two rows.
#
#################################################

PANEL_W = 200
PANEL_H = 200
MARGIN  = 4

BLACK = 0x000000
WHITE = 0xFFFFFF

FONT_GLUCOSE = Widgets.FONTS.DejaVu72   # 50px - the largest that exists here
# The no-alarm reading: DejaVu40 at 2x measures 86px tall and 140px wide for
# three digits, leaving ~46px beside it for the trend arrows. DejaVu72 at 2x
# would be 100px tall but 154px wide, squeezing the arrows below legibility.
FONT_GLUCOSE_BIG = Widgets.FONTS.DejaVu40
GLUCOSE_BIG_SIZE = 2
FONT_UNIT    = Widgets.FONTS.DejaVu18   # 20px
FONT_VALUE   = Widgets.FONTS.DejaVu24   # 26px
FONT_LABEL   = Widgets.FONTS.DejaVu12   # 16px
FONT_ALARM_L = Widgets.FONTS.DejaVu18   # 20px - short alarms
FONT_ALARM_S = Widgets.FONTS.DejaVu12   # 16px - long alarms, shown in full


# Every screen is composed off-screen into a canvas and pushed to the panel
# in ONE operation. This is not an optimisation, it is the difference
# between usable and not: M5GFX drives this e-paper panel on each individual
# drawing call, so a screen built from a few dozen primitives triggers a few
# dozen panel updates. Measured on the device, drawing straight to M5.Lcd
# took 7.7s for the main screen and 18.9s for the stats screen (whose
# hatched bar is a loop of drawLine calls) - and the panel visibly redrew
# the whole way through. The same content composed into a canvas and pushed
# once measures ~30ms. It is also why the switch felt unresponsive: nothing
# could poll the toggle during those seconds of blocking redraw.
#
# 4bpp is the sweet spot: 1bpp pushes in ~340ms (it needs a format
# conversion on the way out) while 4bpp and 8bpp both push in ~25ms, and
# 4bpp costs half the RAM of 8bpp (20KB against 40KB) on a board that has
# no PSRAM.
CANVAS_BPP = 4

# Drawing helpers render to whatever this points at - the canvas while a
# screen is being composed, and the panel directly if a canvas could not be
# allocated. Everything downstream is written against gfx() so the two paths
# cannot drift apart.
_target = [None]


def gfx():
   return _target[0] if _target[0] is not None else M5.Lcd


# The canvas competes with the WiFi stack for the ESP-IDF heap, and both
# ends of that competition are fatal if handled naively. This board has
# ~98KB of IDF heap with a ~55KB largest contiguous block when idle; the
# WiFi stack claims roughly 45KB of it, and this sprite wants 20KB.
#
#   * Allocate the sprite late (at draw time, after the fetch) and the
#     allocation lands on a heap that WiFi and a few opened-and-closed
#     sockets have already carved up. M5GFX does not report that as an
#     error - it abort()s in C++, which no Python try/except can catch, and
#     the device reboots. This reliably killed the first cycle after boot.
#   * Allocate it early, before WiFi starts, and the radio then fails to
#     initialise at all: "OSError: WiFi Out of Memory".
#
# So the sprite is allocated per draw, and main() shuts the radio down
# before drawing - by then the fetch is finished and the network is dead
# weight, which hands back the ~45KB this needs. The guard below is the
# backstop for every path that cannot make that promise (AP-mode setup
# screens, where the access point must stay up): rather than risk an
# uncatchable abort, it checks the largest free block first and falls back
# to drawing straight to the panel, which is slow and flickery but cannot
# crash.
CANVAS_BYTES = PANEL_W * PANEL_H * CANVAS_BPP // 8   # 20000 at 4bpp
CANVAS_HEADROOM = CANVAS_BYTES + CANVAS_BYTES // 2   # refuse to cut it fine


def _largest_free_block():
   # None means "cannot tell" - callers then just try, which is the old
   # behaviour and correct on a firmware without esp32.idf_heap_info.
   try:
      import esp32
      return max(r[2] for r in esp32.idf_heap_info(esp32.HEAP_DATA))
   except Exception:
      return None


def compose(draw_fn, full_refresh=False):
   # Run draw_fn() against an off-screen canvas, then put the finished image
   # on the panel in a single update. Every screen in this file goes through
   # here so none of them can accidentally fall back to the slow
   # one-update-per-primitive path.
   M5.Lcd.setEpdMode(M5.Lcd.EPDMode.EPD_QUALITY if full_refresh
                     else M5.Lcd.EPDMode.EPD_FAST)
   canvas = None
   # Only sweep when the heap actually looks too tight to allocate into.
   # gc.collect() costs ~150ms on a heap this size, which is several times
   # the entire cost of composing and pushing a screen - paying it on every
   # redraw made flicking through the screens visibly laggier than the
   # allocation it was meant to protect.
   free = _largest_free_block()
   if free is not None and free < CANVAS_HEADROOM:
      gc.collect()
      free = _largest_free_block()
   if free is None or free >= CANVAS_HEADROOM:
      try:
         canvas = M5.Lcd.newCanvas(PANEL_W, PANEL_H, CANVAS_BPP)
      except Exception as e:
         print("Canvas allocation failed (%s), drawing direct" % e)
   else:
      print("Only %d bytes contiguous, drawing direct (need %d)"
            % (free, CANVAS_HEADROOM))
   _target[0] = canvas
   try:
      draw_fn()
      if canvas is not None:
         canvas.push(0, 0)
   finally:
      # Always restore the target and release the buffer, including on a
      # drawing error - leaking a 20KB sprite every cycle would exhaust the
      # heap within an hour of an otherwise survivable bug.
      _target[0] = None
      if canvas is not None:
         try:
            canvas.delete()
         except Exception:
            pass


def text_width(s, font, size=1):
   gfx().setFont(font)
   gfx().setTextSize(size)
   w = gfx().textWidth(s)
   gfx().setTextSize(1)
   return w


def font_height(font, size=1):
   gfx().setFont(font)
   gfx().setTextSize(size)
   h = gfx().fontHeight()
   gfx().setTextSize(1)
   return h


def draw_text(s, x, y, font, color, bg=WHITE, size=1):
   gfx().setFont(font)
   gfx().setTextSize(size)
   gfx().setTextColor(color, bg)
   gfx().drawString(s, x, y)
   gfx().setTextSize(1)


def wrap_text_to_width(s, max_w, font):
   # M5GFX has no auto-wrap, same as the Inkplate driver. Unlike main.py's
   # version this measures with the real font metrics rather than a
   # per-character cell, since these fonts are proportional.
   words = s.split(" ")
   lines = []
   cur = ""
   for word in words:
      candidate = (cur + " " + word).strip()
      if cur and text_width(candidate, font) > max_w:
         lines.append(cur)
         cur = word
      else:
         cur = candidate
   if cur:
      lines.append(cur)
   return lines


def truncate_to_width(s, max_w, font):
   if text_width(s, font) <= max_w:
      return s
   ell_w = text_width("...", font)
   out = ""
   for ch in s:
      if text_width(out + ch, font) + ell_w > max_w:
         break
      out += ch
   return out + "..."


#################################################
#
# Screens and the cross-wake state that drives them
#
# The toggle (the 5-way selector) cycles endlessly through three views.
# That requires two things this file otherwise deliberately avoids: state
# that survives a deep-sleep restart, and a wake source other than the
# timer.
#
# State lives in RTC memory (machine.RTC().memory(), ~2KB, retained through
# deep sleep - confirmed present on this build), holding the current screen
# index, the time the next data poll is due, and a compact snapshot of the
# last successful fetch. The snapshot is what makes the toggle feel
# instant: a button wake redraws from it directly, with no WiFi
# association and no HTTP round trip, which would otherwise put several
# seconds between the flick and the screen changing.
#
# This does NOT reintroduce the cross-cycle coupling the top-of-file design
# note warns about. Nothing here is required for correctness - RTC memory
# is allowed to be empty or unparseable at any moment (it is, on the very
# first boot), and every reader below degrades to "no data" rather than
# failing. The scheduled poll cycle still recomputes everything from
# scratch, exactly as before, and never reads the snapshot back.
#
#################################################

SCREEN_MAIN  = 0
SCREEN_STATS = 1
SCREEN_PUMP  = 2
SCREEN_INFO  = 3
SCREEN_COUNT = 4

# The three-position switch, labelled G37/G39/G38 on the case: up is
# GPIO37, down is GPIO39, press is GPIO38. Confirmed by watching every
# candidate GPIO while the control was operated - all three are momentary
# (every pin reads 1 at rest, so they are active-low).
#
# Both up and down advance a screen. Waking on either takes BOTH of the
# ESP32's GPIO wake sources, because neither one alone can express "either
# of two active-low pins": ext0 handles a single pin, and ext1's only modes
# are ALL_LOW (every listed pin low simultaneously) and ANY_HIGH (these
# idle high, so it would fire the instant the device slept). They are
# independent wake sources though, so arming ext0 on one pin and ext1 on
# the other gets both directions - see arm_toggle_wake().
BTN_UP_PIN   = 37
BTN_DOWN_PIN = 39

RTC_STATE_VERSION = 2


def rtc_state_load():
   # Returns a dict, always. Any failure - first boot, a firmware change
   # that scrambled the layout, a truncated write - is treated as "no state
   # yet" rather than an error, so a corrupt byte can never wedge the
   # device into a boot loop it cannot get out of.
   try:
      raw = machine.RTC().memory()
      if raw:
         st = json.loads(raw)
         if isinstance(st, dict) and st.get("v") == RTC_STATE_VERSION:
            return st
   except Exception as e:
      print("RTC state unreadable (%s), starting fresh" % e)
   return {}


def rtc_state_save(screen, next_poll, snap, cycle=0, boot=None):
   try:
      machine.RTC().memory(json.dumps({
         "v": RTC_STATE_VERSION,
         "screen": screen,
         "next_poll": next_poll,
         "snap": snap,
         "cycle": cycle,
         "boot": boot,
      }))
   except Exception as e:
      # Losing this costs a stale toggle view for one cycle, nothing more.
      print("RTC state save failed: %s" % e)


def make_snapshot(state, ip):
   # A JSON-safe, compact projection of everything the four screens need.
   # Times are stored as plain epoch ints rather than the 8-tuples
   # time.localtime() returns, because JSON would turn those into lists and
   # they would come back as lists, not tuples - a difference that would go
   # unnoticed until something indexed past the end.
   def epoch_of(tm):
      return time.mktime(tm) if tm is not None else None
   return {
      "sg": state["sg"],
      "trend": state["trend"],
      "insulin": state["active_insulin"],
      "upd": epoch_of(state["last_update_tm"]),
      "alarm": state["alarm_text"],
      "alarm_at": epoch_of(state["alarm_tm"]),
      "banner": state["banner"],
      "dst": dstDelta,
      "stats": state["stats"],
      "ip": ip,
      # The pump screen's fields. Without these a toggle wake would draw it
      # empty, since that path deliberately never touches the network.
      "batt": state["battery_pct"],
      "resu": state["reservoir_units"],
      "resp": state["reservoir_pct"],
      "sage": state["sage_hours"],
      "pat": state["patient"],
   }


def state_from_snapshot(snap):
   # Rebuild the dict draw_screen() expects. Missing/empty snapshot yields
   # a blank state, which every screen already renders as "no data".
   global dstDelta
   st = new_state()
   if not snap:
      return st
   st["sg"] = snap.get("sg")
   st["trend"] = snap.get("trend") or "NONE"
   st["active_insulin"] = snap.get("insulin")
   st["alarm_text"] = snap.get("alarm")
   st["banner"] = snap.get("banner")
   st["stats"] = snap.get("stats") or {}
   st["battery_pct"] = snap.get("batt")
   st["reservoir_units"] = snap.get("resu")
   st["reservoir_pct"] = snap.get("resp")
   st["sage_hours"] = snap.get("sage", 255)
   st["patient"] = snap.get("pat")
   dstDelta = snap.get("dst", 0)
   if snap.get("upd") is not None:
      st["last_update_tm"] = time.localtime(snap["upd"])
   if snap.get("alarm_at") is not None:
      st["alarm_tm"] = time.localtime(snap["alarm_at"])
   return st


#################################################
#
# Configuration storage: a plain JSON file on the user filesystem
#
#################################################

def config_write(cfg: dict):
   try:
      with open(CONFIG_FILE, "w") as f:
         json.dump(cfg, f)
   except OSError as e:
      print("Failed to write config file: %s" % e)


def config_read():
   try:
      with open(CONFIG_FILE) as f:
         return json.load(f)
   except (OSError, ValueError):
      return {}


# To clear a stuck config and force AP-mode setup again:
#   mpremote connect /dev/ttyACM0 exec "import os; os.remove('/flash/minimed_config.json')"


#################################################
#
# NTP time sync
#
#################################################

def ntp_sync(ntpserver):
   try:
      ntptime.host = ntpserver
      ntptime.settime()  # sets the RTC to UTC
      return True
   except Exception as e:
      print("NTP sync failed: %s" % e)
      return False


def local_now(timezone, dst_delta):
   return time.localtime(time.time() + (int(timezone) + dst_delta) * 3600)


#################################################
#
# HTTP client
#
# This firmware DOES provide an HTTP client (requests2), unlike the
# Inkplate build main.py was written for. This still uses a raw socket
# anyway, for one reason: settimeout() gives a hard, known upper bound on
# how long a cycle can block. On an unattended monitor a fetch that hangs
# forever is not a slow cycle, it is a dead device showing a stale reading
# with no way to notice - the failure mode this whole file's error handling
# exists to avoid. Carried over verbatim from main.py, where it is proven
# against this exact proxy.
#
#################################################

def http_get(host, port, path, timeout_s=30):
   addr = socket.getaddrinfo(host, port)[0][-1]
   s = socket.socket()
   s.settimeout(timeout_s)
   try:
      s.connect(addr)
      req = "GET /%s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n" % (path, host)
      s.send(req.encode())
      chunks = []
      while True:
         data = s.recv(1024)
         if not data:
            break
         chunks.append(data)
   finally:
      s.close()
   response = b"".join(chunks)
   header, _, body = response.partition(b"\r\n\r\n")
   try:
      status_code = int(header.split(b"\r\n", 1)[0].split()[1])
   except (IndexError, ValueError):
      status_code = 0
   return status_code, body


#################################################
#
# WIFI Access Point functions: a minimal captive config server, served to a
# phone/laptop browser during first-time (or failed-WiFi) setup
#
#################################################

def web_page_config(ntpserver,timezone,proxyport):
   html =  '<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd"> \n \
            <html><head><title>M5Ink Minimed Mon</title></head> \n \
            <body><table style="text-align: left; width: 400px; background-color: #2196F3; font-family: Helvetica,Arial,sans-serif; font-weight: bold; color: white;" border="0" cellpadding="2" cellspacing="2"> \n \
            <tbody><tr><td> \n \
            <span style="vertical-align: top; font-size: 48px;">M5Ink Minimed Mon</span><br> \n \
            <span style="font-size: 20px; color: rgb(204, 255, 255);">Configuration</span> \n \
            </td></tr></tbody></table><br> \n \
            <form action="/config"> \n \
            <table style="text-align: left; width: 400px; background-color: white; font-family: Helvetica,Arial,sans-serif; font-weight: bold; font-size: 14px;" border="0" cellpadding="2" cellspacing="3"><tbody> \n \
            <tr style="font-size: 18px; background-color: lightgrey"> \n \
            <td style="width: 200px;">Wifi parameters</td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">SSID<br><input type="text" id="fwifissid" name="fwifissid"></td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">Password<br><input type="text" id="fwifipass" name="fwifipass"></td> \n \
            </tbody></table><br> \n \
            <table style="text-align: left; width: 400px; background-color: white; font-family: Helvetica,Arial,sans-serif; font-weight: bold; font-size: 14px;" border="0" cellpadding="2" cellspacing="3"><tbody> \n \
            <tr style="font-size: 18px; background-color: lightgrey"> \n \
            <td style="width: 200px;">Time and date</td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">NTP server address<br><input type="text" id="fntpserver" name="fntpserver" value=%s></td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">Time Zone (h)<br><input type="text" id="ftimezone" name="ftimezone" value=%s></td> \n \
            </tbody></table><br> \n \
            <table style="text-align: left; width: 400px; background-color: white; font-family: Helvetica,Arial,sans-serif; font-weight: bold; font-size: 14px;" border="0" cellpadding="2" cellspacing="3"><tbody> \n \
            <tr style="font-size: 18px; background-color: lightgrey"> \n \
            <td style="width: 200px;">Carelink proxy</td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">IP address<br><input type="text" id="fproxyaddr" name="fproxyaddr"></td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">Port<br><input type="text" id="fproxyport" name="fproxyport" value=%s></td> \n \
            <tr style="vertical-align: top; background-color: rgb(230, 230, 255);"> \n \
            <td style="width: 300px;">Patient name (optional)<br><input type="text" id="fpatient" name="fpatient"></td> \n \
            </tbody></table><br> \n \
            <input type="submit" value="Save"> \n \
            </form></body></html>' % (ntpserver,timezone,proxyport)
   return html


def web_page_success():
   html =  '<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd"> \n \
            <html><head><title>M5Ink Minimed Mon</title></head> \n \
            <body><table style="text-align: left; width: 400px; background-color: #2196F3; font-family: Helvetica,Arial,sans-serif; font-weight: bold; color: white;" border="0" cellpadding="2" cellspacing="2"> \n \
            <tbody><tr><td> \n \
            <span style="vertical-align: top; font-size: 48px;">M5Ink Minimed Mon</span><br> \n \
            <span style="font-size: 20px; color: rgb(204, 255, 255);">Configuration</span> \n \
            </td></tr></tbody></table><br> \n \
            <table style="text-align: left; width: 400px; background-color: rgb(230, 230, 255); font-family: Helvetica,Arial,sans-serif; font-weight: bold; font-size: 14px;" border="0" cellpadding="2" cellspacing="3"><tbody> \n \
            <tr><td style="color: green; font-size: 18px;">Parameters updated successfully</td> \n \
            <tr><td style="color: grey">Restarting device with new configuration ...</td> \n \
            </tbody></table></body></html>'
   return html


def get_url_param(url,param):
   try:
      value = url.split("?")[1].split(param+"=")[1].split("&")[0]
   except IndexError:
      value = None
   return value


def do_ap_status(msg):
   # Silent by design, even though this board HAS a buzzer: AP mode means
   # somebody is standing in front of the device doing first-time setup, so
   # there is nobody to summon. The buzzer is reserved for pump alarms - if
   # it also chirped for routine setup steps it would train the caregiver to
   # ignore it, which is the one failure this feature exists to prevent.
   def _draw():
      gfx().fillScreen(WHITE)
      y = MARGIN
      lh = font_height(FONT_LABEL)
      for line in msg.split("\n"):
         # AP status strings are longer than this narrow panel fits, so
         # wrap rather than let them run off the edge (M5GFX clips
         # silently).
         for wrapped in wrap_text_to_width(line, PANEL_W - 2*MARGIN, FONT_LABEL):
            draw_text(wrapped, MARGIN, y, FONT_LABEL, BLACK)
            y += lh + 2
   # Full refresh: these are rare, discrete transitions during setup and a
   # clean panel matters more than speed when someone is reading
   # instructions off it.
   compose(_draw, full_refresh=True)


def do_access_point(ntpserver,timezone,proxyport):
   # Start access point
   ap = network.WLAN(network.AP_IF)
   ap.active(True)
   ap.config(essid=AP_SSID)
   ap.config(authmode=3, password='123456789')
   ap.config(max_clients=1)
   do_ap_status("Device configuration needed\nConnect to WIFI network\n%s" %(AP_SSID))

   # Wait for client to connect
   while ap.isconnected() == False:
       pass
   do_ap_status("WIFI connection established\nLoad address %s in web browser" % (AP_ADDR))

   # Get WIFI credentials via Web GUI
   s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
   s.bind((AP_ADDR, 80))
   s.listen(5)

   while True:
      # Get request
      conn,addr = s.accept()
      request = str(conn.recv(1024))
      rmethod  = request.split()[0]
      rurl     = request.split()[1]
      rheaders = request.split()[2]
      print("request: %s\n" % (request))
      print("rurl: %s\n" % (rurl))

      # Send response headers
      conn.send('HTTP/1.1 200 OK\n')
      conn.send('Content-Type: text/html\n')
      conn.send('Connection: close\n\n')

      if rurl.find("/config") != -1:
         # Get input parameters from request
         wifissid  = get_url_param(rurl, "fwifissid")
         wifipass  = get_url_param(rurl, "fwifipass")
         ntpserver = get_url_param(rurl, "fntpserver")
         timezone  = get_url_param(rurl, "ftimezone")
         proxyaddr = get_url_param(rurl, "fproxyaddr")
         proxyport = get_url_param(rurl, "fproxyport")
         patient   = get_url_param(rurl, "fpatient")
         if wifissid  != None and wifissid  != "" and \
            wifipass  != None and wifipass  != "" and \
            ntpserver != None and ntpserver != "" and \
            timezone  != None and timezone  != "" and \
            proxyaddr != None and proxyaddr != "" and \
            proxyport != None and proxyport != "":

            print("New configuration parameters received\n")
            # Send reboot page
            conn.sendall(web_page_success())
            conn.close()
            break

      # Send setup page
      conn.sendall(web_page_config(ntpserver,timezone,proxyport))
      conn.close()

   # Write WIFI credentials to config file
   config_write({
      "wifissid":  wifissid,
      "wifipass":  wifipass,
      "ntpserver": ntpserver,
      "timezone":  timezone,
      "proxyaddr": proxyaddr,
      "proxyport": proxyport,
      "patientname": patient,
   })
   print("New configuration parameters stored in config file\n")
   do_ap_status("New configuration parameters stored\nResetting device ...")

   # Reset device
   time.sleep_ms(2000)
   machine.reset()


#################################################
#
# Access configuration parameters
#
#################################################

def read_config():
   cfg = config_read()
   wifissid  = cfg.get('wifissid')
   wifipass  = cfg.get('wifipass')
   ntpserver = cfg.get('ntpserver')
   timezone  = cfg.get('timezone')
   proxyaddr = cfg.get('proxyaddr')
   proxyport = cfg.get('proxyport')
   # Optional on purpose. The proxy reports firstName, so a device that has
   # never been told a name still shows the right one; this only exists to
   # override it (a nickname, or two pumps in one house). Crucially it must
   # NOT join the test below - an existing device upgraded to this build has
   # no name in its config file, and forcing it into AP mode over a cosmetic
   # label would strand a working monitor until someone walked over to it.
   patient   = cfg.get('patientname')

   if proxyport == None:
      proxyport = DEFAULT_PROXY_PORT
   if ntpserver == None:
      ntpserver = DEFAULT_NTP_SERVER
   if timezone == None:
      timezone  = DEFAULT_TIME_ZONE

   if wifissid == None or wifipass == None or proxyaddr == None:
      print("Needed configuration parameters not found\n")
      do_access_point(ntpserver,timezone,proxyport)

   return (wifissid,wifipass,proxyaddr,proxyport,ntpserver,timezone,patient)


#################################################
#
# Connect to network
#
#################################################

# The WiFi radio is fully powered down by machine.deepsleep() and must
# cold-reassociate from scratch on EVERY wake, every 5 minutes - which can
# occasionally be slow. A short retry budget was observed on the Inkplate
# build to spuriously drop an already-correctly-configured device straight
# into do_access_point()'s blocking, physical-intervention-required config
# flow on a single transient connect hiccup - unacceptable for an
# unattended device that should just retry next cycle. Generous timeout
# below; more importantly, a connect failure here does NOT auto-trigger AP
# mode (AP mode is only for a config file that's missing credentials in the
# first place - see read_config()) - it just skips this cycle's fetch/draw
# and retries on the next wake instead.
WIFI_CONNECT_TIMEOUT_S = 25

def wlan_connect(wifissid, wifipass):
   wlan = network.WLAN(network.STA_IF)
   wlan.active(True)
   try:
      # wlan.connect() can itself raise OSError (e.g. "Wifi Internal Error")
      # rather than just failing to associate - observed on the Inkplate
      # build as a plausible cause of main() crashing outright shortly after
      # a cold radio init, leaving the device stuck showing stale content
      # with no self-recovery. Treat it the same as a plain connect timeout:
      # log and let the caller retry next cycle, never let it propagate.
      wlan.connect(wifissid, wifipass)
      deadline = time.ticks_add(time.ticks_ms(), WIFI_CONNECT_TIMEOUT_S*1000)
      while not wlan.isconnected() and time.ticks_diff(deadline, time.ticks_ms()) > 0:
         time.sleep_ms(250)
   except OSError as e:
      print("wlan.connect() raised %s" % e)
   if not wlan.isconnected():
      wlan.active(False)
      print("Failed to connect to WIFI network %s within %ds" % (wifissid, WIFI_CONNECT_TIMEOUT_S))
   return wlan


#################################################
#
# Data-formatting helpers
#
#################################################

def time_delta(tm,now,timezone):
   if tm != None and now != None:
      delta_min  = now[4] - tm[4]
      if delta_min < 0:
         delta_min += 60
      delta_hour = now[3] - tm[3]
      if delta_hour < 0:
         delta_hour += 24

      if delta_min == 0 and delta_hour == 0:
         delta_txt = "Now"
      elif delta_min > 15 or delta_hour > 1:
         delta_txt = "No data"
      else:
         delta_txt = str(delta_min)+" min ago"
   else:
      delta_txt = "---"
   return delta_txt


# Trend as (direction, count) rather than main.py's ASCII "^^^"/"vvv"
# strings. main.py had no choice - the Inkplate driver could only print
# glyphs from its built-in font. M5GFX has fillTriangle/fillRect, so the
# trend is drawn as real arrows (see draw_arrows()), which is both what was
# asked for and much easier to read at a glance than a row of carets.
TREND_ARROWS = {
   "UP_TRIPLE":   ("up", 3),
   "UP_DOUBLE":   ("up", 2),
   "UP":          ("up", 1),
   "NONE":        ("flat", 1),
   "DOWN":        ("down", 1),
   "DOWN_DOUBLE": ("down", 2),
   "DOWN_TRIPLE": ("down", 3),
}


def convert_datetimestr_to_epoch(datetimestr):
   # datetime string format: yyyy-mm-ddThh:mm:ss.000-00:00
   try:
      d  = datetimestr.split('.')[0].split('T')[0]
      t  = datetimestr.split('.')[0].split('T')[1]
      year = int(d.split('-')[0])
      mon  = int(d.split('-')[1])
      day  = int(d.split('-')[2])
      hour = int(t.split(':')[0])
      min  = int(t.split(':')[1])
      sec  = int(t.split(':')[2])
      return time.mktime((year,mon,day,hour,min,sec,0,0))
   except:
      return 0


def getFaultStr(faultId):
   try:
      faultStr = faultIdTable[faultIdMapping[faultId]]
   except KeyError:
      faultStr = "Unknown error code %s" % faultId
   return faultStr


#################################################
#
# Alarm handling - deliberately has no cross-cycle dedup bookkeeping.
# Re-showing the same alarm banner on every redraw while it's still recent
# (within ALARM_RECENCY_S) is the correct behavior for an ambient always-
# visible display - you want to see it whenever you glance at the device,
# not just once. It also means there is nothing that needs to survive a
# deep-sleep restart, which is what makes machine.deepsleep() straight-
# forward despite each wake re-running the whole script from scratch.
#
# THE BUZZER FOLLOWS THE SAME RULE, AND FOR THE SAME REASON: main() beeps
# once per cycle in which an alarm is showing, so a still-active alarm beeps
# again on the next wake ~5 minutes later. That is intentional, not a
# missing dedup - it is the whole reason this board was chosen over the
# Inkplate, whose header records "a caregiver not looking at the display
# gets no alert" as an accepted shortcoming. A caregiver who slept through
# or was out of earshot for the first beep gets another one. Because the
# beep is derived from the same state["alarm_text"] the banner is, an alarm
# that goes stale by the rules below stops beeping and stops showing in the
# same cycle - the two can never disagree, which is why no separate
# "should we beep" state exists.
#
# The Carelink proxy's lastAlarm field reports the most recent alarm
# NOTIFICATION, not whether the underlying condition is still true - it
# does not update or clear itself just because glucose has since recovered.
# Relying on ALARM_RECENCY_S alone (as an earlier version of the Inkplate
# file did) left a "Low Sensor Glucose" banner showing for up to 15 minutes
# after glucose had already returned to a normal reading, confirmed on real
# hardware. That mattered on a silent display; it matters more here, where
# the same staleness would also mean a spurious beep in the night. For the
# fault IDs below - all directly tied to a low/high glucose *threshold*,
# unlike e.g. a reservoir or battery alarm which this script has no
# independent way to verify - get_alarm_text() additionally corroborates
# against the CURRENT reading and treats the notification as stale once
# glucose is back on the right side of the threshold, even if still within
# the recency window.
#
#################################################

LOW_GLUCOSE_FAULT_IDS  = {"802", "805", "809", "810", "814", "815", "827"}
HIGH_GLUCOSE_FAULT_IDS = {"816", "817", "823"}


def get_alarm_text(lastAlarm, timezone, dst_delta, current_sg):
   # Returns (message, local_tm) for a still-recent AND still-current alarm,
   # else (None, None). local_tm is the alarm's OWN occurrence time (not
   # "now"), so draw_screen() can show caregivers when it actually happened -
   # useful for telling a fresh alarm apart from one that's still within its
   # ALARM_RECENCY_S re-announce window.
   #
   # Unlike lastConduitUpdateServerDateTime (a true UTC epoch in ms),
   # lastAlarm["dateTime"]'s digits are ALREADY local wall-clock time,
   # confirmed on real hardware: displaying them with a further timezone/DST
   # offset ADDED showed a time 2 hours ahead of the real CEST clock.
   # convert_datetimestr_to_epoch() parses those digits with no timezone
   # awareness at all, so its result already directly matches local
   # wall-clock time for display - but comparing it as-is against
   # time.time() (true UTC) for the recency check silently inflated the
   # effective "still recent" window by the full offset (2h for CEST)
   # instead of the intended ALARM_RECENCY_S (15 min): a low/high-glucose
   # alarm could stay shown for ~2h15m after it actually fired, not 15
   # minutes, on top of the separate current-glucose check below. Fixed by
   # shifting the naively-parsed value BACK by the offset to get a true UTC
   # epoch for the comparison, while still displaying the original
   # (already-local) value directly.
   try:
      naive_local_epoch = convert_datetimestr_to_epoch(lastAlarm["dateTime"])
      # "not naive_local_epoch" also guards against a parse failure (which
      # returns 0, see convert_datetimestr_to_epoch) ever being mistaken
      # for a recent alarm - which here would also mean a spurious beep.
      if not naive_local_epoch:
         return None, None
      offset_s = (int(timezone) + dst_delta) * 3600
      utc_epoch = naive_local_epoch - offset_s
      if utc_epoch > (time.time() - ALARM_RECENCY_S):
         canonical_id = faultIdMapping.get(lastAlarm["faultId"], lastAlarm["faultId"])
         if current_sg is not None:
            if canonical_id in LOW_GLUCOSE_FAULT_IDS and current_sg > HYPO_THRESHOLD_MGDL:
               return None, None  # glucose has recovered - notification is stale
            if canonical_id in HIGH_GLUCOSE_FAULT_IDS and current_sg < HYPER_THRESHOLD_MGDL:
               return None, None  # glucose has come back down - notification is stale
         return getFaultStr(lastAlarm["faultId"]), time.localtime(naive_local_epoch)
   except (KeyError, TypeError):
      pass
   return None, None


#################################################
#
# Buzzer
#
#################################################

BUZZER_FREQ_HZ = 2000   # near the resonant peak of a piezo this size
BUZZER_MS      = 400    # "one loud beep" - long enough to carry from another room


def beep():
   # Wrapped broadly: an alarm cycle that fails to beep must still go on to
   # draw the alarm banner. Losing the sound is bad; losing the visible
   # alarm as well because the buzzer raised would be worse.
   try:
      M5.Speaker.begin()
      M5.Speaker.setVolume(255)
      M5.Speaker.tone(BUZZER_FREQ_HZ, BUZZER_MS)
      # tone() queues the sound on the speaker task and returns immediately,
      # so the beep must be waited out here. Without this wait the
      # machine.deepsleep() at the end of main() would cut power to the
      # speaker mid-note and the alarm would be inaudible - a silent
      # failure that looks completely fine in the logs.
      deadline = time.ticks_add(time.ticks_ms(), BUZZER_MS + 500)
      while M5.Speaker.isPlaying() and time.ticks_diff(deadline, time.ticks_ms()) > 0:
         time.sleep_ms(20)
      time.sleep_ms(50)
   except Exception as e:
      print("Buzzer failed: %s" % e)


#################################################
#
# Pump data polling - builds a plain state dict, drawn by draw_screen()
#
#################################################

def new_state():
   # battery_pct/reservoir_units/reservoir_pct/sage_hours are not on the
   # main screen - a caregiver glancing at it wants glucose, not supplies -
   # but they are what the pump screen is made of. sage_hours keeps 255 as
   # its "unknown" sentinel, which is what the pump reports before a sensor
   # has settled; it must render as "--" rather than as 255 hours of life.
   return {
      "haveData": False,
      "battery_pct": None,
      "reservoir_units": None,
      "reservoir_pct": None,
      "patient": None,
      "sensor_ok": False,
      "sage_hours": 255,
      "sage_state": "",
      "sg": None,
      "trend": "NONE",
      "active_insulin": None,
      "last_update_tm": None,
      "banner": None,
      "alarm_text": None,
      "alarm_tm": None,  # local time the alarm itself occurred, not "now"
      # 24h glucose distribution, shown on the stats screen. Same four
      # Carelink fields the sibling M5Stack project puts on its screen 2.
      "stats": {},
   }


def handle_pumpdataupdate(proxyaddr, proxyport, timezone):
   global dstDelta
   state = new_state()

   try:
      status_code, body = http_get(proxyaddr, int(proxyport), API_URL)
   except (OSError, Exception) as e:
      print("Pump data fetch failed: %s" % e)
      return state

   if status_code != 200:
      print("Pump data fetch returned status %s" % status_code)
      return state

   try:
      data = json.loads(body)
   except ValueError:
      print("Pump data fetch returned invalid JSON")
      return state

   # Timestamp/DST bookkeeping and alarm handling run unconditionally - the
   # alarm banner and beep (safety-relevant) shouldn't depend on any other
   # field parsing succeeding.
   try:
      dstDelta = 1 if data["clientTimeZoneName"].lower().find("summer")>-1 else 0
      # INTEGER division, not "/1000". This MicroPython build has
      # single-precision floats (confirmed on device: 1000000000001.0 reads
      # back as 1000000000000.0), and lastConduitUpdateServerDateTime is a
      # Unix time in MILLISECONDS - around 1.79e12 today, where a float32's
      # 24-bit mantissa only resolves to ~64 seconds. Written as "/1000" the
      # timestamp came back up to a minute wrong (measured: a payload
      # stamped 11:51:00 was decoded as 11:50:56), and since time_delta()
      # below compares whole minute fields, that silently reported "5 min
      # ago" for a 4-minute-old reading. json.loads() gives this field as an
      # int and MicroPython ints are arbitrary precision, so "//" keeps the
      # whole computation exact and never converts to float at all.
      unix_epoch_s = data["lastConduitUpdateServerDateTime"] // 1000 - EPOCH_ADJUST_S
      state["last_update_tm"] = time.localtime(
         unix_epoch_s + (int(timezone)+dstDelta)*3600)
      try:
         current_sg = data["lastSG"]["sg"]
         current_sg = current_sg if current_sg > 0 else None
      except (KeyError, TypeError):
         current_sg = None
      state["alarm_text"], state["alarm_tm"] = get_alarm_text(data["lastAlarm"], timezone, dstDelta, current_sg)
   except (KeyError, TypeError):
      pass

   try:
      state["haveData"] = bool(data["conduitInRange"] and data["conduitMedicalDeviceInRange"])
   except (KeyError, TypeError):
      state["haveData"] = False

   try:
      if state["haveData"]:
         state["battery_pct"] = data["pumpBatteryLevelPercent"]
         state["reservoir_units"] = data["reservoirRemainingUnits"]
         state["reservoir_pct"] = data.get("reservoirLevelPercent")
         state["sage_hours"] = data["sensorDurationHours"]
         state["sage_state"] = data["sensorState"]
      state["sensor_ok"] = bool(data.get("conduitSensorInRange"))
      # The proxy already knows who the pump belongs to, so the configured
      # name is only a label to override it with - see draw_pump_screen().
      state["patient"] = data.get("firstName")

      if state["haveData"] and data["therapyAlgorithmState"]["autoModeShieldState"] != "FEATURE_OFF":
         state["trend"] = data["lastSGTrend"]
      else:
         state["trend"] = "NONE"

      lastSG = data["lastSG"]["sg"]
      state["sg"] = lastSG if lastSG > 0 else None

      if state["haveData"]:
         state["active_insulin"] = round(data["activeInsulin"]["amount"], 1)
   except (KeyError, TypeError):
      pass

   # 24h distribution for the stats screen. Parsed in its own try block, and
   # each field individually, so that a proxy version that drops or renames
   # one of them costs a single "--" on a secondary screen rather than
   # taking the whole stats view (or worse, the main reading) down with it.
   stats = {}
   for key, field in (("above", "aboveHyperLimit"),
                      ("inrange", "timeInRange"),
                      ("below", "belowHypoLimit"),
                      ("avg", "averageSG")):
      try:
         v = data[field]
         stats[key] = v if v is not None else None
      except (KeyError, TypeError):
         stats[key] = None
   state["stats"] = stats

   try:
      systemStatus = data["systemStatusMessage"]
      if systemStatus and systemStatus != "NO_ERROR_MESSAGE":
         state["banner"] = systemStatus.replace("_", " ")
   except (KeyError, TypeError):
      pass

   try:
      pumpBanner = data["pumpBannerState"][0]["type"]
      # A pump delivery banner (suspend/bg-required/etc.) takes priority
      # over the generic systemStatusMessage line above if both are set.
      state["banner"] = pumpBanner.replace("_", " ")
   except (KeyError, IndexError, TypeError):
      pass

   return state


#################################################
#
# Display: single consolidated view, redrawn fully every cycle.
#
# Layout, top to bottom on the 200x200 square panel:
#
#   +--------------------------------------+
#   |  142  ^^      <- glucose + trend     |  inverted if out of range
#   |  mg/dL                               |
#   |  ----------------------------------  |
#   |  Act. insulin            2.4 U       |
#   |  Updated 4 min ago         09:55     |
#   |                                      |
#   |  ##################################  |  alarm: two bottom lines,
#   |  ## Low Sensor Glucose. Check BG ##  |  reverse (white on black)
#   +--------------------------------------+
#
# The square aspect is what makes this vertical stack possible: main.py had
# to squeeze active insulin into a side column beside the glucose number
# because 104px of height left nothing below it.
#
# The alarm block is anchored to the BOTTOM edge and sized to its content,
# so the rows above never shift depending on whether an alarm is showing -
# a caregiver's eye learns where the glucose number and the "Updated" line
# live, and having them jump when an alarm appears would undo that.
#
#################################################

def draw_arrows(x, y, w, h, direction, count, color):
   # Real arrow shapes rather than caret characters. M5GFX gives us
   # fillTriangle, so the head is one call and the shaft another.
   for i in range(count):
      ax = x + i * (w + 3)
      cx = ax + w // 2
      shaft_w = max(3, w // 3)
      if direction == "flat":
         # Horizontal, pointing right - the "steady" trend.
         head_w = w // 2
         cy = y + h // 2
         gfx().fillRect(ax, cy - shaft_w//2, w - head_w, shaft_w, color)
         gfx().fillTriangle(ax + w - head_w, cy - h//4,
                             ax + w - head_w, cy + h//4,
                             ax + w,          cy, color)
      elif direction == "up":
         head_h = h // 2
         gfx().fillTriangle(ax, y + head_h, ax + w, y + head_h, cx, y, color)
         gfx().fillRect(cx - shaft_w//2, y + head_h, shaft_w, h - head_h, color)
      else:  # down
         head_h = h // 2
         gfx().fillTriangle(ax, y + h - head_h, ax + w, y + h - head_h, cx, y + h, color)
         gfx().fillRect(cx - shaft_w//2, y, shaft_w, h - head_h, color)


def draw_kv_row(y, label, label_font, value, value_font, color, bg=WHITE):
   # Label left, value right-aligned to the right margin, bottoms aligned so
   # a small label sits on the same baseline as a larger value. The value is
   # placed first and the label truncated to whatever is left, because if
   # something has to be clipped it should be the constant label rather than
   # the number that actually changes.
   vh = font_height(value_font)
   lh = font_height(label_font)
   value_w = text_width(value, value_font)
   value_x = PANEL_W - MARGIN - value_w
   draw_text(value, value_x, y, value_font, color, bg)
   label_max_w = value_x - MARGIN - 4
   label = truncate_to_width(label, label_max_w, label_font)
   draw_text(label, MARGIN, y + (vh - lh), label_font, color, bg)
   return vh


def draw_screen(state):
   # Refresh waveform and canvas setup belong to draw_current_screen(),
   # which is the only thing that knows whether this redraw is a scheduled
   # one or somebody flicking the switch. This function just composes.
   gfx().fillScreen(WHITE)

   banner_text = state["alarm_text"] or state["banner"]
   has_banner = bool(banner_text)

   # --- Glucose number, with the trend arrows to its right ---------------
   #
   # The reading is drawn large whenever the bottom strip is not needed for
   # an alarm, and shrinks back only to make room for one. Nothing else on
   # this screen competes for attention at a distance, so with ~90px of
   # otherwise-idle panel there is no reason to render the one number the
   # device exists for at half that size. When an alarm IS showing the
   # trade reverses: the message text matters more than another 40px of
   # digit, and the smaller figure is still perfectly legible.
   #
   # FONT_GLUCOSE_BIG is a smaller font at double scale rather than the
   # tallest font at double scale, which would be 100px: at 3 digits that
   # leaves too little width beside it for the trend arrows to stay
   # readable, and the arrows are what turn a number into a direction.
   sg_txt = str(state["sg"]) if state["sg"] is not None else "---"
   out_of_range = state["sg"] is not None and (
      state["sg"] > HYPER_THRESHOLD_MGDL or state["sg"] < HYPO_THRESHOLD_MGDL)

   if has_banner:
      g_font, g_size = FONT_GLUCOSE, 1
   else:
      g_font, g_size = FONT_GLUCOSE_BIG, GLUCOSE_BIG_SIZE

   gh = font_height(g_font, g_size)
   gy = 6
   band_y = gy - 4
   band_h = gh + 8

   # No red ink on this panel (the Inkplate coloured this number red), so
   # out-of-range is signalled by inverting the whole glucose band. The band
   # spans the full panel width rather than hugging the digits, so it reads
   # as a solid alert block from across a room rather than as a slightly
   # odd-looking number.
   fg, bg = BLACK, WHITE
   if out_of_range:
      gfx().fillRect(0, band_y, PANEL_W, band_h, BLACK)
      fg, bg = WHITE, BLACK

   draw_text(sg_txt, MARGIN, gy, g_font, fg, bg, size=g_size)

   arrow_x = MARGIN + text_width(sg_txt, g_font, g_size) + 10
   direction, count = TREND_ARROWS.get(state["trend"], ("flat", 1))
   avail = PANEL_W - MARGIN - arrow_x
   arrow_w = 22
   # Shrink rather than let a 3-arrow trend beside a 3-digit reading run off
   # the right edge (M5GFX clips silently, so it would just look wrong).
   while count * (arrow_w + 3) > avail and arrow_w > 7:
      arrow_w -= 1
   draw_arrows(arrow_x, gy + 4, arrow_w, gh - 8, direction, count, fg)

   unit_y = band_y + band_h + 2
   draw_text("mg/dL", MARGIN, unit_y, FONT_UNIT, BLACK)

   sep_y = unit_y + font_height(FONT_UNIT) + 6
   gfx().drawLine(MARGIN, sep_y, PANEL_W - MARGIN, sep_y, BLACK)

   # --- Active insulin, then the update time ----------------------------
   #
   # With no alarm the bottom strip is free, so these two rows sit against
   # the bottom edge instead of floating directly under the separator -
   # which both balances the taller glucose figure above and keeps the
   # "Updated" line in the same place it occupies on the alarm layout, so
   # the eye does not have to hunt for it depending on the pump's mood.
   row_a_h = font_height(FONT_VALUE)
   row_b_h = font_height(FONT_LABEL)
   if has_banner:
      row_a_y = sep_y + 8
   else:
      row_a_y = PANEL_H - MARGIN - row_b_h - 6 - row_a_h
   insulin_txt = "%.1f U" % state["active_insulin"] if state["active_insulin"] is not None else "-- U"
   draw_kv_row(row_a_y, "Act. insulin", FONT_LABEL, insulin_txt, FONT_VALUE, BLACK)

   # The clock value here is the moment THIS PANEL REFRESH happened (now),
   # not the CGM reading's own timestamp - "Updated 4 min ago | 09:55" means
   # the reading was from 9:51 but this display last redrew itself at 9:55.
   # Since the panel only redraws once per ~5-minute cycle, this whole row
   # is computed once and then stays static until the next cycle, by
   # construction (e-paper doesn't tick live). It earns its space twice
   # over: you don't have to wait out a refresh to know whether what's
   # showing is current, and a timestamp that stops advancing is how you'd
   # notice the device's battery died.
   #
   # Both halves are the small label font, unlike the insulin row whose
   # value is larger: at the bigger size the clock is wide enough to squeeze
   # the label past its truncation point, and the label is where the
   # staleness lives - "Updated 4 min ago" degrading to "Updated 4 ..."
   # would throw away the more important half of the row to emphasise the
   # less important one. An insulin figure is a value worth enlarging; a
   # wall-clock time is not.
   row_b_y = row_a_y + row_a_h + 6
   if state["last_update_tm"] is not None:
      now = local_now(current_timezone[0], dstDelta)
      delta_txt = time_delta(state["last_update_tm"], now, current_timezone[0])
      draw_kv_row(row_b_y, "Updated %s" % delta_txt, FONT_LABEL,
                  "%02d:%02d" % (now[3], now[4]), FONT_LABEL, BLACK)
   else:
      draw_kv_row(row_b_y, "Updated", FONT_LABEL, "No data", FONT_LABEL, BLACK)

   # --- Alarm / pump banner: the two bottom lines, in reverse -----------
   if banner_text:
      # For a genuine pump alarm (not the generic systemStatusMessage/
      # pumpBannerState banner, neither of which carries a comparable
      # timestamp), append when it actually occurred - lets a caregiver tell
      # a fresh alarm apart from one still showing only because it's within
      # ALARM_RECENCY_S of its own occurrence, not "now". This is also what
      # distinguishes a repeat beep for an ongoing alarm from a new one.
      if state["alarm_text"] and state["alarm_tm"] is not None:
         banner_text = "%s (%02d:%02d)" % (banner_text, state["alarm_tm"][3], state["alarm_tm"][4])

      # Two lines were asked for, and two lines is also what the fault
      # strings need: the longest entries in faultIdTable run past 50
      # characters, which no single line of this panel holds at any legible
      # size. Prefer the larger font when the message fits in two lines of
      # it, and drop to the smaller one otherwise so a long message is shown
      # in FULL rather than truncated. Showing all of "Suspend Before Low.
      # Delivery Stopped. Check BG" small beats showing "Suspend Bef..."
      # large - the tail of these strings is the actionable half.
      inner_w = PANEL_W - 2*MARGIN - 4
      font = FONT_ALARM_L
      lines = wrap_text_to_width(banner_text, inner_w, font)
      if len(lines) > 2:
         font = FONT_ALARM_S
         lines = wrap_text_to_width(banner_text, inner_w, font)
      # Still too long even at the smaller size (only the very longest
      # faults) - truncate the second line rather than silently lose line 3.
      if len(lines) > 2:
         lines = [lines[0], truncate_to_width(" ".join(lines[1:]), inner_w, font)]

      line_h = font_height(font)
      bar_h = 4 + 2*line_h + 2 + 4
      bar_y = PANEL_H - bar_h
      gfx().fillRect(0, bar_y, PANEL_W, bar_h, BLACK)
      ty = bar_y + 4
      for line in lines:
         draw_text(line, MARGIN + 2, ty, font, WHITE, BLACK)
         ty += line_h + 2


def draw_screen_header(title):
   # Shared chrome for the two secondary screens. The main screen has no
   # header on purpose - there, every pixel of the top band belongs to the
   # glucose figure, and a caregiver glancing over needs to recognise that
   # view instantly rather than read a label to find out which screen is up.
   draw_text(title, MARGIN, 4, FONT_LABEL, BLACK)
   y = 4 + font_height(FONT_LABEL) + 3
   gfx().drawLine(MARGIN, y, PANEL_W - MARGIN, y, BLACK)
   return y + 7


def draw_stats_screen(state):
   gfx().fillScreen(WHITE)
   y = draw_screen_header("GLUCOSE, LAST 24 H")
   stats = state.get("stats") or {}

   def pct(key):
      v = stats.get(key)
      return "--" if v is None else "%d%%" % int(v)

   # A stacked bar makes the split readable without reading any numbers,
   # which is the whole point of a summary screen. With no colour to work
   # with, the three segments are distinguished by fill: below-target is
   # solid black, in-target is hatched, above-target is left open. That
   # ordering is deliberate - low glucose is the dangerous end, so it gets
   # the heaviest ink and is impossible to miss in peripheral vision.
   below, inrange, above = stats.get("below"), stats.get("inrange"), stats.get("above")
   bar_h = 20
   bar_w = PANEL_W - 2*MARGIN
   if None not in (below, inrange, above):
      total = below + inrange + above
      if total > 0:
         x = MARGIN
         # Give the last segment the rounding remainder so the segments
         # always exactly fill the bar - computing each independently
         # leaves a stray 1-2px gap that reads as a rendering fault.
         w_below = int(bar_w * below / total)
         w_in    = int(bar_w * inrange / total)
         w_above = bar_w - w_below - w_in
         gfx().fillRect(x, y, w_below, bar_h, BLACK)
         x += w_below
         for hx in range(x, x + w_in, 3):     # hatch = in target
            gfx().drawLine(hx, y, hx, y + bar_h - 1, BLACK)
         x += w_in
         gfx().fillRect(x, y, w_above, bar_h, WHITE)
         gfx().drawRect(MARGIN, y, bar_w, bar_h, BLACK)
   else:
      gfx().drawRect(MARGIN, y, bar_w, bar_h, BLACK)
   y += bar_h + 8

   rows = (
      ("In target",  pct("inrange")),
      ("Above %d" % HYPER_THRESHOLD_MGDL, pct("above")),
      ("Below %d" % HYPO_THRESHOLD_MGDL,  pct("below")),
   )
   for label, value in rows:
      y += draw_kv_row(y, label, FONT_LABEL, value, FONT_VALUE, BLACK) + 4

   avg = stats.get("avg")
   avg_txt = "-- mg/dL" if avg is None else "%d mg/dL" % int(avg)
   y += 2
   draw_kv_row(y, "Average", FONT_LABEL, avg_txt, FONT_UNIT, BLACK)


def draw_pump_screen(state, cfg):
   # Between glucose and the technical screen: everything about the pump
   # itself that a caregiver might need to plan around - is there insulin
   # left, is the sensor about to expire, is the battery about to die.
   # None of it is urgent enough for the main screen, all of it is the kind
   # of thing you want to know before leaving the house.
   gfx().fillScreen(WHITE)
   y = draw_screen_header("PUMP & SENSOR")

   patient = cfg[5] or state.get("patient") or "--"

   units = state.get("reservoir_units")
   pct = state.get("reservoir_pct")
   if units is None:
      insulin = "--"
   elif pct is None:
      insulin = "%d U" % round(units)
   else:
      insulin = "%d U  %d%%" % (round(units), pct)

   # 255 is the pump's "no sensor / not settled yet" sentinel, not a life
   # of ten and a half days.
   sage = state.get("sage_hours")
   if sage is None or sage >= 255:
      sensor = "--"
   elif sage >= 48:
      sensor = "%dd %dh" % (sage // 24, sage % 24)
   else:
      sensor = "%d h" % sage

   batt = state.get("battery_pct")
   pump_batt = "%d%%" % batt if batt is not None else "--"

   # Values in the larger font: this screen has five rows where the info
   # screen has eight, so the room is there and these are numbers someone
   # reads across a room rather than settings they lean in to check.
   rows = (
      ("Patient",   patient),
      ("Insulin",   insulin),
      ("Sensor",    sensor),
      ("Pump batt", pump_batt),
   )
   for label, value in rows:
      h = draw_kv_row(y, label, FONT_LABEL, value, FONT_VALUE, BLACK)
      y += h + 6


def fmt_runtime(start):
   # Hours, because the question this answers is "how many hours does a
   # charge last" - days would round away exactly the resolution wanted.
   if not start:
      return "--"
   secs = time.time() - start
   if secs < 0:
      return "--"
   hours = secs / 3600.0
   return "%.1f h" % hours if hours < 100 else "%d h" % int(hours)


def draw_info_screen(state, cfg, ip):
   gfx().fillScreen(WHITE)
   y = draw_screen_header("DEVICE & NETWORK")
   # ntpserver/timezone are still in cfg (the other screens and the config
   # page use them); this screen no longer shows them.
   wifissid, proxyaddr, proxyport, _ntp, _tz, _patient = cfg

   try:
      batt = "%d%%" % M5.Power.getBatteryLevel()
   except Exception:
      batt = "--"

   # Battery first: it is the only line here that can change on its own and
   # the only one that predicts the device silently dying, which on a
   # monitor whose whole job is to be trusted at a glance matters more than
   # any of the static settings below it.
   if time.time() > TIME_VALID_EPOCH:
      now = local_now(current_timezone[0], dstDelta)
      clock = "%02d:%02d" % (now[3], now[4])
   else:
      clock = "--"

   # NTP server and timezone used to sit here. They were dropped rather than
   # squeezed: both are write-once settings that can be read back from the
   # config page, whereas runtime and battery change on their own and are
   # the two numbers someone actually comes to this screen to find.
   rows = (
      ("Battery",  batt),
      ("Runtime",  fmt_runtime(session_start[0])),
      ("Time",     clock),
      ("WiFi",     wifissid or "--"),
      ("IP",       ip or "--"),
      ("Proxy",    proxyaddr or "--"),
      ("Port",     str(proxyport or "--")),
      ("Version",  "V%s" % VERSION),
   )
   lh = font_height(FONT_LABEL)
   for label, value in rows:
      # These values (an SSID, an IPv4 address, a hostname) are the one
      # place on any screen where user-supplied text of unbounded length is
      # rendered, so they are right-aligned and truncated rather than
      # trusted to fit.
      draw_kv_row(y, label, FONT_LABEL, value, FONT_LABEL, BLACK)
      y += lh + 5


# How often a scheduled refresh uses the slow full-clear waveform.
#
# The full refresh (the visible flash to black and back) is NOT required on
# every wake - EPD_FAST writes the same image without it. What it is
# required for is ghosting: fast waveforms leave a faint residue of previous
# content, and on a display that redraws the same few shapes in the same few
# places all day that residue accumulates into a permanent shadow. So the
# flash is paid periodically rather than every cycle: at one poll every 5
# minutes, 12 cycles is roughly hourly, which keeps the panel clean while
# leaving the other 11 refreshes silent and flash-free. Interactive redraws
# from the toggle are never full - someone is watching those.
FULL_REFRESH_EVERY = 12


def draw_current_screen(screen, state, cfg, ip, full_refresh=False):
   if screen == SCREEN_STATS:
      compose(lambda: draw_stats_screen(state), full_refresh)
   elif screen == SCREEN_PUMP:
      compose(lambda: draw_pump_screen(state, cfg), full_refresh)
   elif screen == SCREEN_INFO:
      compose(lambda: draw_info_screen(state, cfg, ip), full_refresh)
   else:
      compose(lambda: draw_screen(state), full_refresh)


#################################################
#
# Toggle switch
#
#################################################

def arm_toggle_wake():
   # Both directions advance a screen, which needs both of the ESP32's GPIO
   # wake sources - see the BTN_UP_PIN comment for why one is not enough.
   # Verified on hardware: flicking up reports wake_reason 2 (EXT0) and down
   # reports 3 (EXT1). Non-fatal if it fails - the device then still wakes
   # on its timer and simply stops responding to the toggle, which is a
   # degraded monitor rather than a dead one.
   # A pin that is ALREADY low must not be armed. These wake sources are
   # level-triggered, not edge-triggered, so arming a pin that is currently
   # held down means deepsleep() returns immediately, every time: the device
   # would spin through wake/redraw/sleep as fast as it can boot, never
   # reaching its next poll and flattening the battery in hours. That is a
   # real state, not a hypothetical - it is what a switch left resting
   # off-centre, or a failed contact shorted to ground, looks like. Skipping
   # the stuck pin costs that one direction until it is released, and the
   # other direction and the timer keep working.
   try:
      import esp32
      if machine.Pin(BTN_UP_PIN, machine.Pin.IN).value():
         esp32.wake_on_ext0(pin=machine.Pin(BTN_UP_PIN, machine.Pin.IN),
                            level=esp32.WAKEUP_ALL_LOW)
      else:
         print("GPIO%d held low, not arming ext0" % BTN_UP_PIN)
      if machine.Pin(BTN_DOWN_PIN, machine.Pin.IN).value():
         esp32.wake_on_ext1(pins=(machine.Pin(BTN_DOWN_PIN, machine.Pin.IN),),
                            level=esp32.WAKEUP_ALL_LOW)
      else:
         print("GPIO%d held low, not arming ext1" % BTN_DOWN_PIN)
   except Exception as e:
      print("Toggle wake arming failed: %s" % e)


def toggle_pressed():
   # Active low, confirmed by probing: every pin reads 1 at rest.
   try:
      return (machine.Pin(BTN_UP_PIN, machine.Pin.IN).value() == 0 or
              machine.Pin(BTN_DOWN_PIN, machine.Pin.IN).value() == 0)
   except Exception:
      return False


# Presses are latched by an interrupt rather than discovered by polling.
# Redrawing this panel blocks for a noticeable fraction of a second, and a
# poll loop can only look at the pins between draws - so a flick that landed
# while the previous screen was still being written was simply lost, and the
# device felt like it was ignoring the switch exactly when someone was
# clicking through it fastest. The IRQ fires regardless of what the main
# thread is doing, so the press survives the draw and is acted on the moment
# it finishes.
_toggle_latch = [False]


def _toggle_isr(pin):
   # Keep this trivial - it runs in interrupt context.
   _toggle_latch[0] = True


def install_toggle_irq():
   try:
      for gp in (BTN_UP_PIN, BTN_DOWN_PIN):
         machine.Pin(gp, machine.Pin.IN).irq(
            trigger=machine.Pin.IRQ_FALLING, handler=_toggle_isr)
   except Exception as e:
      # Falls back to plain polling, which still works between draws.
      print("Toggle IRQ install failed: %s" % e)


def toggle_take():
   # True if the switch has been operated since the last call, whether or
   # not it is still held now.
   if _toggle_latch[0]:
      _toggle_latch[0] = False
      return True
   return False


# How long to stay awake after a toggle press, watching for another one.
# Waking this board from deep sleep costs about 4 seconds of firmware boot
# before any of this file's code runs (measured on the serial console), so
# sleeping immediately would put that latency in front of every single
# screen change. Staying awake briefly makes a run of flicks redraw at
# e-paper speed instead, and costs a few seconds of active current only
# when someone is actually standing at the device. 15s rather than a couple
# of seconds because the window is really "how long may someone think before
# the device gives up on them" - long enough to read a screen, consider it,
# and flick on. It does not delay the glucose poll: sleep_until() sleeps only
# until the next poll is due, so time spent awake here comes out of the sleep
# that followed it, never out of the update schedule.
TOGGLE_AWAKE_MS = 15000

# There is deliberately NO equivalent window after a scheduled refresh.
# A poll runs ~283 times a day whether or not anybody is there, so a window
# waiting for a press was over half the awake time in each cycle, burned at
# 3am as much as at noon. Dropping it took the cycle from 11.3s awake to
# ~5.4s - close to halving the daily awake time, measured on hardware.
#
# Nothing is lost but latency: the toggle still wakes the board out of deep
# sleep (ext0/ext1, armed in sleep_until), so a flick right after a refresh
# costs the ~2.7s boot before the screen changes instead of being instant.
# Every flick after that one is instant, because a toggle wake DOES open a
# window - TOGGLE_AWAKE_MS above.

# Bounds that exist purely so no switch fault can keep the device awake.
# A held, wedged or chattering contact must degrade to "the screens stop
# responding until the next scheduled wake", never to "the monitor stops
# polling", which is indistinguishable from a dead device.
# Scaled with TOGGLE_AWAKE_MS so the cap still allows a dozen presses rather
# than half a dozen; it is a fault guard, not a usage budget.
TOGGLE_SESSION_MAX_MS = 180000  # absolute cap on one awake session
TOGGLE_RELEASE_MAX_MS = 3000    # give up waiting for the switch to spring back
TOGGLE_DEBOUNCE_MS    = 120     # contact settle after each accepted press


def run_toggle_session(screen, state, cfg, ip):
   # Returns the screen index left on display. Each press advances one
   # screen and restarts the window, so holding a conversation with the
   # device never drops back to sleep mid-flick.
   install_toggle_irq()
   toggle_take()  # discard the press that woke us; it is already accounted for
   deadline = time.ticks_add(time.ticks_ms(), TOGGLE_AWAKE_MS)
   # Independent of the per-press deadline above, which every press extends.
   # Without this cap a switch held down, wedged, or chattering against a
   # failing contact would keep re-arming that deadline forever and the
   # device would never sleep again - the same never-polls-again lockup the
   # tail of main() guards against, just reached a different way.
   hard_stop = time.ticks_add(time.ticks_ms(), TOGGLE_SESSION_MAX_MS)
   while (time.ticks_diff(deadline, time.ticks_ms()) > 0 and
          time.ticks_diff(hard_stop, time.ticks_ms()) > 0):
      if toggle_take() or toggle_pressed():
         screen = (screen + 1) % SCREEN_COUNT
         # Wait for release BEFORE drawing, so that holding the switch does
         # not queue a second advance, and so the latch cleared afterwards
         # only discards this same press rather than a genuine new one.
         # BOUNDED: a stuck-low pin must not park the device here forever.
         release_by = time.ticks_add(time.ticks_ms(), TOGGLE_RELEASE_MAX_MS)
         while toggle_pressed() and time.ticks_diff(release_by, time.ticks_ms()) > 0:
            time.sleep_ms(10)
         toggle_take()
         draw_current_screen(screen, state, cfg, ip, full_refresh=False)
         # Settle time: these contacts bounce, and without it one physical
         # flick can register several times and race through the screens.
         time.sleep_ms(TOGGLE_DEBOUNCE_MS)
         toggle_take()
         if toggle_pressed():
            # Still down after the release window: the switch is being held,
            # is resting off-centre, or the contact has failed. Advancing on
            # a level rather than an edge would spin through the screens for
            # as long as it stays there, so stop here and let the session
            # end. arm_toggle_wake() will also decline to arm this pin, so
            # the device sleeps properly instead of waking on it forever.
            print("Toggle still held after %dms - ending session"
                  % TOGGLE_RELEASE_MAX_MS)
            break
         deadline = time.ticks_add(time.ticks_ms(), TOGGLE_AWAKE_MS)
      time.sleep_ms(10)
   return screen


# current_timezone is a 1-element list so draw_screen can read the value set
# by main() without needing a global statement for a plain module-level var
# that main() also assigns before the cycle starts.
current_timezone = [DEFAULT_TIME_ZONE]

# Epoch second at which this power-on session started, or None before the
# clock is trustworthy. Same 1-element-list trick as current_timezone.
#
# This is what the info screen's "Runtime" row counts from, and it answers
# "how long has this run on one charge?". It is NOT a measure of time since
# USB was unplugged, because this board cannot tell: M5Unified's isCharging()
# has branches for the Paper, StickS3 and Tab5, and the Core Ink falls
# through to charge_unknown - there is no charge-status pin and no PMIC to
# ask. What it does measure is time since the last cold boot, which is the
# same thing in practice, since deep-sleep wakes preserve RTC memory and
# only applying power (or a reset) clears it. Reset the board while it is on
# USB, then unplug, and the two coincide exactly.
session_start = [None]

# time.time() starts at the 1970 epoch on a cold boot and only becomes
# meaningful once NTP has run, so anything below this is "clock not set yet"
# rather than a real timestamp. 2020-09-13.
TIME_VALID_EPOCH = 1600000000


def note_session_start():
   # Idempotent: the first cycle since power-on that has a valid clock wins,
   # and every later wake carries the value forward in RTC memory.
   if session_start[0] is None and time.time() > TIME_VALID_EPOCH:
      session_start[0] = time.time()


#################################################
#
# Main
#
#################################################

WAKE_EXT0 = 2   # esp_sleep_wakeup_cause_t, verified on this device
WAKE_EXT1 = 3


# The board's power rail is latched on by GPIO12 held high (confirmed on
# hardware: it reads 1 the whole time the device is running). ESP32 deep
# sleep disables GPIO output drivers, so without the hold below the latch
# releases the moment we sleep and, on battery, THE BOARD POWERS OFF
# COMPLETELY, and the back button does not bring it back: that button is a
# bare EN reset (measured: it produces rst:0x1 POWERON_RESET) with no path to
# the latch, so there is no rail left for it to reset. Only the PWR button
# (GPIO27) restarts it: that button powers the rail while it is down, and
# for long enough after for the firmware to take the latch over - a brief
# press is enough on the patched firmware. No firmware can rescue the back
# button: it holds EN low for the whole press, and a chip in reset drives
# nothing. A reset that does NOT hold EN low - a crash, a watchdog, the
# machine.reset() below - does survive on battery, but only on the patched
# firmware; verified there with a reset loop. On USB the fault is invisible,
# because USB feeds the rail directly. The panel keeps its last image with
# no power at all, so a board that is off reads as a board that is frozen.
#
# esp32.gpio_deep_sleep_hold() latches the pad states across the sleep so
# the rail stays up. GPIO12 is also the MTDI strapping pin that selects
# flash voltage at boot, which is normally a reason NOT to hold it high -
# but this board tolerates it: 13 consecutive deep-sleep cycles woke with
# reset_cause=DEEPSLEEP_RESET and GPIO12 still reading 1. If a future board
# revision or firmware ever fails to boot after sleeping, this is the first
# thing to suspect, and pulling EN low (reset button / esptool) clears the
# hold because it power-cycles the RTC domain.
#
# M5.Power.deepSleep() is NOT a substitute: it sleeps the panel but never
# touches power_hold, so it powers the board off on battery exactly like a
# bare machine.deepsleep(). M5.Power.timerSleep() does survive - it powers
# down and lets the RTC switch the board back on - but every wake is then a
# cold boot with no RTC memory and no GPIO wake, which would cost the toggle
# switch entirely.
POWER_HOLD_PIN = 12


def hold_power_rail():
   try:
      machine.Pin(POWER_HOLD_PIN, machine.Pin.OUT).value(1)
      import esp32
      esp32.gpio_deep_sleep_hold(True)
   except Exception as e:
      # Worth shouting about: on battery this is the difference between a
      # monitor and a brick until someone presses the power button.
      print("POWER HOLD FAILED (%s) - device may not survive sleep on battery" % e)


def release_power_rail_hold():
   # Called at the top of every cycle. While the pads are held they cannot
   # be re-driven, so this must happen before anything reconfigures GPIO -
   # and it means a bad hold can never compound across wakes.
   try:
      import esp32
      esp32.gpio_deep_sleep_hold(False)
   except Exception as e:
      print("Releasing GPIO hold failed: %s" % e)


def sleep_until(next_poll):
   # Sleep only until the next poll is actually due, rather than a fresh
   # full POLL_PERIOD_S. Without this, every toggle press would push the
   # next data refresh out by another 5 minutes, so someone idly flicking
   # through the screens could starve the reading this device exists to
   # show - the one failure mode a glucose monitor must not have. Clamped
   # at both ends: never a busy-loop, never longer than a normal period.
   remaining = POLL_PERIOD_S
   if next_poll:
      remaining = next_poll - time.time()
   if remaining < 5:
      remaining = 5
   elif remaining > POLL_PERIOD_S:
      remaining = POLL_PERIOD_S
   print("Sleeping for %d s" % remaining)
   arm_toggle_wake()

   # Put the e-paper panel into its own low-power state. M5.Power.deepSleep()
   # would do this via M5.Display.sleep(), but that is not reachable from
   # MicroPython (M5.Lcd exposes only powerSave*), and M5.Power.deepSleep()
   # cannot be used anyway - see hold_power_rail(). Skipping it leaves the
   # panel's booster and VCOM energised through the whole sleep, which both
   # wastes current and lets the image drift: the display slowly fades to a
   # washed-out, half-transparent version of itself.
   try:
      M5.Lcd.powerSaveOn()
   except Exception as e:
      print("Panel powerSaveOn failed: %s" % e)

   hold_power_rail()
   machine.deepsleep(int(remaining * 1000))


def main():
   # machine.deepsleep() re-runs this whole script from scratch on every
   # wake (there is no way to "return" from it) - so main() is a single
   # cycle, not a loop, and machine.reset_cause() is the only way to tell
   # "just powered on" apart from "woke up from the last deepsleep() call".
   # Only draw the version splash on a genuine cold boot: nobody needs to
   # re-see the version number every 5 minutes, and on a cold boot it is
   # useful confirmation that the right firmware came up.
   # First thing, before any GPIO is touched: drop the pad hold left over
   # from the previous sleep, or the pins stay frozen and nothing can be
   # re-driven this cycle.
   release_power_rail_hold()

   cold_boot = machine.reset_cause() != machine.DEEPSLEEP_RESET
   try:
      woke_on_toggle = machine.wake_reason() in (WAKE_EXT0, WAKE_EXT1)
   except Exception:
      woke_on_toggle = False

   # Seeded before anything that can fail, because the tail of this function
   # runs unconditionally and must have something to save and sleep on.
   screen, next_poll, snap, cycle = SCREEN_MAIN, None, None, 0
   toggle_only = False

   try:
      # The board's power-hold latch (GPIO12) is already high by the time
      # this runs - the firmware's own boot reaches M5GFX board autodetect,
      # which drives the pin, ~1.5 s after reset. Calling M5.begin() here
      # does not establish the latch and moving it earlier cannot help.
      #
      # clear_display=False suppresses the extra M5.Display.clear() that
      # M5.begin() would otherwise do on every wake, and skips the panel
      # reset. That is one less full-panel refresh per cycle.
      #
      # It does NOT stop the panel flashing, and nothing in this file can:
      # Panel_GDEW0154M09::display() ends every update with command 0x12
      # (DRF), and the controller's built-in waveform drives a full
      # black/white inversion sweep over whatever rectangle changed. Since
      # each redraw pushes the entire 200x200 canvas, that rectangle is the
      # whole panel. The flash is the panel working, not a bug - and it is
      # also what keeps the image clean, which is why FULL_REFRESH_EVERY
      # still schedules the slower two-pass EPD_QUALITY waveform
      # periodically (see draw_current_screen).
      try:
         M5.begin({"clear_display": False})
      except Exception as e:
         # Older bindings take no config dict; a flashing panel beats a
         # board that will not start.
         print("M5.begin(cfg) rejected (%s), falling back" % e)
         M5.begin()

      # Undo the panel power-down from the previous sleep. powerSaveOn()
      # issues Power OFF (0x02) only - deliberately NOT the DSLP command
      # that setSleep() would send, because leaving DSLP requires a full
      # panel reset and clear_display=False specifically skips that. A
      # plain Power ON (0x04) is enough to bring it back.
      try:
         M5.Lcd.powerSaveOff()
      except Exception as e:
         print("Panel powerSaveOff failed: %s" % e)

      if cold_boot:
         def _splash():
            gfx().fillScreen(WHITE)
            y = MARGIN
            lh = font_height(FONT_LABEL)
            # No auto-wrap on M5GFX (it clips silently) - word-wrap rather
            # than let this run off the 200px panel edge.
            for line in wrap_text_to_width("M5Ink Minimed Mon v%s" % VERSION,
                                           PANEL_W - 2*MARGIN, FONT_LABEL):
               draw_text(line, MARGIN, y, FONT_LABEL, BLACK)
               y += lh + 2
         compose(_splash, full_refresh=True)

   # Everything below is wrapped in one broad try/except: an unattended
   # device that hits an unexpected error and crashes to an idle REPL
   # prompt - showing whatever was last drawn, indefinitely, with zero
   # chance of self-recovery until someone physically resets it - is a much
   # worse failure mode than just missing one 5-minute update cycle. This
   # was observed directly on the Inkplate build (main() crashed outright,
   # likely from wlan.connect() raising OSError shortly after a cold radio
   # init - see the try/except inside wlan_connect() too) and left the
   # device stuck showing a stale splash screen until manually reset. Any
   # failure here falls through to the same deep sleep as a normal cycle,
   # so the next wake just tries again.
      rtc = rtc_state_load()
      screen = rtc.get("screen", SCREEN_MAIN)
      next_poll = rtc.get("next_poll")
      snap = rtc.get("snap")
      cycle = rtc.get("cycle", 0)
      session_start[0] = rtc.get("boot")
      note_session_start()   # in case the clock is already good (a wake)

      wifissid,wifipass,proxyaddr,proxyport,ntpserver,timezone,patient = read_config()
      current_timezone[0] = timezone
      cfg = (wifissid, proxyaddr, proxyport, ntpserver, timezone, patient)
      print("wifissid: %s, proxyaddr: %s, proxyport: %s, ntpserver: %s, timezone: %s" %
            (wifissid,proxyaddr,proxyport,ntpserver,timezone))

      # --- Toggle wake: redraw from the cached snapshot and go back to
      # sleep. Deliberately does NOT touch the network. Bringing up WiFi
      # and fetching would add several seconds between the flick and the
      # screen changing, on top of the ~4s this board already spends
      # booting its firmware before reaching this line - and none of the
      # four screens gains anything from data a few minutes fresher. The
      # scheduled poll below is what keeps the data current; this path only
      # changes which view of it is showing.
      if woke_on_toggle and not cold_boot:
         screen = (screen + 1) % SCREEN_COUNT
         state = state_from_snapshot(snap)
         draw_current_screen(screen, state, cfg, snap.get("ip") if snap else None,
                             full_refresh=False)
         screen = run_toggle_session(screen, state, cfg,
                                     snap.get("ip") if snap else None)
         toggle_only = True

      # Everything from here is the scheduled poll, skipped entirely on a
      # toggle wake (which has already redrawn from the snapshot above).
      #
      # Config missing falls through to do_access_point() inside
      # read_config() - appropriate there, someone is presumably present for
      # first-time setup. A WiFi connect failure here on an already-
      # configured device does NOT fall through to AP mode (see
      # wlan_connect()'s comment) - it just skips this cycle's fetch/draw,
      # so a transient WiFi hiccup can never strand this device in a state
      # that needs a human to physically walk through AP setup again.
      # A scheduled refresh always returns to the main screen. Beyond it
      # being the view this device exists for, it is the only screen that
      # shows the alarm banner - leaving the display parked on stats or
      # settings could hide an active alarm until someone happened to flick
      # back, which is exactly the failure the buzzer was added to prevent.
      if not toggle_only:
         screen = SCREEN_MAIN

         wlan = wlan_connect(wifissid, wifipass)
         if not wlan.isconnected():
            print("WiFi unavailable this cycle, skipping fetch/draw, retrying next wake")
         else:
            ntp_synced = False
            for _ in range(10):
               if ntp_sync(ntpserver):
                  ntp_synced = True
                  break
               time.sleep_ms(1000)
            print("NTP synced: %s" % ntp_synced)
            note_session_start()   # first valid clock since power-on starts it

            state = handle_pumpdataupdate(proxyaddr, proxyport, timezone)

            # Beep BEFORE the panel refresh, not after: the point of the sound
            # is to summon someone who isn't looking at the device, so it
            # should not wait behind a screen redraw they aren't watching
            # anyway. One beep per cycle in which an alarm is showing - see
            # "Alarm handling" above for why a still-active alarm beeping again
            # next wake is the intended behaviour, not a missing dedup.
            if state["alarm_text"]:
               print("Alarm active, sounding buzzer: %s" % state["alarm_text"])
               beep()

            try:
               ip = wlan.ifconfig()[0]
            except Exception:
               ip = None

            # The network is finished with for this cycle, and the WiFi stack
            # is holding ~45KB of the IDF heap that the drawing buffer needs.
            # Shutting the radio down here is what makes the fast canvas path
            # safe to take - see the comment above compose(). It also trims a
            # few mA off the tail of the wake period.
            try:
               wlan.active(False)
            except Exception as e:
               print("Could not power down WiFi: %s" % e)
            gc.collect()

            snap = make_snapshot(state, ip)
            next_poll = time.time() + POLL_PERIOD_S

            # Only every FULL_REFRESH_EVERY-th scheduled redraw pays the
            # ghost-clearing flash; the rest are silent. Tested before the
            # increment so that cycle 0 - the first draw after a cold boot,
            # which has the version splash still on the panel to clear - is a
            # full one.
            draw_current_screen(screen, state, cfg, ip,
                                full_refresh=(cycle % FULL_REFRESH_EVERY == 0))
            cycle += 1

            # No toggle session here on purpose - the device sleeps straight
            # after drawing. See the note under TOGGLE_AWAKE_MS for the
            # battery arithmetic that bought.
   except Exception as e:
      print("main() cycle failed: %s" % e)

   # --- Everything below MUST run, whatever happened above ---------------
   #
   # This device has no watchdog and nobody watching it. If main() ever
   # returns without reaching deepsleep, MicroPython drops to an idle REPL
   # and the board simply sits there - awake, showing whatever was last
   # drawn, never polling again, flattening the battery - until somebody
   # physically resets it. That is the "locks up after a while" failure, and
   # it is why M5.begin(), the splash and the RTC-state load were moved
   # inside the try above: an exception in any of them used to escape
   # main() entirely.
   #
   # Re-arm the schedule for any path that did not complete a fetch - no
   # WiFi, a proxy timeout, an unexpected exception. Those paths leave
   # next_poll at whatever RTC memory held, which is by definition already
   # due, and sleep_until() floors an overdue deadline at 5 seconds: a
   # WiFi outage would otherwise turn into the device waking every 5
   # seconds all night and flattening the battery, converting a temporary
   # loss of data into a dead monitor. Retry on the normal cadence instead.
   #
   # Computed here rather than before the fetch because time.time() is only
   # trustworthy after the NTP sync inside the try block; on a cold boot
   # the clock starts decades off, and anchoring the schedule to that would
   # put the next wake somewhere in 2002.
   try:
      if next_poll is None or next_poll <= time.time():
         next_poll = time.time() + POLL_PERIOD_S
   except Exception as e:
      print("Could not evaluate next_poll (%s)" % e)
      next_poll = None

   try:
      rtc_state_save(screen, next_poll, snap, cycle, session_start[0])
   except Exception as e:
      print("RTC state save failed: %s" % e)

   try:
      sleep_until(next_poll)
   except Exception as e:
      print("sleep_until failed: %s" % e)

   # Still here? Then deepsleep() did not take. Sleep blind rather than fall
   # through to the REPL, and if even that fails, reset - a reboot costs one
   # cycle, an idle REPL costs every cycle from now on. The reset is only
   # survivable on battery because the patched firmware asserts the power
   # hold 50ms into boot; on stock UIFlow this line would switch the board
   # off for good. See hold_power_rail().
   try:
      machine.deepsleep(POLL_PERIOD_S * 1000)
   except Exception as e:
      print("deepsleep failed (%s), resetting" % e)
      machine.reset()


if __name__ == "__main__":
   main()
