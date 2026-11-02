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

## Download

Built binaries are on the [Releases](https://github.com/connexionlimodubai-pixel/capturekvm/releases)
page, not in the file tree — build output does not belong in a source repository.
`.github/workflows/release.yml` builds and attaches them when a `v*` tag is pushed.

| | |
|---|---|
| **Android** | `CaptureKVM-<tag>-android.apk` — sideload it. Debug-signed, so Android warns about an unknown source. |
| **Windows** | `CaptureKVM-<tag>-windows-x64.zip` — unzip, run `CaptureKVM.exe`. Unsigned, so SmartScreen warns: *More info* then *Run anyway*. About 77 MB zipped. |

Neither is any use on its own: both talk to `beamer-vigem/` running on the PC you want to watch.

To build them yourself:

```bash
cd android && ./gradlew :app:assembleDebug
```

```bash
cd windows && .venv\Scripts\pyinstaller.exe capturekvm.spec --noconfirm
```

## What you need

| | |
|---|---|
| **A UVC capture card** | Any HDMI-to-USB grabber. The ~$5 MS2109 class does 1080p30 (MJPEG); an MS2130 does 1080p60 over USB 3.0. Both report USB class 14, which is all this app looks for. |
| **On the PC** | [`beamer-vigem/`](beamer-vigem) in this repo — Beamer with the gamepad capability this project needs. Install that rather than stock Beamer; [`FORK.md`](beamer-vigem/FORK.md) is the three-step setup. Stock Beamer also works, without the pad. |
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
| **Ctrl+Alt+Shift+Q** | let go — the one chord never sent to the PC, and never swallowed by the hook |
| **Send key** | chords Windows will not deliver to a window: Win+D, Win+R, Alt+F4, Task Manager… |
| **F11** | fullscreen |
| clicking the picture | starts driving |

It also lets go when the window loses focus, and every key and button still held is released on
the way out, so nothing can be left stuck down on the PC.

### Sound

The card carries the PC's HDMI audio as a USB audio device, separate from its video. Press
**Sound** and it plays here. Like the picture, **it never touches the network** — it arrives on the
same cable, so the two stay in step and the link still carries only key presses.

One callback copies input frames to output frames. Nothing is resampled, re-mixed or queued: if the
card and your output disagree about sample rate, the stream refuses to open and says so, because
silently absorbing a mismatch is how audio ends up a quarter-second behind the video. The status bar
shows the rate, the round-trip latency PortAudio reports, and a drop count if any blocks are lost.

Windows' own **Sound → Recording → [your card] → Listen to this device** does the same job in the
kernel and will always beat this by a few milliseconds. The in-app route exists so it works without
going to find that checkbox, and so the app can tell you when it isn't working. On Android the
device list is shown when no USB audio input can be found, which is the usual symptom of a card
whose audio the tablet hasn't claimed.

### Picture quality

Cheap HDMI grabbers hurt the picture in a few separate ways, and they need different answers.
**Picture…** on the toolbar has the controls; everything is off by default and costs nothing when
off.

| Symptom | Cause | Fix |
|---|---|---|
| Black looks grey, or shadows are crushed | HDMI carries limited range (16–235); the card labels it full range (0–255) | **Measure**, then *Expand 16–235 to 0–255* |
| Soft, blocky, fuzzy text | USB 2.0 cards fit 1080p30 down 480 Mbit/s with heavily compressed MJPEG | **Sharpen** — detail is gone for good, but what's left reads better |
| Dull colour | compression hits chroma hardest | **Saturation** |
| Jagged, aliased edges | *was a bug in this app* — fixed | nothing to do |

**Measure** reads the frame on screen rather than guessing, and will say *"too little contrast to
judge"* on a dark menu instead of inventing an answer. That matters: expanding the range of a
picture that was never limited crushes it. Show something with real blacks and whites first.

Measured on this machine at 1080p: levels **3.9 ms**, saturation **7.5 ms**, sharpen **6.9 ms**,
all three **16.4 ms** against a 33 ms budget at 30 fps. The dialog shows the live cost and warns
when you go over it. Levels alone — the fix most people need — is nearly free.

Two view options sit there too: **smooth scaling** (on; turn it off only if you want a deliberate
pixel-art look) and **1:1**, which resamples nothing at all and is as sharp as the card can be,
at the cost of clipping when the window is smaller than the frame.

### Latency

The capture thread drains whatever Media Foundation has queued and shows only the newest frame.
`CAP_PROP_BUFFERSIZE = 1` is a request the back end may ignore, so the depth is measured rather
than trusted: `grab()` returns instantly when a frame is already waiting and blocks for about a
frame interval once caught up, and that difference is what the loop reads.

The status bar shows **device to painted** in milliseconds, plus how many queued frames were
skipped. That is the part this app controls — it is not glass-to-glass, because the card's own
delay before a frame reaches USB cannot be seen from software and is usually the larger half.

### Clipboard

Copy on either machine and it crosses when input does. This is Beamer's own capability, not an
addition — the PC side already handled it; only the client ends were missing.

Windows does text **and images**: a copied screenshot pastes into the PC as a PNG, and back. Android
does text only, and therefore advertises `clipboard` but not `clipboard_image`, so the PC never
sends it a picture it has nowhere to put. That is the capability system doing its job rather than a
caveat.

Per the wire, the clipboard is sent **after the take and before any input**, so a Control-V on the
PC finds what you just copied rather than what was there before.

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
**[`beamer-vigem/`](beamer-vigem)** is a fork of Beamer, carried in this repo as a git subtree, that implements them against
ViGEmBus. Install that on the PC instead of stock Beamer — see
[`beamer-vigem/docs/GAMEPAD.md`](beamer-vigem/docs/GAMEPAD.md).

The two halves are checked against each other over a real socket, so the contract is not a matter
of hope:

```bash
python tools/end_to_end_check.py   # 18 checks: client <-> the PC's real responder
```

The client advertises `gamepad` in its `caps` and **sends no pad frame unless the PC advertises it
too**, so pointing it at a stock Beamer degrades to keyboard and mouse instead of breaking.
[docs/PAD-PROTOCOL.md](docs/PAD-PROTOCOL.md) states the contract from the client's side.

## How the link is built

The keyboard and mouse path is not a reimplementation. `beamer-vigem/core/` is a verbatim
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
python tools/end_to_end_check.py
```

14 checks: the handshake, that `hello` declares port 0, that the focus takes the machine, that
keys, mouse moves, scroll and pad frames all arrive, that sequence numbers start at 1 and only
rise, and that nothing is left unacknowledged. This is what caught the one ordering bug worth
catching — the reader thread has to be running *before* the focus goes out, or the accept that
answers it has nobody to read it and the take times out.

## Known limits

- **Ctrl+Alt+Delete cannot be sent by anything** — it is the secure attention sequence, invisible
  to hooks and unreachable by injection, on this side and the PC's. The Windows key, Alt+Tab and
  Ctrl+Esc *are* forwarded now, through a `WH_KEYBOARD_LL` hook that sits below the shell; they
  are swallowed locally while driving, so pressing Win opens the PC's Start menu, not this one's.
- **Left/right Control and Alt** are not always distinguishable on Windows; the client falls back
  to the left-hand name.
- The client **only initiates**. It declares its link port as 0, which is how Beamer marks a
  machine that is never dialled, so the PC's own keyboard is never sent here.
- **Clipboard images are Windows-only.** Android's clipboard carries pictures as content URIs and
  converting one to a PNG is work this app doesn't do, so it advertises text only.
- **Sound has never met a real capture card here.** The passthrough opens, picks devices and
  reports failures correctly against this machine's inputs, but no card was attached to test the
  one path that matters.
- **One pad**, x360 only. No rumble (that would need a responder-to-initiator message). The PC side
  declines `ds4`, which is in the vocabulary but unimplemented.
- **The pad path has never met a real ViGEmBus driver.** Both halves were written and tested
  against each other here, but this machine has no driver installed, so
  `vigem_target_x360_update` has not actually been called. Everything up to that one function is
  covered.
- The Android client was **built and unit-tested here but not run on a device** — no tablet and no
  capture card were attached while it was written.

## Layout

One repository, two halves: the client you install on the laptop or tablet, and `beamer-vigem/` —
the Beamer fork you install on the PC, carried as a **git subtree** so it keeps its own history.

That means the protocol exists once. The client imports `beamer-vigem/core/` directly rather than a
copy of it, so the two ends cannot drift apart; a wrong byte in a key derivation does not raise an
error, it just stops the link working and tells you nothing.

The subtree is not a snapshot — it merges both ways:

```bash
# take Beamer's upstream updates into beamer-vigem/
git subtree pull --prefix=beamer-vigem beamer-upstream main
```

```bash
# publish beamer-vigem/ back to the fork repo (only needed to send a PR upstream)
git subtree push --prefix=beamer-vigem fork vigem-gamepad
```



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
├── beamer-vigem/            the PC's half, as a git subtree (its own history)
├── tools/make_vectors.py    generates the Kotlin test vectors from that core
├── tools/end_to_end_check.py  the client's link against the PC's real responder
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
