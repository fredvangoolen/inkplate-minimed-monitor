// Pump fault codes, GENERATED from main_m5coreink.py - do not hand-edit.
//
// Reverse-engineered from real Carelink pump alarm data. Treat as data, not
// as something to restructure: extend by adding entries, never by changing
// the shape. The mapping is many-to-one (several raw ids share one canonical
// id); the table gives the human-readable text per canonical id.
//
// Regenerate with the script in the phase 4 commit if main_m5coreink.py's
// tables change - the two builds must not drift.

#include "faults.h"
#include <string.h>
#include <stdio.h>

namespace {

struct FaultMap  { const char *raw; const char *canonical; };
struct FaultText { const char *id;  const char *text; };

const FaultMap FAULT_MAP[] = {
  {"002", "002"},
  {"003", "002"},
  {"004", "002"},
  {"006", "006"},
  {"007", "007"},
  {"008", "007"},
  {"011", "011"},
  {"012", "012"},
  {"013", "002"},
  {"014", "002"},
  {"015", "002"},
  {"016", "002"},
  {"017", "002"},
  {"018", "002"},
  {"019", "002"},
  {"020", "002"},
  {"022", "002"},
  {"023", "002"},
  {"024", "024"},
  {"025", "025"},
  {"026", "002"},
  {"027", "002"},
  {"028", "002"},
  {"029", "029"},
  {"030", "002"},
  {"031", "002"},
  {"033", "002"},
  {"034", "002"},
  {"035", "024"},
  {"037", "037"},
  {"038", "037"},
  {"039", "037"},
  {"040", "024"},
  {"041", "037"},
  {"042", "037"},
  {"043", "037"},
  {"044", "002"},
  {"045", "002"},
  {"046", "002"},
  {"047", "024"},
  {"048", "024"},
  {"049", "002"},
  {"050", "024"},
  {"051", "051"},
  {"052", "052"},
  {"053", "002"},
  {"054", "002"},
  {"055", "024"},
  {"057", "057"},
  {"058", "058"},
  {"060", "002"},
  {"061", "061"},
  {"062", "062"},
  {"063", "002"},
  {"064", "002"},
  {"065", "002"},
  {"066", "066"},
  {"067", "002"},
  {"068", "002"},
  {"069", "069"},
  {"070", "062"},
  {"071", "062"},
  {"072", "062"},
  {"073", "011"},
  {"074", "002"},
  {"075", "002"},
  {"076", "002"},
  {"077", "077"},
  {"079", "002"},
  {"080", "002"},
  {"081", "002"},
  {"082", "002"},
  {"084", "084"},
  {"100", "100"},
  {"103", "103"},
  {"104", "104"},
  {"105", "105"},
  {"106", "105"},
  {"107", "107"},
  {"108", "062"},
  {"109", "109"},
  {"110", "110"},
  {"113", "113"},
  {"114", "062"},
  {"117", "117"},
  {"130", "130"},
  {"131", "024"},
  {"140", "140"},
  {"775", "775"},
  {"776", "776"},
  {"777", "777"},
  {"778", "777"},
  {"779", "779"},
  {"780", "780"},
  {"781", "780"},
  {"784", "784"},
  {"786", "062"},
  {"787", "062"},
  {"788", "062"},
  {"789", "777"},
  {"794", "794"},
  {"795", "795"},
  {"796", "796"},
  {"797", "797"},
  {"798", "797"},
  {"799", "062"},
  {"801", "801"},
  {"802", "802"},
  {"803", "803"},
  {"805", "805"},
  {"806", "062"},
  {"807", "807"},
  {"808", "807"},
  {"809", "809"},
  {"810", "810"},
  {"811", "810"},
  {"812", "812"},
  {"814", "814"},
  {"815", "815"},
  {"816", "816"},
  {"817", "817"},
  {"819", "819"},
  {"820", "819"},
  {"821", "821"},
  {"822", "822"},
  {"823", "823"},
  {"824", "823"},
  {"825", "062"},
  {"827", "827"},
  {"828", "062"},
  {"829", "829"},
  {"830", "829"},
  {"831", "829"},
  {"832", "832"},
  {"833", "833"},
  {"869", "869"},
  {"870", "870"},
};

const FaultText FAULT_TEXT[] = {
  {"002", "Pump Error. Delivery Stopped"},
  {"006", "Pump Battery Out Limit"},
  {"007", "Delivery Stopped. Check BG"},
  {"011", "Replace Pump Battery Now"},
  {"012", "Auto Suspend Limit Reached. Delivery Stopped"},
  {"024", "Critical Pump Error. Stop Pump Use. Use Other Treatment"},
  {"025", "Pump Power Error. Record Settings"},
  {"029", "Pump Restarted. Delivery Stopped"},
  {"037", "Pump Motor Error. Delivery Stopped"},
  {"051", "Bolus Stopped"},
  {"052", "Delivery Limit Exceeded. Check BG"},
  {"057", "Pump Battery Not Compatible"},
  {"058", "Insert A New AA Battery"},
  {"061", "Pump Button Error. Delivery Stopped"},
  {"062", "New Notification Received From Pump"},
  {"066", "No Reservoir Detected During Infusion Set Change"},
  {"069", "Loading Incomplete During Infusion Set Change"},
  {"073", "Replace Pump Battery Now"},
  {"077", "Pump Settings Error. Delivery Stopped"},
  {"084", "Pump Battery Removed. Replace Battery"},
  {"100", "Bolus Entry Timed Out Before Delivery"},
  {"103", "BG Check Reminder"},
  {"104", "Replace Pump Battery Soon"},
  {"105", "Reservoir Low. Change Reservoir Soon"},
  {"107", "Missed Meal Bolus Reminder"},
  {"109", "Set Change Reminder"},
  {"110", "Silenced Sensor Alert. Check Alarm History"},
  {"113", "Reservoir Empty. Change Reservoir Now"},
  {"117", "Active Insulin Cleared"},
  {"130", "Rewind Required. Delivery Stopped"},
  {"140", "Delivery Suspended. Connect Infusion Set"},
  {"775", "Calibrate Now"},
  {"776", "Calibration Error"},
  {"777", "Change Sensor"},
  {"779", "Recharge Transmitter Now"},
  {"780", "Lost Sensor Signal"},
  {"784", "SG Rising Rapidly"},
  {"794", "Sensor Expired. Change Sensor"},
  {"795", "Lost Sensor Signal. Check Transmitter"},
  {"796", "No Sensor Signal"},
  {"797", "Sensor Connected"},
  {"801", "Do Not Calibrate. Wait Up To 3 Hours"},
  {"802", "Low Sensor Glucose"},
  {"803", "Low Sensor Glucose. Check BG"},
  {"805", "Alert Before Low. Check BG"},
  {"807", "Basal Delivery Resumed. Check BG"},
  {"809", "Suspend On Low. Delivery Stopped. Check BG"},
  {"810", "Suspend Before Low. Delivery Stopped. Check BG"},
  {"812", "Call Emergency Assistance"},
  {"814", "Basal Resumed. SG Still Under Low Limit. Check BG"},
  {"815", "Low Limit Changed. Basal Manually Resumed. Check BG"},
  {"816", "High Sensor Glucose"},
  {"817", "Alert Before High. Check BG"},
  {"819", "Auto Mode Exit. Basal Delivery Started. BG Required"},
  {"821", "Minimum Delivery Timeout. BG Required"},
  {"822", "Maximum Delivery Timeout. BG Required"},
  {"823", "High Sensor Glucose For Over 1 Hour"},
  {"827", "Urgent Low Sensor Glucose. Check BG"},
  {"829", "BG Required"},
  {"832", "Calibration Required"},
  {"833", "Correction Bolus Recommended"},
  {"869", "Calibration Reminder"},
  {"870", "Recharge Transmitter Soon"},
};

// Glucose alarms whose notification goes stale once the reading recovers.
const char *LOW_GLUCOSE_IDS[]  = {"802", "805", "809", "810", "814", "815", "827"};
const char *HIGH_GLUCOSE_IDS[] = {"816", "817", "823"};

}  // namespace

const char *fault_canonical(const char *faultId) {
  for (auto &e : FAULT_MAP) if (!strcmp(e.raw, faultId)) return e.canonical;
  return faultId;
}

void fault_str(const char *faultId, char *buf, size_t n) {
  const char *canon = fault_canonical(faultId);
  for (auto &e : FAULT_TEXT) {
    if (!strcmp(e.id, canon)) { snprintf(buf, n, "%s", e.text); return; }
  }
  snprintf(buf, n, "Unknown error code %s", faultId);
}

bool fault_is_low_glucose(const char *canonicalId) {
  for (auto &e : LOW_GLUCOSE_IDS) if (!strcmp(e, canonicalId)) return true;
  return false;
}

bool fault_is_high_glucose(const char *canonicalId) {
  for (auto &e : HIGH_GLUCOSE_IDS) if (!strcmp(e, canonicalId)) return true;
  return false;
}
