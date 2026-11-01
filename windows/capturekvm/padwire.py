"""The gamepad messages, which are not in upstream Beamer.

Upstream wire version 6 has no pad: `docs/PAD-PROTOCOL.md` states the contract this module speaks
so a Beamer fork with a ViGEmBus target can answer it. Everything here is deliberately shaped
like `XUSB_REPORT`, the struct ViGEmBus takes for an Xbox 360 target, so a responder can memcpy
the fields straight into `vigem_target_x360_update` without rescaling anything.

If a fork names its messages differently, this file is the only one to change: `link.py` calls
`attach`, `detach` and `report` and knows nothing else about the pad.

Upstream's `parse_message` ignores a type it does not know, so sending these to a stock Beamer is
inert rather than fatal -- but an unacknowledged `seq` would stall the link's backlog, so
`link.py` only numbers pad frames when the peer advertises CAP_GAMEPAD.
"""

# Advertised in `caps` by both sides. A responder that does not offer it gets no pad frames.
CAP_GAMEPAD = "gamepad"

MSG_GAMEPAD_ATTACH = "gamepad_attach"
MSG_GAMEPAD_DETACH = "gamepad_detach"
MSG_GAMEPAD = "gamepad"

# Pad kinds a responder may support. x360 is the one ViGEmBus emulates most faithfully.
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
    "dpad_up": BTN_DPAD_UP,
    "dpad_down": BTN_DPAD_DOWN,
    "dpad_left": BTN_DPAD_LEFT,
    "dpad_right": BTN_DPAD_RIGHT,
    "start": BTN_START,
    "back": BTN_BACK,
    "left_thumb": BTN_LEFT_THUMB,
    "right_thumb": BTN_RIGHT_THUMB,
    "left_shoulder": BTN_LEFT_SHOULDER,
    "right_shoulder": BTN_RIGHT_SHOULDER,
    "guide": BTN_GUIDE,
    "a": BTN_A,
    "b": BTN_B,
    "x": BTN_X,
    "y": BTN_Y,
}

BUTTON_MASK = 0xFFFF
AXIS_MIN = -32768
AXIS_MAX = 32767
TRIGGER_MAX = 255


class PadState:
    """One pad's current reading, in XUSB units. Mutable on purpose: a reader fills it in place
    every poll and `link.py` compares it with what it last sent."""

    __slots__ = ("buttons", "lt", "rt", "lx", "ly", "rx", "ry")

    def __init__(self, buttons=0, lt=0, rt=0, lx=0, ly=0, rx=0, ry=0):
        self.buttons = buttons
        self.lt = lt
        self.rt = rt
        self.lx = lx
        self.ly = ly
        self.rx = rx
        self.ry = ry

    def fields(self) -> tuple:
        return (self.buttons, self.lt, self.rt, self.lx, self.ly, self.rx, self.ry)

    def copy(self) -> "PadState":
        return PadState(*self.fields())

    def clear(self) -> None:
        self.buttons = self.lt = self.rt = self.lx = self.ly = self.rx = self.ry = 0

    def __eq__(self, other) -> bool:
        return isinstance(other, PadState) and self.fields() == other.fields()

    def __repr__(self) -> str:
        return ("PadState(buttons=0x%04x lt=%d rt=%d lx=%d ly=%d rx=%d ry=%d)" % self.fields())


def _clamp(value, low, high) -> int:
    return low if value < low else (high if value > high else int(value))


def _pad_index(pad: int) -> int:
    if not isinstance(pad, int) or isinstance(pad, bool) or not 0 <= pad < MAX_PADS:
        raise ValueError(f"pad index must be 0 to {MAX_PADS - 1}")
    return pad


def attach(pad: int = 0, kind: str = KIND_X360) -> dict:
    """Asks the responder to plug a virtual pad in. Sent once a local pad appears, and again on
    every reconnect, because the responder's targets die with its link."""
    if kind not in KINDS:
        raise ValueError(f"pad kind must be one of: {', '.join(KINDS)}")
    return {"type": MSG_GAMEPAD_ATTACH, "data": {"pad": _pad_index(pad), "kind": kind}}


def detach(pad: int = 0) -> dict:
    """Unplugs it. A responder must also unplug every pad when the link drops, so a held stick
    cannot be left pressed on the PC."""
    return {"type": MSG_GAMEPAD_DETACH, "data": {"pad": _pad_index(pad)}}


def report(state: PadState, pad: int = 0, seq: int = None) -> dict:
    """One full report. Full rather than a delta: a dropped frame then costs one poll's lag
    instead of leaving the PC holding a button that was released."""
    data = {
        "pad": _pad_index(pad),
        "buttons": _clamp(state.buttons, 0, BUTTON_MASK),
        "lt": _clamp(state.lt, 0, TRIGGER_MAX),
        "rt": _clamp(state.rt, 0, TRIGGER_MAX),
        "lx": _clamp(state.lx, AXIS_MIN, AXIS_MAX),
        "ly": _clamp(state.ly, AXIS_MIN, AXIS_MAX),
        "rx": _clamp(state.rx, AXIS_MIN, AXIS_MAX),
        "ry": _clamp(state.ry, AXIS_MIN, AXIS_MAX),
    }
    if seq is not None:
        data["seq"] = seq
    return {"type": MSG_GAMEPAD, "data": data}


def read_report(message):
    """The responder's side, kept here so one file defines both directions. Returns the fields or
    None when the message is malformed; a fork's receiver can use it as-is."""
    if not isinstance(message, dict) or message.get("type") != MSG_GAMEPAD:
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    ranges = {
        "pad": (0, MAX_PADS - 1),
        "buttons": (0, BUTTON_MASK),
        "lt": (0, TRIGGER_MAX),
        "rt": (0, TRIGGER_MAX),
        "lx": (AXIS_MIN, AXIS_MAX),
        "ly": (AXIS_MIN, AXIS_MAX),
        "rx": (AXIS_MIN, AXIS_MAX),
        "ry": (AXIS_MIN, AXIS_MAX),
    }
    out = {}
    for field, (low, high) in ranges.items():
        value = data.get(field, 0)
        if type(value) is not int or not low <= value <= high:
            return None
        out[field] = value
    return out
