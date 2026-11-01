# The gamepad messages

Upstream Beamer's wire version 6 has no gamepad. This file is the contract CaptureKVM speaks.

**The matching PC side is `../../beamer-vigem/`**, a fork of Beamer in this workspace that
implements it against ViGEmBus. Its own specification is
[`beamer-vigem/docs/GAMEPAD.md`](../../beamer-vigem/docs/GAMEPAD.md), and the two are checked
against each other over a real socket:

```bash
python tools/interop_check.py
```

So this is no longer a proposal — it is what both ends do. If you ever point the client at a
*different* fork that names things differently, the pad lives in two small files and nothing else
knows about it:

- `windows/capturekvm/padwire.py`
- `android/app/src/main/java/com/capturekvm/wire/PadWire.kt`

## Why these fields

The fields are `XUSB_REPORT`'s, the struct ViGEmBus takes for an Xbox 360 target. A report can go
straight into `vigem_target_x360_update` with no rescaling, no sign flips and no deadzone work on
the responder's side. The client does that work, because it is the side that knows what kind of pad
is plugged into it:

- **Windows** reads `XINPUT_GAMEPAD` through `XInputGetStateEx`, whose fields already *are* these.
- **Android** reads `MotionEvent` axes as floats in −1..1, negates the vertical ones (Android's Y
  points down, XUSB's points up), removes the device's own flat zone, and scales to these ranges.

## Capability

Both sides list `gamepad` in `caps` in `hello` / `welcome`.

The client sends no pad frame unless the peer's `welcome` advertised it. This matters more than it
looks: upstream's `parse_message` ignores an unknown type, so a stock Beamer would drop a
`gamepad` frame silently — and never acknowledge its `seq`, which the initiator reads as a dead
link after 1.5 seconds. Gating on the capability is what keeps the client safe against an
unmodified Beamer rather than merely useless.

## Messages, initiator to responder

### `gamepad_attach`

Plug a virtual pad in.

```json
{"type": "gamepad_attach", "data": {"pad": 0, "kind": "x360"}}
```

| Field | Type | Meaning |
|---|---|---|
| `pad` | int, 0–3 | which virtual pad |
| `kind` | `"x360"` or `"ds4"` | which target to create |

Sent when a local pad appears, and **again on every reconnect** — the responder's targets should
die with its link, so the client cannot assume one survived.

### `gamepad_detach`

```json
{"type": "gamepad_detach", "data": {"pad": 0}}
```

Sent when the local pad goes away or the user disconnects.

### `gamepad`

One full report. Carries `seq` and is acknowledged like any other input.

```json
{"type": "gamepad",
 "data": {"pad": 0, "buttons": 4096, "lt": 255, "rt": 0,
          "lx": -32768, "ly": 0, "rx": 0, "ry": 0, "seq": 7}}
```

| Field | Type | Range |
|---|---|---|
| `pad` | int | 0–3 |
| `buttons` | int | 0–65535, the bitmask below |
| `lt`, `rt` | int | 0–255 |
| `lx`, `ly`, `rx`, `ry` | int | −32768–32767, **positive Y is up** |
| `seq` | int | as for every input message |

Full state, not a delta. A dropped frame then costs one poll's worth of lag instead of leaving a
button held down on the PC — which is the failure that matters, because nothing on the PC side can
tell a stuck button from a deliberate one.

The client sends a frame when the reading changes (Windows watches XInput's `dwPacketNumber`) and a
keepalive every 0.5 s while the pad is not neutral. A neutral pad sends nothing.

### Button bits

Straight from `xinput.h` / `XUSB_BUTTON`, so the two ends cannot drift:

| Bit | Button | Bit | Button |
|---|---|---|---|
| `0x0001` | D-pad up | `0x0200` | right shoulder |
| `0x0002` | D-pad down | `0x0400` | Guide |
| `0x0004` | D-pad left | `0x1000` | A |
| `0x0008` | D-pad right | `0x2000` | B |
| `0x0010` | Start | `0x4000` | X |
| `0x0020` | Back | `0x8000` | Y |
| `0x0040` | left thumb | | |
| `0x0080` | right thumb | | |
| `0x0100` | left shoulder | | |

## What the responder must do

All five are implemented in `beamer-vigem` (`core/receiver.py`, `_on_pad` and the `_pad_*_locked`
helpers) and covered by `core/tests/test_responder_gamepad.py`.

1. Advertise `gamepad` in `caps`.
2. On `gamepad_attach`, create the ViGEm target; answer the frame's `seq` with an `ack` as for any
   input. On `gamepad_detach`, free it.
3. On `gamepad`, copy the fields into `XUSB_REPORT` and call `vigem_target_x360_update`.
4. **Free every target when the link drops, and when input is let go or taken by another machine.**
   Otherwise a link lost mid-trigger leaves the PC holding it. Upstream already releases held keys
   and buttons on a let-go; the pad belongs in the same place.
5. Apply the same ownership rule as keyboard and mouse: a `gamepad` frame from a machine that does
   not own the responder's input should be dropped, not injected.

`read_report` in `padwire.py` is the validator for step 3 and is written to be usable as-is on the
responder's side.

## Not covered

- **Rumble and LED**, which would travel responder-to-initiator. Nothing here sends them, and
  Android's `Vibrator` on the pad would be the obvious destination.
- **DS4 reports.** `kind: "ds4"` is in the vocabulary, but both clients only ever build x360
  reports; the touchpad and gyro fields a DS4 target accepts have no source here.
- **More than one pad.** `pad` is 0–3 and both clients use 0.
