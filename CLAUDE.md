# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A remote monitor for the Medtronic Minimed 770G/780G insulin pump, for a caregiver of a Type-1 Diabetes patient. It wakes every ~5 minutes, polls an external **Carelink proxy** for pump status, redraws a single always-current view, and goes back to deep sleep.

**What actually runs today is `arduino/minimed-coreink/` — the C++ port, on two M5Stack Core Ink boards.** Start there. The two MicroPython builds below are kept as references and fallbacks, not as the deployed thing; the Inkplate in particular is retired hardware.

There are three implementations of the same application, and they do not share code:

| | Board | Status |
|---|---|---|
| `arduino/minimed-coreink/` | M5Stack Core Ink | **deployed** — read its `README.md` and `architecture.md` first |
| `main_m5coreink.py` | M5Stack Core Ink | reference / fallback, MicroPython |
| `main.py` | Soldered Inkplate 2 | retired hardware, MicroPython |

`main.py` is a single file with no build step, package manifest or test suite. Neither MicroPython build has a test suite; the Arduino one has no automated tests either, and its `README.md` explains what is verified on hardware instead.

`main_m5coreink.py` is a sibling port of the same application to an **M5Stack Core Ink** (1.54" 200×200 monochrome e-paper, ESP32-PICO-D4, UIFlow2 MicroPython firmware), which has a buzzer and so can sound pump alarms audibly. It is a separate, self-contained file — board-independent logic is duplicated, not shared, and must be updated in both. **Its hardware facts are not the Inkplate's** (different time epoch, no red ink, different fonts, single-precision floats); never copy hardware reasoning between the two files. See `PORTING-M5COREINK.md` for its firmware build, the required `boot_option` NVS setting, the deploy steps, and the on-device testing techniques (module import for display work, stubbing `http_get` to exercise the data path with no network).

Note the fallback is currently theoretical: no patched UIFlow firmware image is kept anywhere, so putting a board back on MicroPython would mean rebuilding one first.

`arduino/minimed-coreink/` is the C++ port of `main_m5coreink.py` and what both boards run. Build and flash with:

```
FQBN=esp32:esp32:m5stack_coreink:PartitionScheme=huge_app
arduino-cli compile --fqbn "$FQBN" --upload -p /dev/ttyACM0 arduino/minimed-coreink
```

`huge_app` is not optional — the default scheme leaves too little room for the WiFi + JSON build. Its `README.md` carries the build details, the measured timings and the hardware quirks; `architecture.md` carries the entry point and file layout. **The duplicate-logic rule spans all three implementations, and the "never copy reasoning between builds" rule bites hardest here**: the same two bytes from the proxy (`""`, a dead token) are rejected outright by ArduinoJson and parsed happily by MicroPython's `json.loads`, so the same-looking check has to be written differently in each. Config lives in NVS (`Preferences`, namespace `minimed`), not in a JSON file — a single setting can be changed without re-entering WiFi credentials by flashing a one-shot sketch that writes that key, then reflashing the real firmware; an app upload does not touch NVS.

## Origin

This started as a redesign of a sibling M5Stack-based project (see the "Credits" note at the top of `main.py`) — the data-polling/config/AP-setup logic and the fault-code lookup tables carried over largely unchanged, but the GUI, timing, and alarm-handling layers are specific to this board's constraints (e-paper, no partial refresh, no speaker, no buttons beyond reset) and were designed from scratch. That history isn't tracked in this repo.

## Running / deploying

There's no local dev/emulation mode — this only runs on the actual Inkplate 2 hardware. Iterate by pushing to the device via [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html) and observing on real hardware; there's no linter/formatter/test command configured.

```
mpremote connect /dev/ttyUSB0 cp main.py :main.py
mpremote connect /dev/ttyUSB0 reset
```

**Important `mpremote` gotcha**: `mpremote exec`/`run` perform an automatic soft-reset on connect (confirmed via `mpremote --help`; there's a `resume` subcommand that skips it, though it can fail with "could not enter raw repl" if the device isn't in a clean REPL state). This means:
- Interactively checking device state (`machine.reset_cause()`, WiFi status, etc.) via `mpremote exec` **will itself trigger a soft-reset first**, so what you observe reflects the state *after* your own connection, not before it. Don't trust `reset_cause()` read this way as evidence of what caused a *previous* wake.
- A device that's mid-cycle (in the middle of the ~17–23s panel refresh, or waiting on a WiFi/NTP/fetch timeout) will look completely unresponsive to `mpremote` for that whole window — this is normal, not a hang. Poll with a retry loop rather than assuming it's stuck.
- If the device is genuinely stuck and `mpremote reset` itself fails (needs REPL cooperation it isn't giving), fall back to a raw hardware reset via direct serial control-line toggling (bypasses MicroPython's REPL protocol entirely, the same mechanism `esptool` uses):
  ```python
  import serial, time
  s = serial.Serial('/dev/ttyUSB0')
  s.dtr = False
  s.rts = True
  time.sleep(0.1)
  s.rts = False
  s.close()
  ```
- Once the device is deployed and running its normal deep-sleep cycle, avoid unnecessary `mpremote` connections — each one forces a soft-reset that interrupts whatever cycle is in progress and defeats the point of leaving it running autonomously. Prefer just watching the panel.

**Power it from a proper adapter, not a PC USB port.** A marginal supply cannot source the WiFi radio's power-up inrush (a few hundred mA), and the rail collapses the moment `wlan.active(True)` runs. Flashing and the REPL draw almost nothing, so a weak port passes every test *except* the one that matters, and the board can run for hours provided the radio is never activated.

The symptom looks exactly like a software hang, which is what makes it expensive: the panel sits on the version splash and updates stop. It is really a reset loop, and self-sustaining — the reset means the device never reaches `machine.deepsleep()`, so the next boot is another cold boot, which redraws the splash and dies at the radio again, forever.

Tell it apart from a real hang by watching the serial console across a hardware reset. A brownout shows repeated `rst:0x1 (POWERON_RESET)` (sometimes `0x10 RTCWDT_RTC_RESET` or `0x3 SW_RESET`); a MicroPython exception would show `SW_CPU_RESET` plus a traceback. To confirm it in isolation, run a few lines that do nothing but `network.WLAN(network.STA_IF)` then `active(True)` — no display, no `main.py`. If that alone resets the board, it is the supply, and no amount of reading the application code will help. Note the panel refresh is a red herring: inserting a settle delay after it changes nothing, and the fault reproduces with the display never touched.

## Architecture

Everything lives in `main.py`, in this order: fault-code tables → config storage (plain JSON file at `/minimed_config.json`, filesystem root is `/` not `/flash/...`) → NTP/HTTP helpers → AP-mode config web server → WiFi connect → data-formatting helpers → alarm handling → pump data polling (`handle_pumpdataupdate()`, builds a plain `state` dict) → display (`draw_screen()`) → `main()`.

**`main()` is a single cycle, not a loop.** `machine.deepsleep(POLL_PERIOD_S*1000)` at the end re-runs the whole script from scratch on the next wake — there is no in-memory state carried between cycles by design (see the "Alarm handling" comment in the file for why that's safe: unlike a typical alarm-dedup scheme, repeating the same alarm banner on every wake while it's still recent is the *correct* behavior here, not something to suppress, since this board has no speaker to sound the alert instead). This is what makes deep sleep straightforward — nothing needs to persist across a restart.

**Read the comment block at the top of `main.py` before changing GUI, timing, HTTP, NTP, storage, or alarm code** — it documents hardware/firmware quirks this design works around (a fixed 16px-tall proportional font rather than a 6x8 cell, no partial e-paper refresh, no `requests`/`urequests` module, a 2000-01-01 time epoch instead of the standard 1970 Unix epoch, no text auto-wrap). Getting these wrong tends to fail silently or look fine until tested against real hardware — several were only caught by deploying and visually inspecting the panel.

### Data source

`handle_pumpdataupdate()` polls `http://<proxyaddr>:<proxyport>/carelink/nohistory` and builds a `state` dict from the JSON response (glucose, pump battery %, reservoir level, sensor state, active insulin, banner state, alarms). If a field's shape/name changes and this monitor breaks, check the actual proxy response format directly (e.g. via `curl`) rather than assuming — the exact schema isn't controlled by this repo.

### Fault codes

`faultIdMapping` (raw Carelink fault ID → canonical ID, many-to-one) and `faultIdTable` (canonical ID → human-readable message) are large, mostly-static lookup tables reverse-engineered from real Carelink pump alarm data — treat them as data, not something to restructure, and extend by adding entries rather than changing their shape.

### DST/timezone

Handled manually via a `dstDelta` offset applied in `local_now()`/`time_delta()`, since MicroPython has no timezone library. Treat DST edge cases as easy to get subtly wrong — this exact mechanism has had multiple regression fixes in its history elsewhere.

### Verification

There's no test suite. Verification is "deploy to real hardware and look at it" — for anything touching the display, WiFi/NTP/HTTP flow, or deep-sleep behavior, push to the device and observe at least one full wake cycle, ideally several, since some failure modes (e.g. a WiFi reconnect that's fine most cycles but occasionally slow after a cold radio init) only show up intermittently.
