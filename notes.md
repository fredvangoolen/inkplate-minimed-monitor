❯ what would be the pros and contras for porting the application to Arduino?

Worth answering with this project's actual numbers rather than in general terms, because they point somewhere slightly unexpected.

## Where your 5.3 s awake actually goes

```
0.00 – 2.70 s   ESP-IDF + MicroPython VM + M5.begin()   51%
2.70 – 4.86 s   WiFi association + NTP                  41%
4.86 – 5.31 s   fetch, parse, beep, draw, save           8%
```

An Arduino build removes almost all of the first block — M5's factory firmware reaches its board init in ~7 ms of app time against our ~2 s — and **none** of the second. WiFi association is radio-bound and identical in any language. So the realistic ceiling is 5.3 s → ~2.8 s per cycle, roughly 25 → 13 minutes awake per day.

## Pros

- **~2.5 s per cycle, permanently.** The single biggest win, and it's the one thing MicroPython can't give you: the VM startup is a fixed tax paid 283 times a day.
- **The memory fight disappears.** No GC heap competing with the WiFi stack over ~98 KB. That whole apparatus — shipping precompiled `.mpy`, `_largest_free_block()` guarding the canvas, an uncatchable C++ `abort()` if you get it wrong — exists only because of MicroPython's heap.
- **Full M5Unified surface.** We repeatedly hit missing bindings: `M5.Display.sleep()` isn't exposed, only `powerSave*`. In C++ you get the whole library, including partial-refresh modes we've never been able to try.
- **Doubles, not floats.** The timestamp bug that decoded 11:51:00 as 11:50:56 and reported "5 min ago" for a 4-minute-old reading was single-precision MicroPython floats. It bit both boards. That class of bug is gone.
- **The firmware patch becomes unnecessary.** M5's Arduino `M5.begin()` grabs the power latch in milliseconds; our `board_init.c` exists to fix a MicroPython-specific latency.

## Cons

- **You lose the iteration loop.** Today: edit, `mpy-cross`, `cp`, reset — seconds. Arduino: recompile the sketch and libraries, flash ~1–3 MB (our firmware flashes take ~30 s), reset. We did dozens of iterations in this session alone; most would have been minutes each.
- **It's a rewrite, not a port.** ~2,200 debugged lines: fault-code tables, DST handling, alarm logic, the AP config web server, the RTC snapshot round-trip. ArduinoJson needs document sizing for a payload this large; the fault tables become PROGMEM; the config server becomes WebServer.
- **A third thing to maintain.** You already carry `main.py` and `main_m5coreink.py` as deliberate duplicates. The Inkplate would stay MicroPython, so you'd have two languages across three files, and every fix would need translating.
- **This device is in daily use.** DST, timestamps and alarm handling are exactly the areas that have already produced silent, plausible-looking wrong answers. A rewrite puts all of them back on the table.

## The part that actually decides it

**We don't know whether awake time dominates your battery.** At a 1.7 % duty cycle, the awake burst and the deep-sleep floor are plausibly the same order of magnitude — and if sleep current dominates, halving awake time buys 10 %, not 45 %.

You have the instrument for this now. The runtime counter has been running since the 1st, and you have a natural before/after: awake time already dropped from 13.5 s to 5.3 s per cycle on board #2. Compare a charge cycle now against what you remember, and the answer falls out.

If it turns out awake time does dominate, two changes inside the existing code capture much of what a port would:

- **NTP every cycle is wasteful** — the RTC doesn't drift meaningfully in five minutes. Syncing hourly would save ~1 s on 11 of every 12 cycles.
- **Static IP instead of DHCP** saves perhaps another 0.5–1 s of association.

Together that's maybe 5.3 → 3.5 s, most of the Arduino gain, for an afternoon's work and no rewrite.

## My recommendation

Don't port now. Get one clean battery figure first, then take the two cheap wins if you want more. Reconsider Arduino only if you hit something MicroPython genuinely can't do — partial refresh for a smoother update, or a feature that needs the RAM.

Happy to do the NTP and static-IP changes as a measured experiment whenever you like; both are small and testable against the runtime counter.