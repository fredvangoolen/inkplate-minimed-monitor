// Pump fault code lookup. Tables generated from main_m5coreink.py; see
// faults.cpp.

#pragma once

#include <stddef.h>

// Raw Carelink fault id -> canonical id (many-to-one). Returns the input
// unchanged when unknown, matching the Python dict .get(id, id) form.
const char *fault_canonical(const char *faultId);

// Human-readable message for a raw fault id, or "Unknown error code <id>".
void fault_str(const char *faultId, char *buf, size_t n);

bool fault_is_low_glucose(const char *canonicalId);
bool fault_is_high_glucose(const char *canonicalId);
