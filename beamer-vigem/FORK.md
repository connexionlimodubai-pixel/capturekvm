# This fork: Beamer with a gamepad

A fork of [Beamer](https://github.com/kalkman-code/beamer) 1.5.18 that adds **one capability** —
`gamepad` — so a peer can drive a virtual Xbox 360 controller on this PC through
[ViGEmBus](https://github.com/nefarius/ViGEmBus), beside the keyboard and mouse Beamer already
carries.

It exists so [CaptureKVM](../capturekvm) can play games on this PC: the picture comes off a USB
capture card on the tablet or laptop, and the keyboard, mouse *and pad* come back over Beamer's
link.

Everything upstream does, it still does. Branch: `vigem-gamepad`.

## Install it on the PC

**1. The ViGEmBus driver** (once, needs a reboot on some builds):

Download and run the installer from [ViGEmBus releases](https://github.com/nefarius/ViGEmBus/releases).

**2. ViGEmClient.dll**, the user-mode library. Either put it next to `win_app/`, or point at it:

```powershell
$env:BEAMER_VIGEM_DLL = "C:\path\to\ViGEmClient.dll"
```

The easiest source is the `vgamepad` package, which ships the same library and is looked for
automatically:

```powershell
pip install vgamepad
```

**3. Run Beamer from this fork:**

```powershell
pip install -r win_app\requirements-win.txt
python win_app\kvm_bridge_win.py
```

Or build the installer the way upstream does — `win_app\build_win_app.ps1` is unchanged.

## Check it took

With the driver reachable, Beamer offers `gamepad` in its `caps`. If it does not, the log says
why in a line beginning `The gamepad capability is not offered:` — usually `bus not found`, which
means step 1 did not take, or `ViGEmClient.dll was not found`, which means step 2 did not.

A peer that connects while the capability is absent simply gets keyboard and mouse, exactly as
with stock Beamer.

## Pair the client with it

Nothing new: open Beamer, choose Pair, and type the six digits into CaptureKVM. The pad needs no
separate pairing — it rides the link the keyboard already uses, under the same ownership rules, so
whoever is driving the PC is whoever drives the pad.

## What changed

| File | |
|---|---|
| `core/gamepad.py` | **new.** The three messages, their builders and their validators. No platform code, like the rest of `core`. |
| `core/receiver.py` | `_on_pad` and three `_pad_*_locked` helpers; `_Link.pads`; the pad types routed in `_dispatch`; `_release_locked` unplugs the pads. |
| `win_app/vigem.py` | **new.** ViGEmClient.dll through ctypes: find it, connect, add and remove targets, push `XUSB_REPORT`s. |
| `win_app/input_injector.py` | five `gamepad_*` functions delegating to it, reached by `getattr` so other platforms are untouched. |
| `win_app/kvm_bridge_win.py` | `gamepad` added to `caps`, offered only when a pad can really be plugged in; the bus closed on quit. |
| `core/tests/responder_harness.py` | the fake injector learned the pad, so tests need no driver. |
| `core/tests/test_gamepad.py` | **new.** 24 tests: the builders and validators. |
| `core/tests/test_responder_gamepad.py` | **new.** 10 tests: a real responder on loopback. |
| `docs/GAMEPAD.md` | **new.** The specification. |

Nothing in `mac_app/`, the Linux back ends, the crossing, the effects or the pairing was touched.
macOS and Linux never offer the capability, because they cannot plug a pad in.

## Test it

```bash
python -m unittest core.tests.test_gamepad core.tests.test_responder_gamepad
```

34 tests, no driver needed. The ones worth reading are in `test_responder_gamepad.py`: a non-owner
drives nothing, a peer offered no capability drives nothing, a report is acknowledged even when it
is dropped, and letting go, being taken over and losing the link each unplug every pad.

And against the client it was written for, over a real socket:

```bash
cd ../capturekvm && python tools/interop_check.py
```

## Licence

GPL-3.0-only, upstream's. Commits are signed off under the
[Developer Certificate of Origin](https://developercertificate.org), as `CONTRIBUTING.md` asks.
