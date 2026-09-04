# M5Stack Core Ink port

`main_m5coreink.py` is a sibling port of `main.py` (Soldered Inkplate 2) to the
**M5Stack Core Ink** — 1.54" 200×200 monochrome e-paper, ESP32-PICO-D4, with a
buzzer. The two files are independent; see "Keeping the two in sync" below.

## Why this board

The Inkplate version's header records its one real shortcoming: *"Alarms are
shown as a visual banner only — this board has no speaker, so a caregiver not
looking at the display gets no alert. Accepted tradeoff for this device."*

The Core Ink has a buzzer, so this port resolves that: an active alarm sounds
one loud beep per update cycle, in addition to the on-screen banner.

## Screens

Four screens, cycled endlessly by the three-position switch (labelled
G37/G39/G38 on the case). Up (GPIO37) and down (GPIO39) both advance;
press (GPIO38) is unused.

**1 — Main.** Glucose reading with trend arrows, active insulin, update time.
The reading is drawn large (86px) whenever there is no alarm, with insulin
and update time anchored to the bottom edge. When an alarm *is* active the
reading shrinks to 50px to free the bottom strip for the alarm, shown in
reverse video across two lines, with the buzzer sounding once per cycle.
Out-of-range readings invert the whole glucose band — this panel has no red
ink to colour the number with.

```
   no alarm                        alarm
+------------------+          +------------------+
|                  |          |  54  vvv         |
|  142  ^^         |          |  mg/dL           |
|                  |          |  --------------- |
|  mg/dL           |          |  Act. insulin    |
|  --------------- |          |  Updated 4 min   |
|                  |          |                  |
|  Act. ins  2.4 U |          | ################ |
|  Updated   09:55 |          | ## Low Sensor ## |
+------------------+          +------------------+
```

**2 — Glucose, last 24 h.** Time in target / above / below as percentages,
plus average SG, over a stacked bar. With no colour available the segments
are distinguished by fill, heaviest ink on the dangerous end: below-target
solid black, in-target hatched, above-target open. Fed by the proxy's
`timeInRange` / `aboveHyperLimit` / `belowHypoLimit` / `averageSG` fields —
the same four the sibling M5Stack project puts on its screen 2.

**3 — Pump & sensor.** Patient name, insulin remaining in the reservoir
(units and percent), sensor life left, and pump battery. Supplies rather than
readings: none of it is urgent enough for the main screen, all of it is what
you want to know before leaving the house. Values are drawn in the larger
font — four rows where the next screen has eight, so the room is there.

The patient name comes from the config (`patientname`), and falls back to the
proxy's own `firstName` when that is unset. The fallback is the important
half: the proxy already knows whose pump it is, so a device that has never
been told a name still shows the right one. The setting exists to override it
— a nickname, or two pumps in one house. It is deliberately *not* part of the
"config is incomplete, start AP mode" test, or upgrading an existing device
would strand it in setup mode over a cosmetic label.

**4 — Device & network.** Battery first (the only line that changes on its
own, and the one that predicts the device dying), then runtime on this
charge, the current time, Wi-Fi SSID, IP, proxy address and port, and the
app version. Eight rows, which is what fits.

NTP server and timezone used to be here and were dropped to make room. Both
are write-once settings readable from the config page; battery and runtime
change by themselves, which is what a status screen is for.

**Runtime** counts from the start of this power-on session, held in RTC
memory (`session_start`) and carried across deep-sleep wakes. It is meant to
answer "how many hours does a charge last", and it is deliberately *not*
"time since USB was unplugged" — this board cannot know that.
`Power_Class::isCharging()` has branches for the Paper, StickS3 and Tab5;
CoreInk falls through to `charge_unknown`, because there is no charge-status
pin and no PMIC to ask. Time since cold boot is the honest approximation:
deep-sleep wakes preserve RTC memory, and only applying power or resetting
clears it. Reset the board while it is on USB, then unplug, and the two
numbers are the same.

### How the switch works across deep sleep

The device is asleep almost all the time, so the switch has to wake it.
Waking on *either* direction needs both of the ESP32's GPIO wake sources:
ext0 takes a single pin, and ext1's only modes are ALL_LOW (every listed pin
low at once) and ANY_HIGH (these idle high, so it would fire immediately) —
neither expresses "either of two active-low pins". They are independent, so
ext0 is armed on GPIO37 and ext1 on GPIO39. Verified on hardware: up reports
`wake_reason` 2, down reports 3.

A button wake does **not** touch the network. It redraws from a snapshot of
the last fetch held in RTC memory, so the screen changes immediately instead
of waiting several seconds for Wi-Fi. After a press the device stays awake
(`TOGGLE_AWAKE_MS`, 15 s, restarted by each press) so a run of flicks redraws
at panel speed rather than paying a ~2.7 s firmware boot per screen — long
enough to read a screen and think before it gives up on you.

A *scheduled* refresh deliberately opens no such window: it draws and sleeps
immediately. That window used to exist and cost more than it was worth —
being paid on every poll, ~283 times a day, whether or not anyone was there:

| | with the window | without |
|---|---|---|
| awake per cycle | 11.3 s | **5.3 s** |
| awake per day | ~53 min | **~25 min** |
| duty cycle | 3.7 % | **1.7 %** |

The only loss is that the first flick after a scheduled refresh waits out a
boot (~2.7 s) instead of landing instantly; every flick after it is instant.
Measured on hardware, poll cadence unchanged.

Of the 5.3 s that remain, roughly 2.0 s is firmware and VM startup before a
line of this file runs, 2.1 s is Wi-Fi association, and the actual work —
fetch, alarm beep, panel redraw — is about 0.4 s. The radio is powered for
only ~2.6 s of the cycle; `wlan.active(False)` runs before the draw, both to
free the ~45 KB the canvas needs and to keep the tail cheap.

Measured individually, which is worth knowing before optimising the wrong
thing: `wlan_connect()` **2081 ms**, `ntp_sync()` **64 ms**, `http_get()`
**15 ms** for a 2.9 KB response. Association dominates and is radio-bound.

### The clock comes from the proxy, not from NTP

`http_get()` returns the epoch parsed out of the response's `Date:` header,
and `handle_pumpdataupdate()` sets the clock from it before parsing the body.
It is free — the header arrives with a response the device was fetching
anyway — needs no DNS, and re-syncs every five minutes, so the clock cannot
drift between syncs.

NTP is only a fallback, run when `time.time()` is below `TIME_VALID_EPOCH`,
which in practice means a device that has never had a clock. The reason is
not the 64 ms of a successful sync; it is the failure path, which retries ten
times at one-second intervals and so costs **up to 10 s** — doubling a cycle
— on a network where DNS is filtered. Sourcing the time from the proxy takes
DNS and an external server out of the five-minute path entirely.

The trade is that the monitor's clock now depends on the proxy host being
correctly synced. That host is the machine already deciding what "now" means
for every reading it serves, so this couples two things that were always
related; verified against the live proxy, its `Date` matched an
independently-synced machine to the second.

The clock is set *before* the body is parsed, because the alarm logic
compares alarm timestamps against now — so even a device booting with no
clock at all gets a correct one before anything depends on it. Any header
that does not parse exactly returns `None` and is ignored rather than
guessed at: a confidently wrong clock would silently corrupt "Updated N min
ago", which is the number that tells a caregiver whether to trust what is on
the screen.

RTC memory also carries the current screen, the time the next poll is due,
and a refresh counter. None of it is required for correctness — it may be
empty or corrupt at any moment and every reader degrades to "no data".
Sleeps are shortened to the *remaining* time until the next poll, so idly
flicking through screens can never starve the data refresh.

A scheduled refresh always returns to screen 1: it is the only screen that
shows the alarm banner, and parking on stats or settings could otherwise
hide an active alarm.

## Hardware facts confirmed on the device

These were measured by probing the actual board, not assumed. Several differ
from `main.py`'s documented facts — **do not copy hardware reasoning between the
two files.**

| Fact | Inkplate 2 (`main.py`) | Core Ink (`main_m5coreink.py`) |
|---|---|---|
| Panel | 212×104, 3-colour (red ink) | 200×200, monochrome |
| Time epoch | 2000-01-01 (`EPOCH_ADJUST_S = 946684800`) | **Unix 1970 (`EPOCH_ADJUST_S = 0`)** |
| Alerting | visual banner only, no speaker | buzzer on GPIO2 via `M5.Speaker` |
| Text | proportional 16px `gfx_standard_font_01` | M5GFX bitmap fonts, max height **50px** |
| Refresh | ~17–23 s | ~37 ms per screen (see below) |
| HTTP | no `requests` module | `requests2` present (unused — see below) |
| Power | none needed | power-hold latch GPIO12, asserted 50 ms into boot (patched firmware; stock ~2 s) |
| Buttons | reset only | toggle 37 / 39 / 38, EXT 5, PWR 27, back button = bare EN reset |

Two of these bit during the port and are worth calling out:

* **Epoch.** `time.localtime(0)` returns `(1970,1,1,...)` here, so
  `EPOCH_ADJUST_S` is `0`. Carrying `main.py`'s `946684800` across unchanged
  dated every reading 30 years early while looking perfectly plausible.
* **Single-precision floats.** `1000000000001.0` reads back as
  `1000000000000.0`. Carelink's `lastConduitUpdateServerDateTime` is Unix time
  in *milliseconds* (~1.79e12), where a float32 mantissa only resolves to ~64
  seconds — so `int(ms/1000)` decoded a payload stamped `11:51:00` as
  `11:50:56`, and since `time_delta()` compares whole minute fields that
  reported *"5 min ago"* for a 4-minute-old reading. This port uses integer
  division (`// 1000`). `main.py` carried the same `/1000` form and was fixed
  the same way, after the error was confirmed against a live payload there too.

`requests2` is available but the port keeps the hand-rolled socket `http_get()`
anyway, because `settimeout()` gives a hard upper bound on how long a cycle can
block. On an unattended monitor a fetch that hangs forever isn't a slow cycle,
it's a dead device showing a stale reading.

### On battery, the back button is an off switch

The rail is latched on by GPIO12, and the *firmware* asserts it — inside
M5GFX's board autodetect (`_pin_level(GPIO_NUM_12, true);  // POWER_HOLD_PIN
12`), reached from the `M5.begin()` that UIFlow performs during its own boot.
Measured on this device: **between 1.65 s and 3.06 s after reset** — the pin
already reads driven at 3.06 s, and the earliest M5 activity on the console
is at 1.65 s. A probe placed as the
literal first line of `main.py` already found the pin driven (`ENA12=1`)
before `mm` was even imported, so no arrangement of application code can
establish the latch any earlier than the firmware already does.

For those seconds after every reset, nothing holds the rail but whatever is
feeding it from outside — USB, or a finger on the PWR button:

| Button | GPIO | Pressed on USB | Pressed on battery |
|---|---|---|---|
| toggle up / down / press | 37 / 39 / 38 | app input | app input |
| EXT | 5 | unused | unused |
| PWR | 27 | unused | **press to switch the board on** |
| back | — (EN) | resets | **switches the board off** |

The back button is a bare EN reset: pressing it prints `rst:0x1
(POWERON_RESET)` and does nothing else. It has no path to the power latch, so
on battery it drops the rail and cannot restore it — and holding it only
parks the chip in reset. The way back is the **PWR button**, which powers the
rail for as long as it is down and long enough after for the firmware to take
the latch over. On the patched firmware a brief tap is enough; on stock it was
only ever tried as a ~3 s hold, which works. USB always revives the board too,
because it feeds the rail directly.

**No firmware can fix the button**, and it is worth being precise about why,
because it is not a speed problem. EN stays low for as long as the button is
held — 100–300 ms for a human press — and a chip sitting in reset cannot
drive any pin at all. Booting faster only shortens the part *after* the
button comes back up. M5's factory Arduino firmware, which takes the latch at
~20 ms of app time, loses this race in exactly the same way.

A reset that does *not* hold EN low is a different story, and it is the one
that matters for an unattended monitor: a crash, a watchdog, or the
`machine.reset()` fallback at the end of `main()`. There GPIO12 is undriven
only for the boot itself — 50 ms with the firmware patch below, and that is
short enough. **Measured on battery: a `machine.reset()` loop survives
indefinitely**, a counter held in RTC memory climbing across reboot after
reboot with the panel redrawing each time. The stock arm of that experiment
was never run, so "stock dies here" is inference — but a 1.6–3 s window
cannot survive what a few hundred milliseconds already kills.

Together the two results bracket the rail's coast time between ~50 ms and
~100 ms. That is the entire physics of this fault: one capacitor, and how
long GPIO12 is left floating.

E-paper holds its last image with **no power at all**, so a board that has
switched off looks exactly like one frozen mid-screen. Watch the serial
console to tell them apart: a running board still prints its cycle.

### Never draw straight to `M5.Lcd`

M5GFX drives this e-paper panel **on every individual drawing call**, so a
screen built from a few dozen primitives triggers a few dozen panel updates
and visibly redraws the whole way through. Measured drawing direct: 7.7s for
the main screen and 18.9s for the stats screen, whose hatched bar is a loop
of `drawLine` calls. That also made the switch feel dead — nothing can poll
it during seconds of blocking redraw.

Every screen is therefore composed into an off-screen canvas and pushed once,
via `compose()`. Same content, **~37ms**. 4bpp is the sweet spot: 1bpp needs
a format conversion on the way out and pushes in ~340ms, while 4bpp and 8bpp
both push in ~25ms and 4bpp costs half the RAM (this board has no PSRAM).

Two related traps:

* `gc.collect()` costs ~150ms here — more than composing and pushing a whole
  screen. It is only called when the heap actually looks too tight to
  allocate into, never on the fast path.
* The full-clear waveform (the visible flash) is **not** needed on every
  wake; `EPD_FAST` writes the same image without it. It is needed
  periodically, or fast-waveform residue accumulates into permanent
  ghosting. So it runs every `FULL_REFRESH_EVERY` scheduled redraws (12 ≈
  hourly) and never on an interactive one, where someone is watching.

Presses are latched by a pin IRQ rather than discovered by polling, so a
flick that lands *during* a redraw still registers instead of being lost.

### The flash on every update is inherent — don't chase it

`M5.begin({"clear_display": False})` is still worth passing: by default
`M5.begin()` resets the panel and ends with `M5.Display.clear()`, which is an
extra full-panel refresh on every wake. Suppressing it halves the panel work
per cycle.

But it does **not** remove the flash, and nothing in this file can. From
`Panel_GDEW0154M09::display()`:

* every update ends with `writeCommand(0x12)` — DRF, the panel's refresh —
  and there is no code path that skips it;
* `epd_mode` changes only *which* waveform runs. `epd_quality` adds a second
  pass that re-writes the old image (`0x10`) before the new one (`0x13`);
  the fast modes do a single pass. Both still issue DRF;
* the refresh covers the rectangle that changed (`_exec_transfer` does use
  the partial-window commands `0x91`/`0x90`), and since every redraw here
  pushes the whole 200×200 canvas, that rectangle is the whole panel.

So the flash is the controller's built-in waveform doing a full black/white
inversion sweep. The only way to shrink it would be to push dirty
sub-rectangles instead of the whole canvas — real, but a substantial
redesign, and the flash doubles as a useful "it just updated" cue.

**It is also good for the panel.** E-paper needs pixels fully re-driven
periodically or residue accumulates into permanent ghosting; the
`epd_quality` two-pass path exists precisely to do that. That is what
`FULL_REFRESH_EVERY` schedules (~hourly): the rest of the time the cheaper
single-pass waveform runs. Turning the periodic quality pass off would trade
a cosmetic flash for gradual, permanent image retention — a bad bargain on a
display meant to stay legible for years.

Note `_refresh_msec = 320`, and `display()` does not block waiting for the
refresh to finish — it returns after issuing DRF and waits at the *start* of
the next update. That is why a canvas push measures ~37ms while the panel
visibly takes about a second.

## Reliability: the device must never stop polling

The worst failure for this device is not a wrong reading, it is a board that
is awake, showing a stale screen, and never polling again — indistinguishable
from a working monitor until someone notices the timestamp has frozen. Three
routes to it were found and closed; keep them closed.

**1. Anything that escapes `main()`.** If `main()` returns without reaching
`deepsleep()`, MicroPython drops to an idle REPL and the board sits there
until physically reset. `M5.begin()`, the cold-boot splash and
`rtc_state_load()` were originally *outside* the try/except that was supposed
to prevent exactly this (the comment claimed otherwise). They are inside it
now, and the tail of `main()` is unconditional: save state, sleep, and if
`deepsleep()` somehow does not take, sleep blind and finally `machine.reset()`
rather than fall through. The boot stub wraps `import mm` for the same reason.

**2. Unbounded waits.** `while toggle_pressed()` had no timeout, so a held or
failed contact parked the device there forever. Every loop in the file is now
bounded except the AP-mode config server, which only runs with a person
standing at the device. There is also a hard cap on one awake session
(`TOGGLE_SESSION_MAX_MS`) that per-press extensions cannot defeat.

**3. Sleeping without holding the power rail — kills the board on battery.**
GPIO12 latches the board's power rail on, and reads high the whole time the
device runs. ESP32 deep sleep disables GPIO output drivers, so the latch
releases the moment you sleep and, on battery, the board **powers off
completely**: it never wakes, the back button does nothing (there is no rail
left to reset — see *On battery, the back button is an off switch* above, and
hold PWR for three seconds instead), and the panel fades as its bias collapses. On USB the
fault is invisible, because USB feeds the rail directly — which is exactly
why it looked like a mysterious "locks up only when unplugged" bug.

`sleep_until()` therefore asserts GPIO12 and calls
`esp32.gpio_deep_sleep_hold(True)` before sleeping, and `main()` releases
that hold as its first action (held pads cannot be re-driven).

GPIO12 is also the MTDI strapping pin that selects flash voltage at boot,
which is normally a reason *not* to hold it high — but this board tolerates
it: 13 consecutive cycles woke with `reset_cause=DEEPSLEEP_RESET` and GPIO12
still reading 1. If a future board or firmware fails to boot after sleeping,
suspect this first; pulling EN low (reset button or esptool) clears the hold
by power-cycling the RTC domain.

Two alternatives were measured and rejected. `M5.Power.deepSleep()` sleeps
the panel but never touches `power_hold`, so it dies on battery exactly like
a bare `machine.deepsleep()`. `M5.Power.timerSleep()` does survive — it
powers the board down and lets the RTC switch it back on — but every wake is
then a cold boot (`cause=2`) with no RTC memory and no GPIO wake, which would
cost the toggle switch entirely.

**4. Leaving the panel powered through sleep.** Call `M5.Lcd.powerSaveOn()`
before sleeping and `powerSaveOff()` after, or the panel's booster and VCOM
stay energised: current is wasted and the image drifts into a washed-out,
half-transparent version of itself. Use `powerSave`, **not** `sleep` —
`setPowerSave` issues Power OFF (`0x02`) alone, while `setSleep` also sends
DSLP (`0x07`/`0xA5`), and leaving DSLP needs a full panel reset that
`clear_display=False` deliberately skips. (`M5.Lcd.sleep()` is not exposed in
MicroPython anyway; only `powerSaveOn`/`powerSaveOff` are.)

**5. Level-triggered wake on a pin that is already low.** `ext0`/`ext1` wake
on a *level*. Arming a pin that is currently held down makes `deepsleep()`
return immediately, every time — the board would spin wake→redraw→sleep as
fast as it can boot and flatten the battery in hours. `arm_toggle_wake()`
therefore refuses to arm a pin reading low, and `run_toggle_session()` stops
advancing when the switch does not spring back. (The fitted switch is
momentary and does spring back; this guards a failed contact.)

No hardware watchdog is fitted. It is the obvious catch-all, but on this port
`machine.WDT` risks interacting badly with deep sleep, and an untested reset
mechanism in an unattended monitor can turn one bug into a reboot loop. If a
stall ever appears that the above cannot explain, that is the next step —
tested against deep sleep first.

## Firmware

The board runs **UIFlow2 MicroPython** (`uiflow_micropython`), built for the
`M5STACK_CoreInk` board — the same firmware family as the sibling M5Stack Fire
project, which is why the port targets `M5.Lcd`/`M5.Speaker` rather than raw
`framebuf` plus a hand-written panel driver.

Build (needs ESP-IDF; the repo asks for v5.4.2, v5.5.1 also works):

```bash
cd ~/uiflow_workspace/esp-idf && . ./export.sh
cd ~/uiflow_micropython/m5stack
make BOARD=M5STACK_CoreInk
# -> build-M5STACK_CoreInk/uiflow-<hash>.bin  (single image, flash at 0x0)
```

### The power-hold patch — apply before building

`firmware/coreink-power-hold.patch` must be applied to the
`uiflow_micropython` tree before building. Without it a crash on battery
leaves the board switched off until someone finds it and plugs in USB — see
*On battery, the back button is an off switch* above.

```bash
cd ~/uiflow_micropython
git apply .../inkplate-minimed-monitor/firmware/coreink-power-hold.patch
```

Two independent changes, both under `boards/M5STACK_CoreInk/`:

1. **`board_init.c` plus `MICROPY_BOARD_STARTUP`** — drives GPIO12 high as
   the first statement of `app_main`, before the MicroPython task is even
   created. Stock UIFlow leaves the latch to M5GFX's board autodetect,
   reached incidentally through `M5.begin()`. (M5Unified *does* assert a hold
   pin deliberately for the Timer Cam in `Power_Class::begin()`; the Core Ink
   branch of that same switch sets ADC and wake pins and never touches
   `power_hold`. On Arduino, where `M5.begin()` runs in the first
   milliseconds, the omission is invisible.)
2. **`CONFIG_BOOTLOADER_SKIP_VALIDATE_ON_POWER_ON=y`** — the hook alone only
   reached 980 ms, because the bootloader SHA-256s the entire 3.4 MB image on
   every power-on reset. Deep-sleep wakes already skipped that
   (`..._IN_DEEP_SLEEP=y` was already set), so the five-minute cycle never
   paid this cost; only resets did. There is a single `factory` partition and
   nothing to roll back to, so a failed validation was never recoverable
   anyway.

This is worth sending upstream — it affects every Core Ink running UIFlow2 on
battery, not just this project. A branch and a draft description are ready and
**not yet submitted**: see `firmware/upstream-pr.md`.

Measured on the device, from reset to the latch being asserted:

| Firmware | Latch asserted |
|---|---|
| UIFlow2 stock | ~2000 ms |
| M5 factory (Arduino) | ~400 ms |
| patch, board hook only | ~980 ms |
| **patch, both changes** | **50 ms** |

The `printf` in `board_init.c` is what makes this observable — `ESP_LOGI`
would be invisible, since this firmware builds with the default log level at
ERROR. Watch for it on the console at every boot:

```
coreink: power hold (GPIO12) asserted at 50 ms
```

**Gotcha:** the sdkconfig change will not take effect on an existing build
tree. `sdkconfig.defaults` entries are ignored once a value is recorded in
`build-M5STACK_CoreInk/sdkconfig` — the build succeeds and silently keeps the
old setting. Delete that file and rebuild.

Flash it:

```bash
esptool --chip esp32 --port /dev/ttyACM0 --baud 921600 \
        write-flash 0x0 build-M5STACK_CoreInk/uiflow-<hash>.bin
```

### Skipping the UIFlow launcher — required

Out of the box the NVS key `boot_option` is `1`, which starts the UIFlow
launcher: it takes over the screen **and holds the REPL**, so `main.py` never
runs and `mpremote` reports `could not enter raw repl`. Set it to `0` ("run
main.py directly"):

```bash
cd ~/uiflow_micropython/m5stack
sed 's/^boot_option,data,u8,1$/boot_option,data,u8,0/' partition_nvs.csv > /tmp/nvs0.csv
. ~/.espressif/python_env/idf5.5_py3.12_env/bin/activate
python ../tools/nvs_partition_gen.py generate /tmp/nvs0.csv /tmp/nvs0.bin 0x6000
esptool --chip esp32 --port /dev/ttyACM0 write-flash 0x9000 /tmp/nvs0.bin
```

(4 MB partition map: `nvs` @ `0x9000`, `factory` @ `0x10000`, `vfs` @ `0x371000`.)

## Memory: why the app ships precompiled

**Do not deploy `main_m5coreink.py` as `main.py`.** It must be cross-compiled
to `mm.mpy` and loaded from a two-line stub. This is a hard requirement, not
a preference, and the reason is worth understanding before changing it.

Three things compete for the ESP-IDF heap — MicroPython's own GC heap, the
Wi-Fi stack (~45KB), and the 20KB off-screen drawing buffer — and the board
does not have room for a careless arrangement of all three. Importing the
~82KB source makes MicroPython compile it at boot, and the GC heap it grows
to do that is taken from the IDF heap and **never returned**. Measured
largest-contiguous-free-block on this device:

| | source `.py` | precompiled `.mpy` |
|---|---|---|
| after import | 30.7 KB | 55.3 KB |
| Wi-Fi associated | 1.9 KB | 51.2 KB |
| after a few sockets | 0.3 KB | 49.2 KB |

With the source, there is no ordering that works: allocate the drawing
buffer late and it fails *inside M5GFX as a C++ `abort()`* — uncatchable by
Python, the device just reboots — and allocate it early and Wi-Fi refuses to
start with `OSError: WiFi Out of Memory`. Both were observed. With `.mpy`
there is comfortable room for everything.

`compose()` still checks the largest free block before allocating and falls
back to slow direct drawing if it looks tight, because an abort cannot be
caught and must therefore be avoided rather than handled.

## Deploying the application

```bash
MPYC=~/uiflow_micropython/micropython/mpy-cross/build/mpy-cross
$MPYC -O2 -o mm.mpy main_m5coreink.py
printf 'import mm\nmm.main()\n' > main.py
mpremote connect /dev/ttyACM0 cp mm.mpy :mm.mpy
mpremote connect /dev/ttyACM0 cp main.py :main.py
mpremote connect /dev/ttyACM0 reset
```

`cp` alone is **not** enough — after a copy `mpremote` leaves the board sitting
in the REPL, and `main.py` only runs on a hard reset or power-on. Without the
explicit `reset` the new code silently doesn't run.

### When the REPL is unreachable

Once the app is running its normal cycle it is asleep almost all the time,
and `mpremote` can rarely catch the ~1s REPL window. Rather than fight it,
rebuild the whole user filesystem and flash that partition — this needs no
REPL cooperation at all, because `esptool` resets into the ROM bootloader:

```bash
# stage main.py, mm.mpy, minimed_config.json and res/ into a directory, then:
python ~/uiflow_micropython/tools/fs_packed.py \
    ~/uiflow_micropython/tools/littlefs/prebuilt/littlefs2 \
    coreink <stage-dir> fs-user.bin \
    ~/uiflow_micropython/m5stack/build-M5STACK_CoreInk/partition_table/partition-table.bin
esptool --chip esp32 --port /dev/ttyACM0 write-flash 0x371000 fs-user.bin
```

Gotcha: `fs_packed.py` picks the target partition by looking for the string
`user` (or `system`) **in the output filename**. Name the output anything
else and it silently does nothing — no error, no file.

First boot has no config and drops into AP mode: join Wi-Fi `M5INK_MINIMED_MON`
(password `123456789`), open `http://192.168.4.1`, fill in Wi-Fi / NTP /
timezone / Carelink proxy address, save. The device stores
`/flash/minimed_config.json` and reboots. To force setup again:

```bash
mpremote connect /dev/ttyACM0 exec "import os; os.remove('/flash/minimed_config.json')"
```

## Testing on hardware

There's no test suite; verification is "deploy and look at it". Two things make
that less painful than on the Inkplate:

**1. Import the file as a module to exercise the display without running the
poll cycle.** `main()` is guarded by `if __name__ == "__main__"`, so copying it
under another name lets you drive `draw_screen()` with a hand-built state:

```bash
mpremote connect /dev/ttyACM0 cp main_m5coreink.py :mm.py
mpremote connect /dev/ttyACM0 exec "
import mm, M5; M5.begin()
s = mm.new_state(); s['sg']=54; s['trend']='DOWN_TRIPLE'; s['active_insulin']=2.4
s['alarm_text']='Low Sensor Glucose. Check BG'; s['alarm_tm']=(2026,8,25,11,51,0,0,0)
mm.beep(); mm.draw_screen(s)"
```

**2. Stub `http_get` to test the whole data path with no network.** Assigning
`mm.http_get = lambda h,p,path,timeout_s=30: (200, body)` drives
`handle_pumpdataupdate()` through JSON parsing, the alarm staleness rules and
the epoch conversion against canned Carelink payloads. This is how the epoch and
float-precision bugs above were caught. Pin the clock first with
`RTC().datetime(...)` so the "N min ago" arithmetic is deterministic, and
remember `lastAlarm["dateTime"]` digits are **local wall-clock**, not UTC.

Delete the `mm.py` copy afterwards so a stale duplicate can't confuse things.

### `reset_cause()` cannot be read over mpremote

`mpremote exec` soft-resets on connect, so `machine.reset_cause()` read that way
always reports the soft reset (5), never what actually caused the previous boot.
To observe the real value, have the device record it itself — a temporary
`main.py` that appends `machine.reset_cause()` to a file, sleeps briefly, and
**stops after N boots** so it can't strand the board in a sleep loop. Measured
that way: power-on gives `2`, deep-sleep wake gives `4` (`DEEPSLEEP_RESET`),
which is what `main()`'s cold-boot splash logic relies on.

## Troubleshooting: reaches the internet but not the proxy

If the panel shows a reading of `---` and the log says `Pump data fetch
failed: [Errno 116] ETIMEDOUT` while Wi-Fi and NTP both succeed, the device
is associated but not permitted to talk to the LAN. Diagnose from the device
rather than guessing, since a host-side `ping` proves nothing (the board is
in deep sleep almost all the time):

```python
w = network.WLAN(network.STA_IF)
print(w.ifconfig())            # ip, mask, gateway, dns
# then try TCP to: the gateway, the proxy, and a raw public IP like 1.1.1.1
```

Observed on this network: the gateway connected in ~19ms, while the proxy,
the host's other interface **and `1.1.1.1` by raw IP** all timed out, and a
DNS lookup of `example.com` failed. Reaching only the gateway, from a MAC
the network has never seen before, is the signature of device-level access
control (a DNS/ad blocker doing DHCP with a "new device" policy, or AP
client isolation) — not a bug in this code and not something the port can
work around. Approve the board's MAC in whatever does that on the network.

## Keeping the two in sync

Board-independent logic is duplicated between `main.py` and
`main_m5coreink.py` and must be updated in both:

* `faultIdMapping` / `faultIdTable`
* config storage + the AP-mode config server
* `wlan_connect()`, `handle_pumpdataupdate()`, `get_alarm_text()`,
  `convert_datetimestr_to_epoch()`, `time_delta()`
* the alarm staleness rules and their fault-ID sets

Deliberately **not** shared: the display layer, fonts and geometry, the buzzer,
`EPOCH_ADJUST_S`, and the millisecond→second conversion.
