"""The keys Windows keeps for itself.

`grabKeyboard` does not get you the Windows key, Alt-Tab or Control-Escape. The shell claims them
below the window level, so the app never sees a Qt event and they cannot be forwarded -- which
means no Start menu and no task switching on the PC you are driving, the moment you try to use it
for real. A `WH_KEYBOARD_LL` hook sits below the shell and does see them. It is what Beamer
itself uses, for the same reason.

**Only the reserved keys go through here.** Everything else falls through to Qt's normal path,
which already handles layouts, dead keys and input methods properly; routing all typing through a
raw hook would throw that away to solve a problem it does not have. So the hook claims a short
list and passes on everything else.

What it claims is also swallowed locally -- returning 1 from the callback -- because the point is
that pressing the Windows key opens the PC's Start menu and not this machine's.

Control-Alt-Delete is absent and cannot be added by anyone. It is the secure attention sequence:
no hook sees it and no injection produces it, on this side or the PC's.

The callback runs on whichever thread installed the hook, during its message loop, so it is
installed from the GUI thread and does nothing but hand the key to a queue. Windows removes a
hook whose callback is too slow (LowLevelHooksTimeout), so it must never block.
"""

import ctypes
import logging
from ctypes import wintypes

from core import keytable

LOGGER = logging.getLogger("capturekvm.lowlevelkeys")

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLKHF_ALTDOWN = 0x20
LLKHF_UP = 0x80

VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_SNAPSHOT = 0x2C
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12

# vk: the wire name, for the keys this hook claims.
RESERVED_NAMES = {
    VK_LWIN: "cmd",
    VK_RWIN: "cmd_r",
    VK_TAB: "tab",
    VK_ESCAPE: "esc",
    VK_SNAPSHOT: "print_screen",
}

DOWN_MESSAGES = (WM_KEYDOWN, WM_SYSKEYDOWN)
UP_MESSAGES = (WM_KEYUP, WM_SYSKEYUP)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(KBDLLHOOKSTRUCT))


class ReservedKeys:
    """Claims the shell's keys while `active` is true, and hands them to `on_key`.

    `on_key(name, us, down)` is called on the GUI thread. `active` is a callable so the hook can
    stay installed across drive mode being turned on and off, rather than being torn down and
    rebuilt, which is a system-wide operation.
    """

    def __init__(self, on_key, active, release_chord=None):
        self._on_key = on_key
        self._active = active
        # The one chord that must never be swallowed: it is how the user lets go. It is passed
        # through so Qt sees it and the window can stop driving.
        self._release_chord = release_chord or (lambda vk: False)
        self._hook = None
        self._proc = None
        self._user32 = None
        self.installed = False
        self.error = None

    def install(self) -> bool:
        if self.installed:
            return True
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        except (OSError, AttributeError) as exc:
            self.error = f"no user32: {exc}"
            return False

        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM,
                                          ctypes.POINTER(KBDLLHOOKSTRUCT)]
        user32.CallNextHookEx.restype = ctypes.c_long
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short

        # Kept on the instance: if the ctypes callback is collected the next key press calls
        # freed memory, which takes the process with it.
        self._proc = HOOKPROC(self._callback)
        self._user32 = user32
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not hook:
            self.error = f"SetWindowsHookEx failed: {ctypes.get_last_error()}"
            LOGGER.info(self.error)
            self._proc = None
            return False
        self._hook = hook
        self.installed = True
        LOGGER.info("reserved-key hook installed")
        return True

    def remove(self) -> None:
        if self._hook and self._user32 is not None:
            self._user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._proc = None
        self.installed = False

    def _held(self, vk: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    def _claims(self, vk: int, flags: int) -> bool:
        """Whether this key is one the shell would otherwise eat."""
        if vk in (VK_LWIN, VK_RWIN):
            return True
        if vk == VK_TAB:
            # Alt-Tab. Plain Tab belongs to Qt, which handles it with the rest of typing.
            return bool(flags & LLKHF_ALTDOWN) or self._held(VK_MENU)
        if vk == VK_ESCAPE:
            # Control-Escape opens the Start menu; Control-Shift-Escape opens Task Manager.
            return self._held(VK_CONTROL)
        if vk == VK_SNAPSHOT:
            return True
        return False

    def _callback(self, code, wparam, lparam):
        passthrough = lambda: self._user32.CallNextHookEx(self._hook, code, wparam, lparam)
        if code < 0 or not lparam:
            return passthrough()
        try:
            event = lparam.contents
            vk = int(event.vkCode)
            flags = int(event.flags)

            if not self._active():
                return passthrough()
            # Never swallow a key this app injected, if it ever injects one.
            if flags & LLKHF_INJECTED:
                return passthrough()
            # The release chord has to reach Qt, or there is no way to stop driving.
            if self._release_chord(vk):
                return passthrough()
            if not self._claims(vk, flags):
                return passthrough()

            down = int(wparam) in DOWN_MESSAGES
            if int(wparam) not in DOWN_MESSAGES + UP_MESSAGES:
                return passthrough()

            name = RESERVED_NAMES.get(vk)
            if name is None:
                return passthrough()
            us = keytable.EVDEV_US.get(int(event.scanCode))
            self._on_key(name, us, down)
            return 1                                        # claimed: the shell never sees it
        except Exception:                                   # noqa: BLE001 - a raise here is fatal
            LOGGER.exception("reserved-key hook")
            return passthrough()


# What the palette offers, for anything a hook cannot deliver or a user would rather click.
# Each is (label, [key names pressed in order]); they are released in reverse.
CHORDS = (
    ("Start menu (Win)", ["cmd"]),
    ("Switch window (Alt+Tab)", ["alt", "tab"]),
    ("Task Manager (Ctrl+Shift+Esc)", ["ctrl", "shift", "esc"]),
    ("Show desktop (Win+D)", ["cmd", "d"]),
    ("Lock (Win+L)", ["cmd", "l"]),
    ("Run (Win+R)", ["cmd", "r"]),
    ("Snip (Win+Shift+S)", ["cmd", "shift", "s"]),
    ("Close window (Alt+F4)", ["alt", "f4"]),
    ("Print Screen", ["print_screen"]),
)
