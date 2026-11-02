"""The pad, as this client sends it.

The messages themselves are `core.gamepad`'s -- the same module the PC's responder validates them
with, now that `beamer-vigem/` is in this repository. This file used to carry its own copy of the
builders; it does not any more, because two definitions of one wire format are two things to keep
in step, and the symptom of their drifting apart is a controller that quietly does nothing.

What is left here is the part that belongs to a sender: `PadState`, a mutable reading that a poller
fills in place and compares against what it last sent. `core.gamepad` has no such thing, and
should not -- a responder has no use for it.

`docs/PAD-PROTOCOL.md` is the contract; `beamer-vigem/docs/GAMEPAD.md` is the specification.
"""

from core import gamepad

# Re-exported so callers say `padwire.BTN_A` and need not know which module defines it.
CAP_GAMEPAD = gamepad.CAP_GAMEPAD

MSG_GAMEPAD_ATTACH = gamepad.MSG_GAMEPAD_ATTACH
MSG_GAMEPAD_DETACH = gamepad.MSG_GAMEPAD_DETACH
MSG_GAMEPAD = gamepad.MSG_GAMEPAD

KIND_X360 = gamepad.KIND_X360
KIND_DS4 = gamepad.KIND_DS4
KINDS = gamepad.KINDS
MAX_PADS = gamepad.MAX_PADS

BTN_DPAD_UP = gamepad.BTN_DPAD_UP
BTN_DPAD_DOWN = gamepad.BTN_DPAD_DOWN
BTN_DPAD_LEFT = gamepad.BTN_DPAD_LEFT
BTN_DPAD_RIGHT = gamepad.BTN_DPAD_RIGHT
BTN_START = gamepad.BTN_START
BTN_BACK = gamepad.BTN_BACK
BTN_LEFT_THUMB = gamepad.BTN_LEFT_THUMB
BTN_RIGHT_THUMB = gamepad.BTN_RIGHT_THUMB
BTN_LEFT_SHOULDER = gamepad.BTN_LEFT_SHOULDER
BTN_RIGHT_SHOULDER = gamepad.BTN_RIGHT_SHOULDER
BTN_GUIDE = gamepad.BTN_GUIDE
BTN_A = gamepad.BTN_A
BTN_B = gamepad.BTN_B
BTN_X = gamepad.BTN_X
BTN_Y = gamepad.BTN_Y

BUTTON_NAMES = gamepad.BUTTON_NAMES
BUTTON_MASK = gamepad.BUTTON_MASK
AXIS_MIN = gamepad.AXIS_MIN
AXIS_MAX = gamepad.AXIS_MAX
TRIGGER_MAX = gamepad.TRIGGER_MAX

read_report = gamepad.read_report
button_names = gamepad.button_names

FIELDS = ("buttons", "lt", "rt", "lx", "ly", "rx", "ry")


class PadState:
    """One pad's current reading, in XUSB units.

    Mutable on purpose: a reader fills it in place every poll, and the link compares it with what
    it last sent so a pad at rest costs nothing."""

    __slots__ = FIELDS

    def __init__(self, buttons=0, lt=0, rt=0, lx=0, ly=0, rx=0, ry=0):
        self.buttons = buttons
        self.lt = lt
        self.rt = rt
        self.lx = lx
        self.ly = ly
        self.rx = rx
        self.ry = ry

    def fields(self) -> tuple:
        return tuple(getattr(self, name) for name in FIELDS)

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in FIELDS}

    def copy(self) -> "PadState":
        return PadState(*self.fields())

    def clear(self) -> None:
        for name in FIELDS:
            setattr(self, name, 0)

    def is_neutral(self) -> bool:
        return not any(self.fields())

    def __eq__(self, other) -> bool:
        return isinstance(other, PadState) and self.fields() == other.fields()

    def __repr__(self) -> str:
        return ("PadState(buttons=0x%04x lt=%d rt=%d lx=%d ly=%d rx=%d ry=%d)" % self.fields())


def attach(pad: int = 0, kind: str = KIND_X360) -> dict:
    """Asks the PC to plug a virtual pad in. Sent when a local pad appears, and again on every
    reconnect, because the responder's targets die with its link."""
    return gamepad.attach_msg(pad, kind)


def detach(pad: int = 0) -> dict:
    return gamepad.detach_msg(pad)


def report(state: PadState, pad: int = 0, seq: int = None) -> dict:
    """One full report, built by `core.gamepad` from this reading."""
    return gamepad.report_msg(pad, seq, **state.as_dict())
