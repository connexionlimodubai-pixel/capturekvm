"""Qt events to wire messages: WIRE.md section 7's key names, buttons and scroll conventions.

Two rules from the specification drive the shape of this module. A character key travels as the
character it types, with Shift applied -- but with Control, Alt or Command held it travels
unshifted, so a shortcut lands on the same letter whatever the layout. And `us` carries what the
same physical key types on a US keyboard, which Windows reads out of `keytable.EVDEV_US` by set-1
scan code, so the PC can fall back to position when the character means nothing to it.

Left and right modifiers: Qt reports `Key_Shift` for both, so the side comes from the native
virtual key where Windows gives a sided one. Windows does not distinguish left from right Control
or Alt in every message, and this falls back to the left-hand name, which is what a one-sided
peer would do with it anyway.
"""

from PySide6.QtCore import Qt

from core import keytable

# Sided virtual keys (winuser.h). Only these prove which hand pressed a modifier.
_VK_SIDED = {
    0xA0: "shift", 0xA1: "shift_r",
    0xA2: "ctrl", 0xA3: "ctrl_r",
    0xA4: "alt", 0xA5: "alt_r",
    0x5B: "cmd", 0x5C: "cmd_r",
}

_MODIFIER_KEYS = {
    Qt.Key_Shift: "shift",
    Qt.Key_Control: "ctrl",
    Qt.Key_Alt: "alt",
    Qt.Key_AltGr: "alt_r",
    Qt.Key_Meta: "cmd",
    Qt.Key_CapsLock: "caps_lock",
}

_NAMED_KEYS = {
    Qt.Key_Return: "enter",
    Qt.Key_Backspace: "backspace",
    Qt.Key_Tab: "tab",
    Qt.Key_Backtab: "tab",
    Qt.Key_Escape: "esc",
    Qt.Key_Space: "space",
    Qt.Key_Delete: "delete",
    Qt.Key_Insert: "insert",
    Qt.Key_Home: "home",
    Qt.Key_End: "end",
    Qt.Key_PageUp: "page_up",
    Qt.Key_PageDown: "page_down",
    Qt.Key_Left: "left",
    Qt.Key_Right: "right",
    Qt.Key_Up: "up",
    Qt.Key_Down: "down",
    Qt.Key_Menu: "menu",
    Qt.Key_Print: "print_screen",
    Qt.Key_Pause: "pause",
    Qt.Key_NumLock: "num_lock",
    Qt.Key_ScrollLock: "scroll_lock",
    Qt.Key_VolumeMute: "volume_mute",
    Qt.Key_VolumeDown: "volume_down",
    Qt.Key_VolumeUp: "volume_up",
    Qt.Key_MediaNext: "media_next",
    Qt.Key_MediaPrevious: "media_prev",
    Qt.Key_MediaStop: "media_stop",
    Qt.Key_MediaTogglePlayPause: "media_play_pause",
    Qt.Key_MediaPlay: "media_play_pause",
    Qt.Key_MonBrightnessUp: "brightness_up",
    Qt.Key_MonBrightnessDown: "brightness_down",
    Qt.Key_MicMute: "mic_mute",
    Qt.Key_Back: "browser_back",
    Qt.Key_Forward: "browser_forward",
    Qt.Key_Stop: "browser_stop",
    Qt.Key_HomePage: "browser_home",
    Qt.Key_Refresh: "browser_refresh",
    Qt.Key_Search: "search",
    Qt.Key_Favorites: "browser_favourites",
    Qt.Key_LaunchMail: "mail",
    Qt.Key_Calculator: "calculator",
    Qt.Key_Hangul: "hangul",
    Qt.Key_Hangul_Hanja: "hanja",
}
_NAMED_KEYS.update({getattr(Qt, f"Key_F{i}"): f"f{i}" for i in range(1, 25) if hasattr(Qt, f"Key_F{i}")})

# The keypad. Qt reports the main-block key with KeypadModifier set, so the side comes from the
# modifier rather than the scan code, which Windows leaves ambiguous.
_KEYPAD_KEYS = {
    Qt.Key_0: "numpad_0", Qt.Key_1: "numpad_1", Qt.Key_2: "numpad_2", Qt.Key_3: "numpad_3",
    Qt.Key_4: "numpad_4", Qt.Key_5: "numpad_5", Qt.Key_6: "numpad_6", Qt.Key_7: "numpad_7",
    Qt.Key_8: "numpad_8", Qt.Key_9: "numpad_9",
    Qt.Key_Period: "numpad_decimal", Qt.Key_Comma: "numpad_decimal",
    Qt.Key_Plus: "numpad_add", Qt.Key_Minus: "numpad_subtract",
    Qt.Key_Asterisk: "numpad_multiply", Qt.Key_Slash: "numpad_divide",
    Qt.Key_Equal: "numpad_equal", Qt.Key_Enter: "numpad_enter", Qt.Key_Return: "numpad_enter",
    Qt.Key_Delete: "numpad_decimal",
}

BUTTONS = {
    Qt.LeftButton: "left",
    Qt.RightButton: "right",
    Qt.MiddleButton: "middle",
    Qt.XButton1: "back",
    Qt.XButton2: "forward",
}

# Keys Windows keeps for itself: the window never sees them, so the PC cannot be sent them from
# here. Listed so the UI can say so rather than leaving the user guessing.
UNREACHABLE = ("cmd", "cmd_r", "alt+tab", "ctrl+alt+delete", "print_screen")

WHEEL_UNITS_PER_NOTCH = 120.0


def physical_name(event) -> str:
    """The wire name of the key `event` pressed on this keyboard, before any cross-family
    mapping, or None when nothing sensible can be said about it."""
    key = event.key()
    modifiers = event.modifiers()

    if key in _MODIFIER_KEYS:
        return _VK_SIDED.get(event.nativeVirtualKey(), _MODIFIER_KEYS[key])

    if modifiers & Qt.KeypadModifier and key in _KEYPAD_KEYS:
        return _KEYPAD_KEYS[key]

    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]

    # A character key. With a command modifier held it goes unshifted, so Control-Shift-Z is
    # "ctrl" + "shift" + "z" rather than a capital.
    commanded = bool(modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier))
    if commanded:
        unshifted = keytable.EVDEV_US.get(event.nativeScanCode())
        if unshifted:
            return unshifted
        if 0x20 <= key <= 0x10FFFF:
            return chr(key).lower()
        return None

    text = event.text()
    if len(text) == 1 and text.isprintable():
        return text
    unshifted = keytable.EVDEV_US.get(event.nativeScanCode())
    if unshifted:
        return unshifted
    if 0x20 <= key <= 0x10FFFF:
        return chr(key).lower()
    return None


def us_name(event):
    """`us`: what this physical key types on a US keyboard, for a character key only. A named key
    needs none -- its name already says which key it is."""
    name = keytable.EVDEV_US.get(event.nativeScanCode())
    if name is None:
        return None
    physical = physical_name(event)
    if physical is None or physical in keytable.SPEC_NAMES:
        return None
    return name if name != physical else None


def key_fields(event, own_platform: str, peer_platform: str, style: str):
    """(key, us) for a `keydown` or `keyup`, or (None, None) for a key not worth sending."""
    physical = physical_name(event)
    if physical is None:
        return None, None
    wire = keytable.wire_name(physical, own_platform, peer_platform, style)
    return wire, us_name(event)


def button_name(button):
    return BUTTONS.get(button)


def scroll_fields(event, lines_per_notch: float = 3.0, natural: bool = False):
    """(dy, dx, mode) for a Qt wheel event, in the macOS sense the wire uses: a positive dy moves
    the view up. Windows reports a positive delta for a wheel pushed away, which moves the view
    up, so dy keeps its sign; the horizontal wheel is reversed, which WIRE.md section 7 asks a
    Windows sender to do before sending."""
    pixels = event.pixelDelta()
    if not pixels.isNull():
        dy, dx = float(pixels.y()), float(-pixels.x())
        mode = "pixel"
    else:
        angle = event.angleDelta()
        dy = angle.y() / WHEEL_UNITS_PER_NOTCH * lines_per_notch
        dx = -angle.x() / WHEEL_UNITS_PER_NOTCH * lines_per_notch
        mode = "line"
    if natural:
        dy, dx = -dy, -dx
    return dy, dx, mode
