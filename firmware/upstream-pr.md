# Upstream PR — prepared, not submitted

Status as of 2026-08-31: **parked deliberately**, to be picked up later. Do
not open it without asking.

Everything below is ready to go:

* Fork branch, already pushed:
  <https://github.com/fredvangoolen/uiflow-micropython/tree/coreink-power-hold>
* Two commits, +58 lines, no deletions, all under
  `m5stack/boards/M5STACK_CoreInk/`. Based on upstream `master` at `587e134`
  (V2.5.2). Upstream's Core Ink board files were byte-identical to the ones
  this patch was built against, and `board_init.c` does not exist there.
* Target: `m5stack/uiflow-micropython:master` (note the repo was renamed —
  hyphen, not underscore).
* Differences from `coreink-power-hold.patch` in this directory: `printf`
  becomes `ESP_LOGI`, and the comments carry no reference to this project.
  The code is otherwise identical to what runs on the device.

To submit: `gh pr create --repo m5stack/uiflow-micropython --base master
--head fredvangoolen:coreink-power-hold --title "CoreInk: assert the
power-hold latch (GPIO12) at startup" --body-file firmware/upstream-pr.md`
(strip this status section from the body first).

---

## The problem

CoreInk latches its own 3V3 rail on with GPIO12. Out of reset that pad is an
input, and nothing in this firmware drives it until M5GFX's board autodetect
does so as a side effect of `M5.begin()`. Measured on hardware, that lands
**between 1.6 s and 3 s after reset**.

While running on USB this is invisible, because USB feeds the rail directly.
On battery, the rail during that window is held up by nothing but the user's
finger on the power button, with two consequences:

1. **Any reset switches the board off, permanently.** A crash, a watchdog, or
   `machine.reset()` leaves the board dead until USB is connected or the power
   button is pressed. For an unattended, battery-powered device this turns
   any transient fault into a device that silently stops until someone finds
   it. (The reset button on the back cannot recover it: it holds EN low for
   the whole press, and a chip in reset drives nothing. That case is not
   fixable in firmware and this PR does not claim to fix it.)
For reference, the factory Arduino firmware on a new device reaches its board
init ~400 ms after reset - still slow against the rail, but 5x sooner than
this one.

## The change

Two commits, both scoped to `boards/M5STACK_CoreInk/`:

1. **`board_init.c` + `MICROPY_BOARD_STARTUP`** — drive GPIO12 high as the
   first statement of `app_main`, before the MicroPython task is created.
   `Power_Class::begin()` already does this for the Timer Cam; the CoreInk
   branch of that switch sets ADC and wake pins but never touches
   `power_hold`.
2. **`CONFIG_BOOTLOADER_SKIP_VALIDATE_ON_POWER_ON=y`** — the hook alone only
   reaches 980 ms, because the bootloader SHA-256s the whole ~3.4 MB image
   before `app_main` is reached. Deep-sleep wakes already skip validation, so
   the normal wake path never paid this; only resets did. This board has a
   single `factory` partition and no OTA slot, so a failed validation was not
   recoverable in any case.

Measured on hardware, from reset to the latch being asserted:

| Firmware | Latch asserted |
|---|---|
| stock | 1.6-3 s |
| M5Stack factory Arduino build | ~400 ms |
| commit 1 alone | ~980 ms |
| **both commits** | **50 ms** |

**The second commit is what makes this work.** 980 ms is still far too long:
independent measurement brackets the rail's coast time between ~50 ms and
~100 ms. I have kept them separate so the bootloader change can be discussed
on its own, but merging only the first would not fix the behaviour.

## Verification

On a CoreInk running UIFlow2 MicroPython, on battery, with this patch applied:

- a test app that counts in RTC memory, redraws the panel and calls
  `machine.reset()` every ten seconds reboots indefinitely
- a brief press of the power button starts the board

I did not run the stock arm of that reset-loop experiment, so treat "switches
off after any reset" above as inference rather than measurement. What is
measured on stock is the 1.6-3 s assert; measured separately, and with the
50 ms build, is that a reset which leaves GPIO12 floating for the length of a
button press (100-300 ms) is already fatal. A window of seconds cannot
survive what a window of a few hundred milliseconds does not.

The `ESP_LOGI` in `board_init.c` reports the assert on the console at boot;
note it is invisible at this firmware's default log level of ERROR, so
reproducing the timings above needs the log level raised or the call swapped
for a `printf`.

Happy to adjust naming, comments, or the split if you would prefer this
shaped differently.
