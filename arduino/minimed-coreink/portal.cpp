// First-time setup: an access point serving a config form.
//
// Ported from do_access_point() in main_m5coreink.py. Reached when the
// config is incomplete, which in practice means a device that has never been
// set up - so somebody is standing in front of it. That assumption is why
// this blocks indefinitely rather than timing out, and why it is silent.

#include "portal.h"
#include "screens.h"

#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <M5Unified.h>

static const char *AP_SSID = "M5INK_MINIMED_MON";
static const char *AP_PASS = "123456789";
static const char *AP_ADDR = "192.168.4.1";

static WebServer server(80);
static bool saved = false;

// Same form as the MicroPython build serves, field names included, so the
// two are interchangeable to anyone who has set one up before.
static String config_page(const Config &c) {
  String html =
    "<!DOCTYPE html><html><head><title>M5Ink Minimed Mon</title>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'></head>"
    "<body style=\"font-family:Helvetica,Arial,sans-serif\">"
    "<table style=\"text-align:left;width:400px;background-color:#2196F3;"
    "font-weight:bold;color:white\" cellpadding=2 cellspacing=2><tbody><tr><td>"
    "<span style=\"font-size:48px\">M5Ink Minimed Mon</span><br>"
    "<span style=\"font-size:20px;color:rgb(204,255,255)\">Configuration</span>"
    "</td></tr></tbody></table><br><form action='/config'>"
    "<table style=\"text-align:left;width:400px;background-color:white;"
    "font-weight:bold;font-size:14px\" cellpadding=2 cellspacing=3><tbody>"
    "<tr style=\"font-size:18px;background-color:lightgrey\"><td>Wifi parameters</td>"
    "<tr style=\"background-color:rgb(230,230,255)\"><td>SSID<br>"
    "<input type='text' name='fwifissid'></td>"
    "<tr style=\"background-color:rgb(230,230,255)\"><td>Password<br>"
    "<input type='text' name='fwifipass'></td>"
    "</tbody></table><br>"
    "<table style=\"text-align:left;width:400px;background-color:white;"
    "font-weight:bold;font-size:14px\" cellpadding=2 cellspacing=3><tbody>"
    "<tr style=\"font-size:18px;background-color:lightgrey\"><td>Time and date</td>"
    "<tr style=\"background-color:rgb(230,230,255)\"><td>NTP server address<br>"
    "<input type='text' name='fntpserver' value='" + c.ntpserver + "'></td>"
    "<tr style=\"background-color:rgb(230,230,255)\"><td>Time Zone (h)<br>"
    "<input type='text' name='ftimezone' value='" + String(c.timezone) + "'></td>"
    "</tbody></table><br>"
    "<table style=\"text-align:left;width:400px;background-color:white;"
    "font-weight:bold;font-size:14px\" cellpadding=2 cellspacing=3><tbody>"
    "<tr style=\"font-size:18px;background-color:lightgrey\"><td>Carelink proxy</td>"
    "<tr style=\"background-color:rgb(230,230,255)\"><td>IP address<br>"
    "<input type='text' name='fproxyaddr'></td>"
    "<tr style=\"background-color:rgb(230,230,255)\"><td>Port<br>"
    "<input type='text' name='fproxyport' value='" + String(c.proxyport) + "'></td>"
    "<tr style=\"background-color:rgb(230,230,255)\"><td>Patient name (optional)<br>"
    "<input type='text' name='fpatient'></td>"
    "</tbody></table><br><input type='submit' value='Save'></form></body></html>";
  return html;
}

static const char SUCCESS_PAGE[] =
  "<!DOCTYPE html><html><head><title>M5Ink Minimed Mon</title></head>"
  "<body style=\"font-family:Helvetica,Arial,sans-serif\">"
  "<table style=\"text-align:left;width:400px;background-color:#2196F3;"
  "font-weight:bold;color:white\" cellpadding=2 cellspacing=2><tbody><tr><td>"
  "<span style=\"font-size:48px\">M5Ink Minimed Mon</span><br>"
  "<span style=\"font-size:20px;color:rgb(204,255,255)\">Configuration</span>"
  "</td></tr></tbody></table><br>"
  "<table style=\"text-align:left;width:400px;background-color:rgb(230,230,255);"
  "font-weight:bold;font-size:14px\" cellpadding=2 cellspacing=3><tbody>"
  "<tr><td style=\"color:green;font-size:18px\">Parameters updated successfully</td>"
  "<tr><td style=\"color:grey\">Restarting device with new configuration ...</td>"
  "</tbody></table></body></html>";

// Silent by design, even though this board HAS a buzzer: AP mode means
// somebody is standing in front of the device doing setup, so there is
// nobody to summon. The buzzer is reserved for pump alarms - if it also
// chirped for routine setup steps it would train the caregiver to ignore it,
// which is the one failure it exists to prevent.
//
// Full refresh: these are rare, discrete transitions, and a clean panel
// matters more than speed when someone is reading instructions off it.
static void status(const char *msg) {
  Serial.printf("portal: %s\n", msg);
  M5.Display.setEpdMode(epd_mode_t::epd_quality);
  M5Canvas canvas(&M5.Display);
  canvas.setColorDepth(CANVAS_BPP);
  if (!canvas.createSprite(PANEL_W, PANEL_H)) {
    draw_status_screen(M5.Display, msg);
    return;
  }
  draw_status_screen(canvas, msg);
  canvas.pushSprite(0, 0);
  canvas.deleteSprite();
}

static void serve(Config c, bool use_ap) {
  if (!c.ntpserver.length()) c.ntpserver = "pool.ntp.org";

  if (use_ap) {
    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID, AP_PASS, 1, 0, 1);   // channel 1, not hidden, 1 client
    Serial.printf("portal: AP %s at %s\n", AP_SSID, AP_ADDR);
  } else {
    Serial.printf("portal: serving on http://%s/ (STA test)\n",
                  WiFi.localIP().toString().c_str());
  }

  char msg[160];
  if (use_ap) {
    snprintf(msg, sizeof(msg),
             "Device configuration needed\nConnect to WIFI network\n%s", AP_SSID);
    status(msg);
  }

  bool announced = false;

  server.on("/", [&c]() { server.send(200, "text/html", config_page(c)); });

  server.on("/config", [&c]() {
    // WebServer::arg() URL-decodes, which the MicroPython build's raw string
    // splitting does not - so a password containing %, & or a space works
    // here and silently does not there.
    String ssid = server.arg("fwifissid");
    String pass = server.arg("fwifipass");
    String ntp  = server.arg("fntpserver");
    String tz   = server.arg("ftimezone");
    String addr = server.arg("fproxyaddr");
    String port = server.arg("fproxyport");
    String pat  = server.arg("fpatient");

    // Patient name is deliberately NOT required: the proxy reports
    // firstName, so a device never told a name still shows the right one.
    if (!ssid.length() || !pass.length() || !ntp.length() || !tz.length() ||
        !addr.length() || !port.length()) {
      server.send(200, "text/html", config_page(c));
      return;
    }

    Preferences p;
    if (!p.begin("minimed", false)) {
      server.send(500, "text/plain", "could not open NVS");
      return;
    }
    p.putString("wifissid",  ssid);
    p.putString("wifipass",  pass);
    p.putString("proxyaddr", addr);
    p.putString("ntpserver", ntp);
    p.putString("patient",   pat);
    p.putUShort("proxyport", (uint16_t)port.toInt());
    p.putInt("timezone",     tz.toInt());
    p.end();

    server.send(200, "text/html", SUCCESS_PAGE);
    Serial.println("portal: configuration stored");
    saved = true;
  });

  server.onNotFound([&c]() { server.send(200, "text/html", config_page(c)); });
  server.begin();

  // Blocks until configured. No timeout on purpose: there is nothing useful
  // to fall back to - a device with no proxy address cannot monitor
  // anything - and someone is present by definition.
  while (!saved) {
    server.handleClient();
    if (use_ap && !announced && WiFi.softAPgetStationNum() > 0) {
      announced = true;
      snprintf(msg, sizeof(msg),
               "WIFI connection established\nLoad address %s in web browser",
               AP_ADDR);
      status(msg);
    }
    delay(5);
  }

  // Let the success page reach the browser before the radio goes down.
  for (int i = 0; i < 100; i++) { server.handleClient(); delay(10); }

  status("New configuration parameters stored\nResetting device ...");
  delay(2000);
  esp_restart();
}

void run_config_portal(const Config &current) { serve(current, true); }
void run_config_portal_sta(const Config &current) { serve(current, false); }
