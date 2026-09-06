# Architecture

How the Arduino port is put together, and why the entry point is not where an
Arduino sketch normally has one. For build and flash instructions see
[README.md](README.md); for the hardware facts both this and the MicroPython
build depend on, see [../../PORTING-M5COREINK.md](../../PORTING-M5COREINK.md).

## The entry point

Arduino gives you `setup()` and `loop()`. This port uses neither in the usual
way.

```
reset
  └─ ROM + second-stage bootloader                 ~145 ms
      ├─ early_power_hold()      <- __attribute__((constructor))
      │      grabs GPIO12 at 1.7 ms of app time
      └─ app_main (Arduino core)
          └─ setup()             <- the entire application, one poll cycle
              └─ esp_deep_sleep()   never returns
loop()                           <- never runs at all
```

**`early_power_hold()` runs before `setup()`**, before `app_main()` even. It
is a global constructor, which is the earliest hook a sketch can reach, and
it exists for one job: latch the power rail. The rail is held on by GPIO12,
and out of reset that pad is an input. Anything later — `M5.begin()`, say —
and the board switches itself off on battery after a reset, taking the
self-recovery property with it. Measured 147 ms from a software reset, which
is inside the rail's coast; verified on battery with a restart loop.

**`setup()` is one poll cycle, not an initialisation routine.** It ends in
`esp_deep_sleep()`, which does not return, so the next wake re-enters
`setup()` from the top. `loop()` is empty and unreachable. This mirrors
`main()` in `main_m5coreink.py`, where `machine.deepsleep()` re-runs the whole
script: a cycle, not a loop, carrying no in-memory state across a wake by
design.

## The files

| file | lines | contents |
|---|---|---|
| `minimed-coreink.ino` | 840 | the sketch: power, config, network, parsing, alarms, toggle, sleep |
| `screens.cpp` / `.h` | 547 + 61 | all four screens and the drawing helpers |
| `faults.cpp` / `.h` | 253 + 16 | pump fault code tables, **generated** |
| `portal.cpp` / `.h` | 189 + 15 | the AP setup portal |
| `types.h` | 66 | `Config`, `State`, `state_init()` |

### `minimed-coreink.ino`

Sectioned by banner comments, in the order the cycle uses them:

| section | what |
|---|---|
| hardware | the GPIO12 power-hold constructor, pad-hold release |
| timing | poll period, WiFi/HTTP timeouts, the clock-validity epoch |
| config | `config_read()` from NVS via `Preferences` |
| http/time | `http_get()`, `parse_http_date()`, `set_clock()` |
| alarms | `resolve_alarm()` and the local-wall-clock timestamp handling |
| buzzer | `beep()`, including waiting out its own note |
| polling | `fetch_pump_data()` — the ArduinoJson parse |
| display | `compose()`, plus diagnostics behind `DEBUG_ASCII` |
| toggle | the IRQ latch, `arm_toggle_wake()`, `run_toggle_session()` |
| rtc state | the `RtcState` struct in `RTC_DATA_ATTR` |
| main | `sleep_until()` and `setup()` |

### `types.h`

`State` is deliberately a plain POD with **no default member initialisers**,
initialised by `state_init()` instead. It lives in RTC memory, and a member
with an initialiser makes the enclosing struct non-trivially-constructible —
at which point the compiler emits a dynamic initialiser that runs at startup
on *every* boot, deep-sleep wakes included, silently wiping it. That bug
shipped once and reached the panel: the plain fields beside it (`magic`,
`cycle`, `next_poll`) persisted correctly while the snapshot was cleared on
every wake, so the secondary screens always drew `--`.

### `screens.cpp`

Every drawing function takes a `LovyanGFX&`, the common base of both `M5GFX`
(the panel) and `M5Canvas` (the off-screen sprite). That is the
statically-typed equivalent of the MicroPython build's `gfx()` indirection,
and it exists so the composed and direct-to-panel paths cannot drift apart.

Note the fonts are **not** the ones `main_m5coreink.py` uses. M5GFX reports
different metrics to C++ than through its MicroPython binding — DejaVu72 is
50 px there and 75 px here — so layout constants must not be copied between
the builds. See README.md.

### `faults.cpp`

Generated from `main_m5coreink.py`'s `faultIdMapping` (137 entries) and
`faultIdTable` (63), plus the low/high glucose id sets. Hand-transcribing 200
lines of reverse-engineered pump data is how one entry ends up silently
wrong, and the two builds must not drift. **Regenerate rather than edit.**

### `portal.cpp`

Only reached when the config is incomplete — which in practice means a device
that has never been set up, so somebody is standing in front of it. That
assumption is why it blocks indefinitely rather than timing out, and why it
is silent despite the board having a buzzer: the buzzer is reserved for pump
alarms, and chirping for routine setup would train the caregiver to ignore
it.

A WiFi *failure* deliberately does not come here. That would strand a working
monitor in setup mode over a transient hiccup.

## One cycle, end to end

```
setup()
 |- release pad hold, Serial, pin TZ to UTC
 |- read RTC state -> cold boot? wake cause?
 |- M5.begin(clear_display = true)     <- the anti-ghosting clear, ~1.9 s
 |- config_read()  -> empty? run_config_portal(), never returns
 |
 |- TOGGLE WAKE --> advance screen, compose() from rtc.snap   (no network)
 |                  run_toggle_session()  -> sleep_until(next_poll)
 |
 `- SCHEDULED POLL
     |- WiFi connect (~1.2 s), capture the IP while the radio is up
     |- fetch_pump_data() -> Date header sets the clock, then parse
     |- radio off (it holds heap the canvas wants)
     |- beep() if an alarm is live
     |- rtc.snap = state; next_poll = now + 300
     |- compose(SCREEN_MAIN, ...)
     `- sleep_until(next_poll)
```

## Invariants worth keeping

**The two paths draw from different sources.** The scheduled poll draws from
freshly fetched state; the toggle path draws from `rtc.snap` in RTC memory.
Exercising one proves nothing about the other — both bugs found by looking at
the panel rather than the console lived on the toggle side, while every log
line looked perfect.

**`sleep_until()` sleeps only until the next poll is due**, clamped to
5..300 s, so flicking through screens can never push the next reading out.
The cadence anchors on the fetch, not on the wake, so a slow tail cannot
shift the schedule.

**Never arm a wake pin that is already low.** ext0/ext1 are level-triggered,
so arming a held-down pin makes deep sleep return immediately and the board
spins wake/redraw/sleep until the battery is flat — which is what a switch
resting off-centre looks like.

**The panel is cleared on every wake.** Not an oversight to optimise away:
without it the display ghosts until a changed screen cannot be read, and no
choice of waveform substitutes. It costs ~1.9 s of the 4.28 s cycle.

**The beep waits out its own note.** `tone()` returns immediately, and the
deep sleep at the end of the cycle would otherwise cut power mid-note — an
inaudible alarm that looks entirely fine in the logs.
