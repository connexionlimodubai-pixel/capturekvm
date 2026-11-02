"""The gamepad messages: an extension to wire version 6, not part of it.

Upstream Beamer carries a keyboard, a mouse, gestures and a clipboard. This fork adds a pad, so a
machine with a controller plugged into it can drive a Windows PC's games through a virtual
controller (ViGEmBus). `docs/GAMEPAD.md` is the specification.

The fields are `XUSB_REPORT`'s, the struct ViGEmBus takes for an Xbox 360 target, so a report
needs no rescaling on its way in: whatever reads the local pad does the conversion, because it is
the side that knows what kind of pad it has.

Everything here follows the conventions of `core/protocol.py`: a builder per message, a validator
per message that returns the fields in Python types or None when the message is malformed, and a
closed vocabulary with bounds on every integer. Unknown fields are ignored, as section 2 requires,
so a later version may add some without breaking this one.

This module is deliberately free of platform code and of any import from the apps, like the rest
of `core`.
"""

CAP_GAMEPAD = "gamepad"

# Offered by an *initiator* that can make its own pad rumble. Separate from CAP_GAMEPAD because
# the two are independent: a tablet can send pad input with no motors to drive, and a responder
# must not send rumble to a peer that would only drop it.
CAP_RUMBLE = "gamepad_rumble"

MSG_GAMEPAD_ATTACH = "gamepad_attach"
MSG_GAMEPAD_DETACH = "gamepad_detach"
MSG_GAMEPAD = "gamepad"

# The one message in this extension that travels the other way: responder to initiator. The game
# runs on the PC, so the PC is the only side that knows when to rumble.
MSG_GAMEPAD_RUMBLE = "gamepad_rumble"

PAD_TYPES = frozenset({MSG_GAMEPAD_ATTACH, MSG_GAMEPAD_DETACH, MSG_GAMEPAD})

# Only `gamepad` carries a `seq` and is acknowledged; attach and detach are control records, like
# `focus`, and are answered by their effect rather than by a number.
SEQUENCED_TYPES = frozenset({MSG_GAMEPAD})

KIND_X360 = "x360"
KIND_DS4 = "ds4"
KINDS = (KIND_X360, KIND_DS4)

MAX_PADS = 4

# XINPUT_GAMEPAD / XUSB_BUTTON bits, unchanged from xinput.h so the two ends cannot drift.
BTN_DPAD_UP = 0x0001
BTN_DPAD_DOWN = 0x0002
BTN_DPAD_LEFT = 0x0004
BTN_DPAD_RIGHT = 0x0008
BTN_START = 0x0010
BTN_BACK = 0x0020
BTN_LEFT_THUMB = 0x0040
BTN_RIGHT_THUMB = 0x0080
BTN_LEFT_SHOULDER = 0x0100
BTN_RIGHT_SHOULDER = 0x0200
BTN_GUIDE = 0x0400
BTN_A = 0x1000
BTN_B = 0x2000
BTN_X = 0x4000
BTN_Y = 0x8000

BUTTON_NAMES = {
    "dpad_up": BTN_DPAD_UP, "dpad_down": BTN_DPAD_DOWN,
    "dpad_left": BTN_DPAD_LEFT, "dpad_right": BTN_DPAD_RIGHT,
    "start": BTN_START, "back": BTN_BACK,
    "left_thumb": BTN_LEFT_THUMB, "right_thumb": BTN_RIGHT_THUMB,
    "left_shoulder": BTN_LEFT_SHOULDER, "right_shoulder": BTN_RIGHT_SHOULDER,
    "guide": BTN_GUIDE,
    "a": BTN_A, "b": BTN_B, "x": BTN_X, "y": BTN_Y,
}

BUTTON_MASK = 0xFFFF
AXIS_MIN = -32768
AXIS_MAX = 32767
TRIGGER_MAX = 255

NEUTRAL = {"buttons": 0, "lt": 0, "rt": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0}

# Motor speeds are carried at the resolution ViGEmBus reports them in -- one byte each -- rather
# than XInput's 16 bits. Widening here would invent precision the source does not have; a client
# whose own API wants 16 bits multiplies by 257, which maps 255 exactly onto 65535.
MOTOR_MAX = 255
RUMBLE_SCALE_TO_16_BIT = 257

_REPORT_RANGES = (
    ("buttons", 0, BUTTON_MASK),
    ("lt", 0, TRIGGER_MAX),
    ("rt", 0, TRIGGER_MAX),
    ("lx", AXIS_MIN, AXIS_MAX),
    ("ly", AXIS_MIN, AXIS_MAX),
    ("rx", AXIS_MIN, AXIS_MAX),
    ("ry", AXIS_MIN, AXIS_MAX),
)


def _int(value, low, high) -> bool:
    # JSON's 1.0 and true both compare equal to 1 in Python; an integer field takes neither.
    return type(value) is int and low <= value <= high


def _clamp(value, low, high) -> int:
    return low if value < low else (high if value > high else int(value))


def _data(message, kind):
    """`message`'s data when it is a `kind` with an object for data, or None."""
    if not isinstance(message, dict) or message.get("type") != kind:
        return None
    data = message.get("data")
    return data if isinstance(data, dict) else None


# --- builders (the initiator's side) ---

def attach_msg(pad: int = 0, kind: str = KIND_X360) -> dict:
    """Asks the peer to plug a virtual pad in. Sent when a local pad appears, and again on every
    reconnect: a responder's targets die with its link, so nothing may be assumed to survive."""
    if not _int(pad, 0, MAX_PADS - 1):
        raise ValueError(f"a pad index is 0 to {MAX_PADS - 1}")
    if kind not in KINDS:
        raise ValueError(f"a pad kind is one of: {', '.join(KINDS)}")
    return {"type": MSG_GAMEPAD_ATTACH, "data": {"pad": pad, "kind": kind}}


def detach_msg(pad: int = 0) -> dict:
    if not _int(pad, 0, MAX_PADS - 1):
        raise ValueError(f"a pad index is 0 to {MAX_PADS - 1}")
    return {"type": MSG_GAMEPAD_DETACH, "data": {"pad": pad}}


def report_msg(pad: int = 0, seq: int = None, **fields) -> dict:
    """One full report, not a delta: a dropped frame then costs one poll's lag instead of leaving
    a button held down on the PC, which is the failure that matters because nothing on this side
    can tell a stuck button from a deliberate one."""
    if not _int(pad, 0, MAX_PADS - 1):
        raise ValueError(f"a pad index is 0 to {MAX_PADS - 1}")
    unknown = set(fields) - {name for name, _low, _high in _REPORT_RANGES}
    if unknown:
        raise ValueError(f"not report fields: {', '.join(sorted(unknown))}")
    data = {"pad": pad}
    for name, low, high in _REPORT_RANGES:
        data[name] = _clamp(fields.get(name, 0), low, high)
    if seq is not None:
        data["seq"] = seq
    return {"type": MSG_GAMEPAD, "data": data}


# --- validators (the responder's side) ---

def read_attach(message):
    """{"pad", "kind"} or None. An unknown `kind` is refused rather than taken as x360: a peer
    asking for a pad this side cannot emulate should be told, not quietly given another."""
    data = _data(message, MSG_GAMEPAD_ATTACH)
    if data is None:
        return None
    pad, kind = data.get("pad", 0), data.get("kind", KIND_X360)
    if not _int(pad, 0, MAX_PADS - 1) or kind not in KINDS:
        return None
    return {"pad": pad, "kind": kind}


def read_detach(message):
    data = _data(message, MSG_GAMEPAD_DETACH)
    if data is None:
        return None
    pad = data.get("pad", 0)
    if not _int(pad, 0, MAX_PADS - 1):
        return None
    return {"pad": pad}


def read_report(message):
    """{"pad", "seq", and every XUSB field} or None.

    A missing field reads as neutral, so a peer that only ever moves a stick need not send eight
    zeroes; a field that is present but out of range or not an integer makes the whole report
    malformed, because guessing at a stick position is worse than dropping a frame."""
    data = _data(message, MSG_GAMEPAD)
    if data is None:
        return None
    pad = data.get("pad", 0)
    seq = data.get("seq")
    if not _int(pad, 0, MAX_PADS - 1) or not _int(seq, 0, 2 ** 53 - 1):
        return None
    read = {"pad": pad, "seq": seq}
    for name, low, high in _REPORT_RANGES:
        value = data.get(name, 0)
        if not _int(value, low, high):
            return None
        read[name] = value
    return read


# --- rumble: responder to initiator ---

def rumble_msg(large: int = 0, small: int = 0, pad: int = 0) -> dict:
    """What the game asked the pad to do. `large` is the low-frequency motor, `small` the
    high-frequency one, both 0 to 255, as ViGEmBus reports them.

    Not sequenced. Only input from the initiator carries `seq` and is acknowledged (WIRE.md
    section 9); this goes the other way and is fire-and-forget, like `ack` itself. A lost rumble
    frame is a missed buzz, and the next one supersedes it anyway."""
    if not _int(pad, 0, MAX_PADS - 1):
        raise ValueError(f"a pad index is 0 to {MAX_PADS - 1}")
    return {"type": MSG_GAMEPAD_RUMBLE, "data": {
        "pad": pad,
        "large": _clamp(large, 0, MOTOR_MAX),
        "small": _clamp(small, 0, MOTOR_MAX),
    }}


def read_rumble(message):
    """{"pad", "large", "small"} or None.

    A malformed frame is dropped rather than read as silence: a client that treated junk as
    "stop" would stutter, and one that treated it as "go" could leave the motors running."""
    data = _data(message, MSG_GAMEPAD_RUMBLE)
    if data is None:
        return None
    pad = data.get("pad", 0)
    if not _int(pad, 0, MAX_PADS - 1):
        return None
    read = {"pad": pad}
    for name in ("large", "small"):
        value = data.get(name, 0)
        if not _int(value, 0, MOTOR_MAX):
            return None
        read[name] = value
    return read


STOP_RUMBLE = {"large": 0, "small": 0}


def button_names(buttons: int) -> list:
    """Which buttons a mask names, for a log line that a person can read."""
    return sorted(name for name, bit in BUTTON_NAMES.items() if buttons & bit)
