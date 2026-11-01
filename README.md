# CaptureKVM

Watch your PC through a USB capture card and drive it from the device the card is plugged into —
keyboard, mouse and **gamepad** — over [Beamer](https://github.com/kalkman-code/beamer)'s link.

```
                      HDMI cable                   USB-C / USB-A
  ┌───────────┐                   ┌─────────────┐                 ┌──────────────────┐
  │    PC     │ ── HDMI out ────▶ │ USB capture │ ──── UVC ─────▶ │ Windows laptop   │
  │           │                   │    card     │                 │   or Android     │
  │  Beamer   │                   └─────────────┘                 │    tablet        │
  │ (responder│                                                   │   (CaptureKVM)   │
  │ + ViGEmBus)│◀───── Wi-Fi / LAN, Beamer wire v6, TCP 24820 ────│                  │
  └───────────┘        keyboard · mouse · pad reports only        └──────────────────┘
```

**The video never touches the network.** It arrives down a cable as UVC frames, so the picture's
lag is the card's (roughly 60–100 ms for a cheap MS2109 grabber) and no encoder, no bitrate and no
Wi-Fi hiccup can touch it. Only input goes over the air, which is a few dozen bytes a frame.

That split is the whole point: streaming video over Wi-Fi *and* sending input back over the same
Wi-Fi gives you two sources of lag that interact. Here they are independent, and the one you feel
most — the picture — is wired.

## What you need

| | |
|---|---|
| **A UVC capture card** | Any HDMI-to-USB grabber. The ~$5 MS2109 class does 1080p30 (MJPEG); an MS2130 does 1080p60 over USB 3.0. Both report USB class 14, which is all this app looks for. |
| **On the PC** | [`../beamer-vigem/`](../beamer-vigem) — Beamer with the gamepad capability this project needs. Install that rather than stock Beamer; [`FORK.md`](../beamer-vigem/FORK.md) is the three-step setup. Stock Beamer also works, without the pad. |
| **A second HDMI output** on the PC (or a splitter) if you want to keep using its own monitor. |
| **Windows client** | Python 3.11+ |
| **Android client** | Android 8.0+ with USB host (OTG). Most tablets; check yours supports USB host. |

Both machines must be on the same network and able to reach TCP 24820 and 24821 on the PC.

## Windows client

```bash
cd windows
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
.venv\Scripts\python.exe -m capturekvm
```

Or just `windows\run.ps1`.

1. **Card** — pick the capture device. The list is probed, so each entry shows the format that
   index actually opened with. Press *Rescan cards* after plugging one in.
2. **Format** — `1920x1080 30` with MJPG is the right default. The pixel format matters more than
   the size: a USB 2.0 grabber offers uncompressed YUY2 only at small sizes and single-digit frame
   rates, so MJPG is what makes 1080p possible at all.
3. **Start card** — the picture appears.
4. **Pair…** — type the PC's address and the six digits Beamer is showing. One minute, one use.
5. **Connect**, then **Drive (F9)**.

### Driving

While driving, the window takes the keyboard and pins the cursor to its centre so mouse movement
can be sent as relative motion.

| | |
|---|---|
| **F9** | start / stop driving |
| **Ctrl+Alt+Shift+Q** | let go — the one chord that is never sent to the PC |
| **F11** | fullscreen |
| clicking the picture | starts driving |

It also lets go when the window loses focus, and every key and button still held is released on
the way out, so nothing can be left stuck down on the PC.

### Audio

Deliberately not handled. The card's audio is a separate USB audio device, and Windows's own
**Sound → Recording → [your card] → Listen to this device** routes it to your speakers with less
delay than this process could add. On Android, plug headphones into the card if it has a jack.

## Android client

```bash
cd android
./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

A prebuilt debug APK is at `android/app/build/outputs/apk/debug/app-debug.apk`.

Plug the card into the tablet (a powered OTG hub helps — grabbers draw real current), grant the USB
permission, then **Pair** and **Connect** as above.

| Control | |
|---|---|
| **Touchpad** mode | the picture is a trackpad: drag to move, tap to click, two fingers to scroll, long-press for right-click |
| **Gamepad** mode | touch is ignored so a stray thumb cannot move the pointer mid-game; pad reports go to the PC |
| **Keyboard** | opens the soft keyboard; a Bluetooth or USB keyboard works too and sends real key names |

It is a trackpad, not a touchscreen, because the wire has no absolute-pointer message and the
tablet has no idea where the PC's cursor currently is — so tapping where you want it cannot work.

## Gamepad

A pad plugged into the client appears on the PC as a virtual Xbox 360 controller.

- **Windows** reads it through `XInputGetStateEx` (so the Guide button works).
- **Android** reads it through `MotionEvent` axes and `KeyEvent` buttons, removes the stick's own
  flat zone, and converts to XUSB units.
- Either way the client sends `XUSB_REPORT`-shaped frames the PC can hand straight to
  `vigem_target_x360_update`.

The pad messages are not part of upstream Beamer, so the PC side comes with this project:
**[`../beamer-vigem/`](../beamer-vigem)** is a fork of Beamer that implements them against
ViGEmBus. Install that on the PC instead of stock Beamer — see
[`beamer-vigem/docs/GAMEPAD.md`](../beamer-vigem/docs/GAMEPAD.md).

The two halves are checked against each other over a real socket, so the contract is not a matter
of hope:

```bash
python tools/interop_check.py      # 9 checks: client <-> fork
```

The client advertises `gamepad` in its `caps` and **sends no pad frame unless the PC advertises it
too**, so pointing it at a stock Beamer degrades to keyboard and mouse instead of breaking.
[docs/PAD-PROTOCOL.md](docs/PAD-PROTOCOL.md) states the contract from the client's side.

## How the link is built

The keyboard and mouse path is not a reimplementation. `third_party/beamer/core/` is a verbatim
copy of upstream's `protocol.py`, `pairing.py` and `keytable.py`, and the Windows client imports
it, so the handshake, the ChaCha20-Poly1305 framing, the CPace pairing and every validation rule
are upstream's own code. **This makes the project GPL-3.0** — see [NOTICE.md](NOTICE.md).

The Android client cannot import Python, so its `wire/` package is a transcription of the same
modules. To stop that being a matter of hope, `tools/make_vectors.py` generates test vectors from
the vendored Python and `WireVectorsTest` asserts the Kotlin against them:

```bash
python tools/make_vectors.py
cd android && ./gradlew :app:testDebugUnitTest
```

13 tests, covering the key id, both direction keys, the sealed frames byte for byte, replay
rejection, canonical base64, Elligator 2, and the whole CPace exchange down to the agreed token and
all three tags. Re-run both after touching the vendored core.

The Windows link is checked end to end against a responder built from the same vendored core:

```bash
python tools/loopback_check.py
```

14 checks: the handshake, that `hello` declares port 0, that the focus takes the machine, that
keys, mouse moves, scroll and pad frames all arrive, that sequence numbers start at 1 and only
rise, and that nothing is left unacknowledged. This is what caught the one ordering bug worth
catching — the reader thread has to be running *before* the focus goes out, or the accept that
answers it has nobody to read it and the take times out.

## Known limits

- **Windows reserves some keys.** The Windows key, Alt+Tab, Ctrl+Alt+Delete and the secure desktop
  never reach the app, so they cannot be sent. This is the same limit Beamer itself documents.
- **Left/right Control and Alt** are not always distinguishable on Windows; the client falls back
  to the left-hand name.
- The client **only initiates**. It declares its link port as 0, which is how Beamer marks a
  machine that is never dialled, so the PC's own keyboard is never sent here.
- **No clipboard** yet. The wire supports it and the vendored core can build the messages; nothing
  is wired up.
- **One pad**, x360 only. No rumble (that would need a responder-to-initiator message). The PC side
  declines `ds4`, which is in the vocabulary but unimplemented.
- **The pad path has never met a real ViGEmBus driver.** Both halves were written and tested
  against each other here, but this machine has no driver installed, so
  `vigem_target_x360_update` has not actually been called. Everything up to that one function is
  covered.
- The Android client was **built and unit-tested here but not run on a device** — no tablet and no
  capture card were attached while it was written.

## Layout

Two projects, side by side: this client, and the PC-side fork it talks to.

```
beamer-vigem/                Beamer + the gamepad capability (install this on the PC)
├── FORK.md                  what changed, and how to install it
├── docs/GAMEPAD.md          the specification
├── core/gamepad.py          the three messages
├── win_app/vigem.py         ViGEmClient.dll through ctypes
└── core/tests/test_gamepad.py, test_responder_gamepad.py

capturekvm/
├── LICENSE                  GPL-3.0 (upstream Beamer's)
├── NOTICE.md                what is vendored, and why the licence is what it is
├── docs/PAD-PROTOCOL.md     the gamepad contract from the client's side
├── third_party/beamer/      verbatim upstream core + the commit it came from
├── tools/make_vectors.py    generates the Kotlin test vectors from that core
├── tools/loopback_check.py  the client's link against a responder built from that core
├── tools/interop_check.py   the client against the fork, over a real socket
├── windows/capturekvm/
│   ├── capture.py           UVC capture through Media Foundation
│   ├── link.py              the Beamer initiator
│   ├── pair.py              the "pair by address" exchange
│   ├── inputmap.py          Qt events to wire key names
│   ├── gamepad.py           XInput
│   ├── padwire.py           ← the pad messages
│   └── app.py               the window
└── android/app/src/main/java/com/capturekvm/
    ├── wire/{Crypto,Wire,Pairing,BeamerLink,KeyNames,PadWire}.kt
    ├── MainActivity.kt      UVC preview + input
    ├── PadReader.kt         Android pad to XUSB units
    └── ui/TouchpadView.kt   the picture as a trackpad
```
