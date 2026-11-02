# The gamepad, version 1

An extension to wire version 6. Upstream Beamer carries a keyboard, a mouse, gestures and a
clipboard; this fork adds a pad, so a machine with a controller plugged into it can play a game on
a Windows PC through a virtual controller.

It is an extension and not a change: a peer that does not know these messages ignores them, as
section 2 of WIRE.md requires of any unknown type, and a peer that does not offer the capability is
never sent one.

## The capability

`gamepad`, in `caps` in `hello` and `welcome`.

A responder offers it only where a virtual pad can actually be plugged in — Windows with the
ViGEmBus driver reachable. `win_app/kvm_bridge_win.py` asks the injector
(`gamepad_available()`) once, on the first handshake, and leaves the capability out when the answer
is no. A peer must not be told it can send reports and then have them dropped in silence.

An initiator **must not** send a pad message to a peer whose `welcome` left `gamepad` out. Besides
being the rule for every capability, the consequence here is specific: an unknown type is dropped
without an `ack`, and an initiator ends its link when its oldest unacknowledged message is 1.5
seconds old (section 9). Sending pad frames to a stock Beamer would look like a dead link rather
than like a missing feature.

## Why the fields are XUSB's

`XUSB_REPORT` is the struct ViGEmBus takes for an Xbox 360 target. The wire carries exactly those
fields, in exactly those ranges, so a report crosses from a peer to the driver without being
rescaled or reinterpreted on the way.

The conversion belongs to whichever side reads the local pad, because that is the side that knows
what it has: XInput on Windows already reports these values; an Android pad reports floats in
−1..1 with its own flat zone, and negates its vertical axes, since Android's Y points down and
XUSB's points up.

## Messages, initiator to responder

### `gamepad_attach`

```json
{"type": "gamepad_attach", "data": {"pad": 0, "kind": "x360"}}
```

| Field | Type | |
|---|---|---|
| `pad` | int, 0–3 | which virtual pad. Default 0. |
| `kind` | `"x360"` or `"ds4"` | which target to create. Default `"x360"`. |

Not sequenced: it is a control record, like `focus`, answered by its effect.

Send it when a local pad appears, and **again on every reconnect**. A responder's targets die with
its link, so nothing may be assumed to have survived one.

An unknown `kind` makes the message malformed, and is ignored rather than treated as `x360`. A
responder that cannot emulate the kind asked for leaves the pad unplugged; this fork emulates
`x360` only, and logs when `ds4` is asked for.

### `gamepad_detach`

```json
{"type": "gamepad_detach", "data": {"pad": 0}}
```

Send it when the local pad goes away. Not needed on a clean disconnect — the responder unplugs
everything when ownership ends — but harmless.

### `gamepad`

```json
{"type": "gamepad",
 "data": {"pad": 0, "buttons": 4096, "lt": 0, "rt": 255,
          "lx": -32768, "ly": 0, "rx": 0, "ry": 0, "seq": 7}}
```

| Field | Type | Range |
|---|---|---|
| `pad` | int | 0–3, default 0 |
| `buttons` | int | 0–65535, the mask below |
| `lt`, `rt` | int | 0–255 |
| `lx`, `ly`, `rx`, `ry` | int | −32768–32767, **positive Y is up** |
| `seq` | int | required; acknowledged as section 9 describes |

Sequenced and acknowledged exactly like keyboard and mouse input.

**A full report, not a delta.** A dropped frame then costs one poll's worth of lag instead of
leaving a button held down on the PC — and a stuck button is the failure that matters, because
nothing on the responder's side can tell one from a button deliberately held.

A missing field reads as neutral, so a peer that only moves a stick need not send eight zeroes. A
field that is **present but out of range, or not an integer**, makes the whole report malformed and
it is dropped: guessing at a stick position is worse than losing a frame. A builder clamps instead,
because by then the sender has already scaled a real reading and a rounding error at the end of a
stick's travel should not cost the frame.

Send a frame when the reading changes, and a keepalive every half second while the pad is not
neutral. A neutral pad should send nothing.

### Button bits

From `xinput.h` / `XUSB_BUTTON`, unchanged so the two ends cannot drift:

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

## What the responder does

`core/receiver.py`, `_on_pad` and the three `_pad_*_locked` helpers.

1. **Ownership.** Only the machine that owns this one may drive the pad, for attach, detach and
   reports alike. The owner check and the injection happen under the same lock that guards
   keyboard and mouse input, so an ownership ending on another thread cannot slip between them and
   leave a stick pushed over after the owner has gone.
2. **Acknowledgement.** A valid report is acknowledged whether or not it was injected — dropped
   because this machine is not being driven, dropped because the lock screen is clearing, dropped
   because the driver refused. Withholding the `ack` would read to the initiator as a dead link.
3. **Release.** `_release_locked` unplugs every pad, so a let-go, a take by another machine and a
   dropped link all leave the PC holding nothing. `quit()` closes the bus handle as a backstop, so
   no phantom controller is left in the device list.
4. **A report with no attach** plugs an `x360` in rather than dropping the frame: a reconnect can
   land a report first, and a pad already being held should work the moment it arrives.

## The driver

`win_app/vigem.py` talks to ViGEmClient.dll through ctypes — no third-party Python package is
required, though an installed `vgamepad` is one of the places the library is looked for.

ViGEmClient.dll is found, best first, from:

1. `BEAMER_VIGEM_DLL`, an absolute path to it;
2. beside `win_app/`, beside the repository root, or inside a PyInstaller bundle;
3. `vgamepad/win/vigem/client/x64/ViGEmClient.dll` in an installed `vgamepad`;
4. whatever Windows' own search path finds.

The **ViGEmBus driver** itself is a kernel driver and is installed separately — see
[ViGEmBus releases](https://github.com/nefarius/ViGEmBus/releases). Without it, `vigem_connect`
fails with `bus not found`, the capability is not offered, and the fork behaves exactly as upstream
does.

## Testing

```bash
python -m unittest core.tests.test_gamepad core.tests.test_responder_gamepad
```

`test_gamepad.py` covers the builders and validators. `test_responder_gamepad.py` drives a real
responder over loopback through `core/tests/responder_harness.py` and covers what actually matters:
that a non-owner drives nothing, that a peer offered no capability drives nothing, that a report is
acknowledged even when dropped, and that letting go, being taken over and losing the link each
unplug every pad. Neither needs the driver — the harness's `FakeInjector` stands in for it.

## Not in version 1

- **Rumble and LEDs**, which would need a responder-to-initiator message.
- **DS4 targets.** `ds4` is in the vocabulary so a later version can add it without a wire change;
  this fork declines it.
- **More than one pad per link** works by index, but nothing yet sends more than pad 0.
