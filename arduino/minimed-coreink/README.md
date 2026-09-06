# Core Ink monitor — Arduino port

A C++ port of `main_m5coreink.py`, in progress. The MicroPython build remains
the reference implementation and stays deployed on the second Core Ink until
this reaches parity; board-independent logic is duplicated across the two,
not shared.

**Read `../../PORTING-M5COREINK.md` first.** Its hardware facts are
language-independent — the power latch, the panel's refresh behaviour, the
toggle wake rules, the epoch, the UIFlow `boot.py` trap — and this port
depends on every one of them.

## Status

| Phase | | |
|---|---|---|
| 0 | toolchain, splash, power-hold gate | done |
| 1 | config, WiFi, fetch, clock, parse | done |
| 2 | main screen | done |
| 3 | deep sleep, RTC state, toggle | done |
| 4 | remaining screens, fault tables, alarms | done |
| 5 | AP config portal | done |
| 6 | soak and cutover | in progress |

All four screens confirmed against live pump data on the panel. Two bugs
found that way and only that way, both invisible from the serial console:
the RTC snapshot was re-initialised on every wake (so every toggle wake drew
the secondary screens from nothing), and the IP was read after the radio had
been powered down. See `1429da6` - the lesson is that the scheduled-poll path
and the toggle path draw from different sources, and testing the first says
nothing about the second.

## Build and flash

```bash
FQBN=esp32:esp32:m5stack_coreink:PartitionScheme=huge_app
arduino-cli compile --fqbn "$FQBN" .
arduino-cli compile --fqbn "$FQBN" --upload -p /dev/ttyACM0 .
```

`huge_app` is not optional: the default 4 MB scheme reserves two OTA slots
this project never uses and leaves only 1.2 MB for the app, which the
WiFi + JSON build already fills to 88%.

Requires `esp32:esp32` core 3.3.11 and libraries **M5Unified** and
**ArduinoJson**.

## Configuration

Normally you never touch this: an unconfigured device starts an access
point, `M5INK_MINIMED_MON` (password `123456789`), and serves a setup form at
`http://192.168.4.1` — the same fields and field names as the MicroPython
build, so the two are interchangeable to anyone who has set one up before.

One improvement over that build: `WebServer::arg()` URL-decodes, where the
Python splits the raw query string, so a WiFi password containing `%`, `&` or
a space works here and silently does not there.

To seed settings over the cable instead, they live in NVS under the namespace
`minimed`, read via `Preferences`.
Credentials are deliberately **not** in this repo. Until the AP portal
arrives in phase 5, seed them by generating an NVS image and flashing it to
the `nvs` partition at `0x9000` (`0x5000` long in every 4 MB scheme):

```bash
cat > /tmp/nvs.csv <<'EOF'
key,type,encoding,value
minimed,namespace,,
wifissid,data,string,YOUR_SSID
wifipass,data,string,YOUR_PASSWORD
proxyaddr,data,string,192.168.1.5
ntpserver,data,string,pool.ntp.org
patient,data,string,Marie
proxyport,data,u16,8081
timezone,data,i32,1
EOF
python nvs_partition_gen.py generate /tmp/nvs.csv /tmp/nvs.bin 0x5000
esptool --chip esp32 --port /dev/ttyACM0 write-flash 0x9000 /tmp/nvs.bin
```

`patient` is optional — the proxy reports `firstName`, so a device never told
a name still shows the right one; the setting only overrides it.

## Font metrics differ from the MicroPython build

M5GFX reports different metrics to C++ than through its MicroPython binding,
so **layout constants cannot be copied between the two builds**:

| font | Python's comments | C++ measured |
|---|---|---|
| DejaVu72 | 50 px | 75 px |
| DejaVu40 x2 | 86 px tall, 140 wide | 84 tall, 156 wide |
| DejaVu24 | 26 px | 25 px |
| DejaVu18 | 20 px | 18 px |
| DejaVu12 | 16 px | 13 px |

The Python file chose DejaVu40-at-2x for the large reading precisely because
its DejaVu72 measured only 50 px. Here DejaVu72 is taller *and* 15 px
narrower, so the port uses it instead: with DejaVu40-at-2x the trend arrows
shrink to 10 px against the 17 px DejaVu72 leaves them, and the arrows are
what turn a number into a direction. Both builds compute their geometry from
`fontHeight()`/`textWidth()` at runtime, so each is internally consistent -
they simply do not produce identical panels.

## Fault tables are generated, not transcribed

`faults.cpp` is generated from `main_m5coreink.py`'s `faultIdMapping` (137
entries) and `faultIdTable` (63), plus the low/high glucose id sets, by a
script in the phase 4 commit. Hand-transcribing 200 lines of
reverse-engineered pump data is exactly the kind of job that produces one
silently wrong entry, and the two builds must not drift. Regenerate rather
than edit.

Verified on device against the Python's own answers: 002, 816, 802, 011 and
an unknown 999 all resolve identically, and the glucose-recovered checks
agree.

## The panel needs a clear on every wake

`M5.begin()`'s `clear_display` gates exactly one thing — `Display.clear()` —
and the port needs it **true**, where `main_m5coreink.py` sets it false.

Under UIFlow the firmware ran its own `M5.begin()` with default config on
every boot, so the panel was cleared each wake regardless; the app's `false`
only suppressed a second, redundant clear. Here the sketch's `M5.begin()` is
the only one, so `false` means the panel is never cleared and residue
accumulates until a changed screen cannot be read.

Waveform choice is not a substitute. Both the quality two-pass on every draw
and a black/white conditioning flush on cold boot were tried on hardware and
neither helped; only the per-wake clear did. It costs ~1.9 s (2.35 s -> 4.28 s
per cycle), which is worth paying and still under MicroPython's 5.26 s.

This is the sharpest example so far of why the two builds cannot share
reasoning: `clear_display: False` and `FULL_REFRESH_EVERY = 12` are both
correct in the Python file and both wrong here, for a reason that lives in
the firmware rather than in either file.

## Measured on this board

Against the MicroPython build running the same cycle on the same hardware:

| | MicroPython | Arduino |
|---|---|---|
| WiFi association | 2081 ms | 1178 ms |
| fetch + parse | 79 ms | 102 ms |
| awake per cycle | 5260 ms | **4284 ms** |
| awake per day | ~25 min | **~20 min** |

(2.0-2.3 s of that before the per-wake panel clear became necessary.)
| free RAM | ~55 KB largest block | 275 KB |
| image size | 3.4 MB | 1.16 MB |

### Power hold, from reset

The rail is latched by GPIO12 and coasts only briefly with it floating, so
how fast firmware takes the latch decides whether a crash on battery recovers
or switches the board off for good.

| | assert |
|---|---|
| MicroPython (patched firmware) | 100 ms |
| Arduino, software reset — the crash case | 147 ms |
| Arduino, cold boot | 238 ms |

Arduino is slower because the stock bootloader validates the image, which our
patched UIFlow firmware skips. **Verified on battery** with a restart loop
counting in `RTC_NOINIT_ATTR` memory: the count keeps climbing, so 147 ms is
inside the coast and the self-recovery property survives the port.

Note `RTC_DATA_ATTR` is **not** suitable for anything that must outlive a
crash — it lives in `.rtc.bss`, which ESP-IDF zeroes on every boot that is
not a deep-sleep wake. That is the right behaviour for the state snapshot
(which must be discarded on a cold boot, as MicroPython's RTC memory is) and
the wrong one for a reboot counter.
