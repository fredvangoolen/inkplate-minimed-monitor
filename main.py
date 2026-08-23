###############################################################################
#
#  Inkplate Minimed Monitor
#
#  Description:
#
#  A remote monitor for the Medtronic Minimed 770G/780G insulin pump system,
#  for use by caregivers of a Type-1 Diabetes patient wearing the pump. Runs
#  on a Soldered Inkplate 2 (2.13" 3-color e-paper, classic ESP32) under
#  MicroPython (github.com/SolderedElectronics/Inkplate-micropython): it
#  wakes every ~5 minutes, polls an external Carelink proxy for the pump's
#  current glucose/insulin/sensor status, redraws a single always-current
#  view, and goes back to deep sleep.
#
#  Credits:
#
#  This project builds on and was originally inspired by Ondrej Wisniewski's
#  M5 Minimed Monitor (an M5Stack-based version of the same idea) - notably
#  the fault-code lookup tables below, which were reverse-engineered from
#  Carelink pump alarm data, and the overall data-polling/config/AP-setup
#  design. The e-paper display this runs on has different enough
#  constraints (a small 3-color panel with no partial refresh, no speaker,
#  no buttons beyond reset) that the GUI, timing, and alarm-handling code
#  here is its own design, not a port.
#
#  Dependencies:
#
#  Polls an external Carelink proxy (a REST API in front of Medtronic's
#  Carelink Cloud) for pump data - see the Carelink Python Client project
#  (https://github.com/ondrej1024/carelink-python-client) for one way to
#  run such a proxy. Point this script at it via the config's proxyaddr/
#  proxyport (see "Access configuration parameters" below).
#
#  Hardware/firmware notes (all confirmed on real hardware):
#
#  * No partial-refresh API exists for this board - draw_screen() below
#         redraws the whole 212x104 panel from scratch every cycle, ending
#         in one display.display() call (~17-23s; more with red content).
#  * The on-device font (gfx_standard_font_01) is a fixed 16px-tall,
#         proportional-width font, not a 6x8 cell - see FONT_HEIGHT_1X and
#         text_width() below.
#  * No requests/urequests module in this firmware - http_get() hand-rolls
#         HTTP/1.0 over a raw socket instead, matching every official
#         Inkplate-micropython network example.
#  * No text auto-wrap either - see wrap_text_to_width()/truncate_to_width().
#  * This MicroPython build's time module uses a 2000-01-01 epoch, not the
#         standard 1970 Unix epoch Carelink's timestamps use - see
#         EPOCH_ADJUST_S below.
#  * machine.deepsleep() between cycles re-runs this whole script from
#         scratch on every wake - main() carries no state across a cycle by
#         design (see "Alarm handling" below), which is what makes that
#         safe. It's also what keeps average power draw low (~8uA asleep
#         vs. tens of mA while awake) - CGM readings only update roughly
#         every 5 minutes upstream regardless of display tech, so polling
#         faster than that would just burn battery for no new data.
#  * Alarms are shown as a visual banner only - this board has no speaker,
#         so a caregiver not looking at the display gets no alert. Accepted
#         tradeoff for this device.
#
#  TODO:
#
#  * Factory-reset gesture for a stuck bad config (currently: delete
#         /minimed_config.json via mpremote manually - see the comment near
#         config_read()/config_write()). Proposed: detect N power-cycles
#         within a short window via a boot counter, distinguished from a
#         normal deep-sleep wake (which also reports via
#         machine.reset_cause()) some other way, e.g. an RTC-memory flag
#         cleared at the end of a normal cycle - needs on-device
#         verification with real power-cycling before it's safe to wire up
#         to a destructive config wipe.
#  * A compact status row (battery/reservoir/sensor/sage) - currently
#         parsed into state but not rendered (see new_state()), dropped
#         after on-device visual review found it cluttered; a future
#         version could use pre-converted bitmap icons for it.
#  * draw_screen()'s red-banner-ghosting workaround (one extra blank
#         display() pass on the cycle after an alarm banner clears - see
#         the comment above FONT_HEIGHT_1X) is a caregiver-reported-symptom
#         fix, not yet confirmed sufficient on real hardware - if a single
#         extra pass doesn't fully clear it, add more. It also claims one
#         byte of RTC memory (see _rtc above) - any future use of RTC
#         memory (e.g. the factory-reset boot counter above) needs to not
#         collide with that byte.
#
#  Copyright 2021-2026, Ondrej Wisniewski and contributors
#
#  Modified 2026 to target the Soldered Inkplate 2 platform: the GUI,
#  timing, and alarm-handling logic were redesigned for this board's
#  e-paper display and deep-sleep power model (see "Credits" above).
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

from inkplate2 import Inkplate
import ntptime
import time
import json
import network
import socket
import machine

VERSION = "0.1"

# Constants
CONFIG_FILE = "/minimed_config.json"

# Default configuration parameters
DEFAULT_NTP_SERVER = "pool.ntp.org"
DEFAULT_TIME_ZONE  = "1"
DEFAULT_PROXY_PORT = "8081"

# Access point parameters
API_URL     = "carelink/nohistory"
AP_SSID     = "INKPLATE_MINIMED_MON"
AP_ADDR     = "192.168.4.1"

# Fixed local thresholds - there's no pump-configured per-reading threshold
# in the Carelink proxy's response to read instead. Used to color the
# glucose number red on draw_screen(), and also by get_alarm_text() to
# decide whether a low/high-glucose alarm notification still reflects the
# current reading (see "Alarm handling" below).
HYPER_THRESHOLD_MGDL = 180
HYPO_THRESHOLD_MGDL  = 70

# Timing. There is no periodic NTP-resync interval to track separately -
# main() below resyncs NTP on every wake instead, since machine.deepsleep()
# re-runs the whole script from scratch each cycle anyway (measured cost:
# ~75ms, negligible next to the ~17-23s panel refresh) and there is no
# in-memory state that would let it track "time since last sync" across a
# deep-sleep restart in the first place.
POLL_PERIOD_S        = 300    # 5 min: matches upstream CGM reading cadence
ALARM_RECENCY_S      = 15*60  # only announce alarms newer than this

# This MicroPython build's time module uses a 2000-01-01 epoch for
# time.time()/time.localtime(), NOT the standard 1970-01-01 Unix epoch -
# confirmed on real hardware (time.time() read back a plausible ~840M
# seconds for "now" in 2026, and feeding a raw Carelink
# lastConduitUpdateServerDateTime value - which IS standard Unix-epoch ms -
# straight into time.localtime() produced a date exactly 30 years in the
# future). Subtract this constant when converting an external Unix-epoch
# value to this device's local epoch. Not needed for
# convert_datetimestr_to_epoch()/get_alarm_text()'s recency check below,
# since that builds its epoch value via a local time.mktime() call and
# compares it against time.time() - both already on this device's own
# epoch.
EPOCH_ADJUST_S = 946684800  # seconds between 1970-01-01 and 2000-01-01

# Panel geometry (Inkplate 2 is 212x104)
PANEL_W = 212
PANEL_H = 104

# Global variables. Note there is deliberately no per-alarm dedup state
# here - see the "Alarm handling" section below for why, and how that keeps
# main()'s machine.deepsleep() cycle stateless across wakes.
dstDelta     = 0


# Fault ID mapping: raw Carelink fault ID -> canonical ID (many-to-one).
# Reverse-engineered from real Carelink pump alarm data - treat this as
# data, not something to restructure; extend by adding entries.
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
# Configuration storage: a plain JSON file at the filesystem root (this
# firmware's root is "/", confirmed on real hardware)
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


# To clear a stuck config and force AP-mode setup again: os.remove(CONFIG_FILE)
# via mpremote (see TODO at the top of this file for a proper factory-reset
# gesture - not implemented yet).


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
# HTTP client (no requests/urequests module on this firmware - hand-rolled
# HTTP/1.0-over-socket, same pattern the official inkplate2 examples use)
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
            <html><head><title>Inkplate Minimed Mon</title></head> \n \
            <body><table style="text-align: left; width: 400px; background-color: #2196F3; font-family: Helvetica,Arial,sans-serif; font-weight: bold; color: white;" border="0" cellpadding="2" cellspacing="2"> \n \
            <tbody><tr><td> \n \
            <span style="vertical-align: top; font-size: 48px;">Inkplate Minimed Mon</span><br> \n \
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
            </tbody></table><br> \n \
            <input type="submit" value="Save"> \n \
            </form></body></html>' % (ntpserver,timezone,proxyport)
   return html


def web_page_success():
   html =  '<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd"> \n \
            <html><head><title>Inkplate Minimed Mon</title></head> \n \
            <body><table style="text-align: left; width: 400px; background-color: #2196F3; font-family: Helvetica,Arial,sans-serif; font-weight: bold; color: white;" border="0" cellpadding="2" cellspacing="2"> \n \
            <tbody><tr><td> \n \
            <span style="vertical-align: top; font-size: 48px;">Inkplate Minimed Mon</span><br> \n \
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


def do_ap_status(inkplate, msg):
   # No speaker on this board, so there's no sound here - just drawn
   # straight onto the panel. AP-mode status changes are rare, discrete
   # transitions, so paying a full refresh per message here is a non-issue.
   inkplate.clear_display()
   inkplate.set_cursor(2, 2)
   inkplate.set_text_size(1)
   inkplate.set_text_color(inkplate.BLACK)
   for line in msg.split("\n"):
      inkplate.println(line)
   inkplate.display()


def do_access_point(inkplate, ntpserver,timezone,proxyport):
   # Start access point
   ap = network.WLAN(network.AP_IF)
   ap.active(True)
   ap.config(essid=AP_SSID)
   ap.config(authmode=3, password='123456789')
   ap.config(max_clients=1)
   do_ap_status(inkplate, "Device configuration needed\nConnect to WIFI network\n%s" %(AP_SSID))

   # Wait for client to connect
   while ap.isconnected() == False:
       pass
   do_ap_status(inkplate, "WIFI connection established\nLoad address %s in web browser" % (AP_ADDR))

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
   })
   print("New configuration parameters stored in config file\n")
   do_ap_status(inkplate, "New configuration parameters stored\nResetting device ...")

   # Reset device
   time.sleep_ms(2000)
   machine.reset()


#################################################
#
# Access configuration parameters
#
#################################################

def read_config(inkplate):
   cfg = config_read()
   wifissid  = cfg.get('wifissid')
   wifipass  = cfg.get('wifipass')
   ntpserver = cfg.get('ntpserver')
   timezone  = cfg.get('timezone')
   proxyaddr = cfg.get('proxyaddr')
   proxyport = cfg.get('proxyport')

   if proxyport == None:
      proxyport = DEFAULT_PROXY_PORT
   if ntpserver == None:
      ntpserver = DEFAULT_NTP_SERVER
   if timezone == None:
      timezone  = DEFAULT_TIME_ZONE

   if wifissid == None or wifipass == None or proxyaddr == None:
      print("Needed configuration parameters not found\n")
      do_access_point(inkplate, ntpserver,timezone,proxyport)

   return (wifissid,wifipass,proxyaddr,proxyport,ntpserver,timezone)


#################################################
#
# Connect to network
#
#################################################

# This board's WiFi radio is fully powered down by machine.deepsleep() and
# must cold-reassociate from scratch on EVERY wake, every 5 minutes - which
# can occasionally be slow. A short retry budget was observed on real
# hardware to spuriously drop an already-correctly-configured device
# straight into do_access_point()'s blocking, physical-intervention-
# required config flow on a single transient connect hiccup - unacceptable
# for an unattended device that should just retry next cycle. Generous
# timeout below; more importantly, see the callers of this function: a
# connect failure here no longer auto-triggers AP mode by itself (AP mode
# is only for a config file that's missing credentials in the first place -
# see read_config()) - a transient failure on an already-configured device
# just skips this cycle's fetch/draw and retries on the next wake instead.
WIFI_CONNECT_TIMEOUT_S = 25

def wlan_connect(wifissid, wifipass):
   wlan = network.WLAN(network.STA_IF)
   wlan.active(True)
   try:
      # wlan.connect() can itself raise OSError (e.g. "Wifi Internal Error")
      # rather than just failing to associate - observed as a plausible
      # cause of main() crashing outright on real hardware shortly after a
      # cold radio init (right after a hard reset), leaving the device
      # stuck showing stale content with no self-recovery. Treat it the
      # same as a plain connect timeout: log and let the caller retry next
      # cycle, never let it propagate and crash the whole cycle.
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


TREND_GLYPH = {
   "UP_TRIPLE":   "^^^",
   "UP_DOUBLE":   "^^",
   "UP":          "^",
   "NONE":        "->",
   "DOWN":        "v",
   "DOWN_DOUBLE": "vv",
   "DOWN_TRIPLE": "vvv",
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
# visible display with no speaker - you want to see it whenever you glance
# at the device, not just once. It also means there is nothing that needs
# to survive a deep-sleep restart, which is what makes machine.deepsleep()
# straightforward here despite each wake re-running the whole script from
# scratch - see main() below.
#
# The Carelink proxy's lastAlarm field reports the most recent alarm
# NOTIFICATION, not whether the underlying condition is still true - it
# does not update or clear itself just because glucose has since recovered.
# Relying on ALARM_RECENCY_S alone (as an earlier version of this file did)
# left a "Low Sensor Glucose" banner showing in red for up to 15 minutes
# after glucose had already returned to a normal reading, confirmed on real
# hardware. For the fault IDs below - all directly tied to a low/high
# glucose *threshold*, unlike e.g. a reservoir or battery alarm which this
# script has no independent way to verify - get_alarm_text() additionally
# corroborates against the CURRENT reading and treats the notification as
# stale (cleared) once glucose is back on the right side of the threshold,
# even if still within the recency window.
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
   # Unlike lastConduitUpdateServerDateTime (a true UTC epoch in ms - see
   # EPOCH_ADJUST_S), lastAlarm["dateTime"]'s digits are ALREADY local
   # wall-clock time, confirmed on real hardware: displaying them with a
   # further timezone/DST offset ADDED showed a time 2 hours ahead of the
   # real CEST clock. convert_datetimestr_to_epoch() parses those digits
   # with no timezone awareness at all, so its result already directly
   # matches local wall-clock time for display - but comparing it as-is
   # against time.time() (true UTC) for the recency check silently
   # inflated the effective "still recent" window by the full offset (2h
   # for CEST) instead of the intended ALARM_RECENCY_S (15 min): a
   # low/high-glucose alarm could stay shown for ~2h15m after it actually
   # fired, not 15 minutes, on top of the separate current-glucose check
   # below. Fixed by shifting the naively-parsed value BACK by the offset
   # to get a true UTC epoch for the comparison, while still displaying
   # the original (already-local) value directly.
   try:
      naive_local_epoch = convert_datetimestr_to_epoch(lastAlarm["dateTime"])
      # "naive_local_epoch and ..." also guards against a parse failure
      # (which returns 0, see convert_datetimestr_to_epoch) ever being
      # mistaken for a recent alarm.
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
# Pump data polling - builds a plain state dict, drawn by draw_screen()
# below
#
#################################################

def new_state():
   # battery_pct/reservoir_units/sensor_ok/sage_hours/sage_state are parsed
   # and carried in state but NOT rendered by draw_screen() (the status row
   # that would show them was dropped - see the TODO at the top of this
   # file). Kept here since they're cheap to parse and are the obvious
   # candidate for a compact bitmap-based status row later.
   return {
      "haveData": False,
      "battery_pct": None,
      "reservoir_units": None,
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
   # alarm banner (safety-relevant) shouldn't depend on any other field
   # parsing succeeding.
   try:
      dstDelta = 1 if data["clientTimeZoneName"].lower().find("summer")>-1 else 0
      unix_epoch_s = int(data["lastConduitUpdateServerDateTime"]/1000)
      state["last_update_tm"] = time.localtime(
         unix_epoch_s - EPOCH_ADJUST_S + (int(timezone)+dstDelta)*3600)
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
         state["sage_hours"] = data["sensorDurationHours"]
         state["sage_state"] = data["sensorState"]
      state["sensor_ok"] = bool(data.get("conduitSensorInRange"))

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
# Display: single consolidated view, redrawn fully every cycle (confirmed on
# real hardware: no partial-refresh API exists for this board's driver)
#
# Layout note: the on-device font (gfx_standard_font_01, confirmed via
# get_ch() on real hardware) is NOT the 6x8 cell an Adafruit-GFX-style
# default font would suggest - it's a fixed 16px-tall, proportional-width
# font (digits are 8px wide at size 1). At set_text_size(4) that's a 64px-
# tall glyph, not 32px - an earlier version of this layout assumed 32px and
# visibly collided the "mg/dL" label into the bottom of the glucose number
# on real hardware. text_width() below measures real per-glyph widths
# instead of assuming a flat cell size, to avoid repeating that mistake for
# any other string. Status icons (battery/reservoir/sensor/sage) from an
# earlier layout sketch were dropped per on-device visual feedback - this
# is just the glucose figure, its unit/trend, and active insulin.
#
# Red-banner ghosting: this panel's red plane doesn't fully clear on a
# single full-panel refresh when the previous frame had a large solid red
# area (the alarm banner) - a caregiver reported the glucose number
# updating fine while the red banner itself stayed on screen after the
# alarm had aged out. This is the physical red e-ink pigment being slower
# to migrate than black/white, not a framebuffer bug: clear_display()
# already zeroes this driver's own framebuffer every cycle (see
# handle_pumpdataupdate()/draw_screen() below), so what's lingering is
# purely on the panel's glass. The fix is an extra blank (all-white)
# display() pass to force a second physical flash, but only on the one
# cycle where a banner just stopped being shown - every cycle would
# double the ~17-23s awake time this design otherwise avoids. Since
# main() carries no state across a deep-sleep restart by design, that
# "was a banner shown last cycle" bit is stashed in RTC memory instead of
# the config file (survives deep sleep, no flash wear, cleared to
# "no banner" on a true cold boot - which is fine, there's nothing to
# clear yet). Needs on-device confirmation that one extra pass is enough;
# if ghosting persists, this is the place to add more passes.
#
#################################################

_rtc = machine.RTC()


def _had_banner_last_cycle():
   try:
      return _rtc.memory() == b"\x01"
   except Exception:
      return False


def _set_banner_flag(has_banner):
   try:
      _rtc.memory(b"\x01" if has_banner else b"\x00")
   except Exception:
      pass


FONT_HEIGHT_1X = 16  # gfx_standard_font_01, confirmed via get_ch() on real hw


def text_width(inkplate, s, size=None):
   size = inkplate.text_size if size is None else size
   w = 0
   for ch in s:
      try:
         _, _, cw = inkplate.font_family.get_ch(ch)
      except (ValueError, TypeError):
         _, _, cw = inkplate.font_family.get_ch("?")
      w += cw * size
   return w


def wrap_text_to_width(inkplate, s, max_w, size):
   words = s.split(" ")
   lines = []
   cur = ""
   for word in words:
      candidate = (cur + " " + word).strip()
      if cur and text_width(inkplate, candidate, size) > max_w:
         lines.append(cur)
         cur = word
      else:
         cur = candidate
   if cur:
      lines.append(cur)
   return lines


def truncate_to_width(inkplate, s, max_w, size):
   if text_width(inkplate, s, size) <= max_w:
      return s
   ellipsis_w = text_width(inkplate, "...", size)
   out = ""
   w = 0
   for ch in s:
      _, _, cw = inkplate.font_family.get_ch(ch)
      if w + cw*size + ellipsis_w > max_w:
         break
      out += ch
      w += cw*size
   return out + "..."


def draw_screen(inkplate, state):
   inkplate.clear_display()

   banner_text = state["alarm_text"] or state["banner"]

   if not banner_text and _had_banner_last_cycle():
      # See "Red-banner ghosting" comment above - force one extra blank
      # flash to physically clear the outgoing red banner before drawing
      # this cycle's real (non-banner) content into the same buffer below.
      inkplate.display()

   BLACK = inkplate.BLACK
   WHITE = inkplate.WHITE
   RED   = inkplate.RED

   # --- Glucose number, its unit/trend beside it, and active insulin ---
   sg_txt = str(state["sg"]) if state["sg"] is not None else "---"
   glucose_color = BLACK
   if state["sg"] is not None:
      if state["sg"] >= HYPER_THRESHOLD_MGDL or state["sg"] <= HYPO_THRESHOLD_MGDL:
         glucose_color = RED

   GLUCOSE_SIZE = 4
   gx, gy = 2, 2
   inkplate.set_text_size(GLUCOSE_SIZE)
   inkplate.set_text_color(glucose_color)
   inkplate.set_cursor(gx, gy)
   inkplate.print(sg_txt)
   gw = text_width(inkplate, sg_txt, GLUCOSE_SIZE)
   gh = FONT_HEIGHT_1X * GLUCOSE_SIZE

   # Trend glyph and "mg/dL" sit to the right of the number (same vertical
   # band it occupies), not below it - there's no room below at this font
   # size without colliding into the active-insulin row.
   side_x = gx + gw + 6
   inkplate.set_text_size(2)
   inkplate.set_text_color(BLACK)
   inkplate.set_cursor(side_x, gy)
   inkplate.print(TREND_GLYPH.get(state["trend"], "->"))

   inkplate.set_text_size(1)
   inkplate.set_text_color(BLACK)
   inkplate.set_cursor(side_x, gy + FONT_HEIGHT_1X*2 + 4)
   inkplate.print("mg/dL")

   insulin_y = gy + gh + 2
   inkplate.set_text_size(1)
   inkplate.set_text_color(BLACK)
   inkplate.set_cursor(2, insulin_y)
   if state["active_insulin"] is not None:
      inkplate.print("%.1f U active insulin" % state["active_insulin"])
   else:
      inkplate.print("-- U active insulin")

   # --- Bottom row: alarm banner when active, else a "last updated"
   # timestamp (share the row - the alarm, being safety-relevant, takes
   # priority over the informational timestamp when both would apply).
   # The timestamp serves two purposes: you don't need to stare at the
   # panel waiting for the next ~17-23s refresh to know whether what's
   # showing is current, and a timestamp that stops advancing is how
   # you'd notice the device's battery died (staleness signal).
   bottom_y = insulin_y + FONT_HEIGHT_1X + 2
   inkplate.set_text_size(1)
   if banner_text:
      # For a genuine pump alarm (not the generic systemStatusMessage/
      # pumpBannerState banner, neither of which carries a comparable
      # timestamp), append when it actually occurred - lets a caregiver
      # tell a fresh alarm apart from one still showing only because it's
      # within ALARM_RECENCY_S of its own occurrence, not "now". Truncate
      # the message first, not the concatenated string, so this suffix is
      # never the part that gets clipped.
      suffix = ""
      if state["alarm_text"] and state["alarm_tm"] is not None:
         hh, mm = state["alarm_tm"][3], state["alarm_tm"][4]
         suffix = " (%02d:%02d)" % (hh, mm)
      inkplate.fill_rect(0, bottom_y, PANEL_W, FONT_HEIGHT_1X, RED)
      inkplate.set_text_color(WHITE)
      inkplate.set_cursor(1, bottom_y)
      max_w = PANEL_W - 2 - text_width(inkplate, suffix, 1)
      inkplate.print(truncate_to_width(inkplate, banner_text, max_w, 1) + suffix)
   else:
      if state["last_update_tm"] is not None:
         now = local_now(current_timezone[0], dstDelta)
         delta_txt = time_delta(state["last_update_tm"], now, current_timezone[0])
         # The bracketed clock time is the moment THIS PANEL REFRESH
         # happened (now), not the CGM reading's own timestamp - "Updated
         # 4 min ago (09:55)" means the reading was from 9:51 but this
         # display last redrew itself at 9:55. Since the panel only
         # redraws once per ~5-minute cycle, this whole line - both the
         # relative delta and the absolute clock time - is computed once
         # and then stays static/unchanging on-screen until the next
         # cycle, by construction (e-paper doesn't tick live).
         hh, mm = now[3], now[4]
         age_txt = "Updated %s (%02d:%02d)" % (delta_txt, hh, mm)
      else:
         age_txt = "No data"
      inkplate.set_text_color(BLACK)
      inkplate.set_cursor(2, bottom_y)
      inkplate.print(truncate_to_width(inkplate, age_txt, PANEL_W-4, 1))

   inkplate.display()
   _set_banner_flag(bool(banner_text))


# current_timezone is a 1-element list so draw_screen can read the value set
# by main() without needing a global statement for a plain module-level var
# that main() also assigns before the loop starts.
current_timezone = [DEFAULT_TIME_ZONE]


#################################################
#
# Main
#
#################################################

def main():
   # machine.deepsleep() re-runs this whole script from scratch on every
   # wake (there is no way to "return" from it) - so main() is a single
   # cycle, not a loop, and machine.reset_cause() is the only way to tell
   # "just powered on" apart from "woke up from the last deepsleep() call".
   # Only draw the version splash on a genuine cold boot - it costs a full
   # ~17s panel refresh that would otherwise be paid on every single 5-min
   # wake for no benefit (nobody needs to re-see the version number every
   # cycle), eating into the deep-sleep power budget this is all for.
   cold_boot = machine.reset_cause() != machine.DEEPSLEEP_RESET

   inkplate = Inkplate()
   inkplate.begin()

   if cold_boot:
      inkplate.clear_display()
      inkplate.set_text_size(1)
      inkplate.set_text_color(inkplate.BLACK)
      # No auto-wrap on this driver (confirmed on real hardware - see the
      # "Layout note" comment above draw_screen()) - word-wrap onto
      # multiple lines rather than let it silently run off the 212px
      # panel edge, since this string doesn't fit on one line at this size.
      splash_lines = wrap_text_to_width(inkplate, "Inkplate Minimed Mon v%s" % VERSION, PANEL_W-4, 1)
      y = 2
      for line in splash_lines:
         inkplate.set_cursor(2, y)
         inkplate.print(line)
         y += FONT_HEIGHT_1X
      inkplate.display()

   # Everything below is wrapped in one broad try/except: an unattended
   # device that hits an unexpected error and crashes to an idle REPL
   # prompt - showing whatever was last drawn, indefinitely, with zero
   # chance of self-recovery until someone physically resets it - is a much
   # worse failure mode than just missing one 5-minute update cycle. This
   # was observed directly on real hardware (main() crashed outright,
   # likely from wlan.connect() raising OSError shortly after a cold radio
   # init - see the try/except added inside wlan_connect() too) and left
   # the device stuck showing a stale splash screen until manually reset.
   # Any failure here now falls through to the same deep sleep as a normal
   # cycle, so the next wake just tries again.
   try:
      wifissid,wifipass,proxyaddr,proxyport,ntpserver,timezone = read_config(inkplate)
      current_timezone[0] = timezone
      print("wifissid: %s, proxyaddr: %s, proxyport: %s, ntpserver: %s, timezone: %s" %
            (wifissid,proxyaddr,proxyport,ntpserver,timezone))

      # Config missing falls through to do_access_point() inside
      # read_config() - appropriate there, someone is presumably present
      # for first-time setup. A WiFi connect failure here on an already-
      # configured device does NOT fall through to AP mode (see
      # wlan_connect()'s comment) - it just skips this cycle's fetch/draw,
      # so a transient WiFi hiccup can never strand this device in a state
      # that needs a human to physically walk through AP setup again.
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

         state = handle_pumpdataupdate(proxyaddr, proxyport, timezone)
         draw_screen(inkplate, state)
   except Exception as e:
      print("main() cycle failed: %s" % e)

   print("Sleeping for %d s" % POLL_PERIOD_S)
   machine.deepsleep(POLL_PERIOD_S * 1000)


if __name__ == "__main__":
   main()
