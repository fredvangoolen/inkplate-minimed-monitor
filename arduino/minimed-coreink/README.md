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
| 2 | main screen | next |
| 3 | deep sleep, RTC state, toggle | |
| 4 | remaining screens, fault tables, alarms | |
| 5 | AP config portal | |
| 6 | soak and cutover | |

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

Settings live in NVS under the namespace `minimed`, read via `Preferences`.
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

## Measured on this board

Against the MicroPython build running the same cycle on the same hardware:

| | MicroPython | Arduino |
|---|---|---|
| WiFi association | 2081 ms | 1178 ms |
| fetch + parse | 79 ms | 102 ms |
| awake per cycle | 5260 ms | **2026 ms** |
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
