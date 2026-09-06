// First-time setup over an access point. See portal.cpp.

#pragma once

#include "types.h"

// Starts the AP, serves the config form, stores what comes back in NVS and
// reboots. Blocks until configured and never returns.
void run_config_portal(const Config &current);

// Same form and handlers, but served over an existing STA connection
// instead of the AP. Exists so the page, the validation, the NVS write and
// the reboot can be exercised from a laptop without wiping a working
// device's config to get at them.
void run_config_portal_sta(const Config &current);
