"""Reading the pad plugged into this machine, through XInput.

XInput rather than Qt: Qt 6 dropped QtGamepad, and XInput's `XINPUT_GAMEPAD` is field-for-field
the struct ViGEmBus takes for an Xbox 360 target, so a reading taken here needs no rescaling on
its way to the PC. `XInputGetStateEx` (ordinal 100, undocumented but present since XInput 1.3) is
tried first because it is the only way to see the Guide button.

`dwPacketNumber` changes only when the pad's state does, so the poller sends a frame on change
and a keepalive now and then, instead of a frame per poll.
"""

import ctypes
import ctypes.util
import logging
import sys
import threading
import time

from .padwire import PadState

LOGGER = logging.getLogger("capturekvm.gamepad")

ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 1167
XUSER_MAX_COUNT = 4

DEFAULT_POLL_HZ = 125.0
KEEPALIVE_SECONDS = 0.5


class _XInputGamepad(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", _XInputGamepad)]


class _XInputVibration(ctypes.Structure):
    """XINPUT_VIBRATION. Left is the low-frequency (large) motor, right the high-frequency one."""

    _fields_ = [("wLeftMotorSpeed", ctypes.c_ushort), ("wRightMotorSpeed", ctypes.c_ushort)]


class XInput:
    """The DLL, or `available` false when no XInput is present (a Windows install without the
    DirectX runtime, or this module imported off Windows for a test)."""

    DLLS = ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll")

    def __init__(self):
        self.dll = None
        self.name = None
        self._get_state = None
        self._set_state = None
        self.sees_guide = False
        for candidate in self.DLLS:
            try:
                self.dll = ctypes.WinDLL(candidate)
            except (OSError, AttributeError):
                continue
            self.name = candidate
            break
        if self.dll is None:
            return
        try:
            # Ordinal 100 is XInputGetStateEx, which reports the Guide button in bit 0x0400.
            self._get_state = self.dll[100]
            self.sees_guide = True
        except (AttributeError, ValueError, OSError):
            self._get_state = getattr(self.dll, "XInputGetState", None)
        if self._get_state is not None:
            self._get_state.argtypes = [ctypes.c_ulong, ctypes.POINTER(_XInputState)]
            self._get_state.restype = ctypes.c_ulong
        self._set_state = getattr(self.dll, "XInputSetState", None)
        if self._set_state is not None:
            self._set_state.argtypes = [ctypes.c_ulong, ctypes.POINTER(_XInputVibration)]
            self._set_state.restype = ctypes.c_ulong

    @property
    def available(self) -> bool:
        return self._get_state is not None

    def read(self, user_index: int):
        """(packet_number, PadState) for a connected pad, or None when that slot is empty."""
        if not self.available:
            return None
        state = _XInputState()
        result = self._get_state(ctypes.c_ulong(user_index), ctypes.byref(state))
        if result != ERROR_SUCCESS:
            if result != ERROR_DEVICE_NOT_CONNECTED:
                LOGGER.debug("XInputGetState(%d) returned %d", user_index, result)
            return None
        pad = state.Gamepad
        return state.dwPacketNumber, PadState(
            buttons=pad.wButtons,
            lt=pad.bLeftTrigger,
            rt=pad.bRightTrigger,
            lx=pad.sThumbLX,
            ly=pad.sThumbLY,
            rx=pad.sThumbRX,
            ry=pad.sThumbRY,
        )

    def rumble(self, user_index: int, large: int, small: int) -> bool:
        """Drive the motors. `large` and `small` are 0-255, as the wire carries them.

        XInput wants 16 bits, so each is multiplied by 257 -- which maps 255 exactly onto 65535
        rather than leaving the top of the range unreachable."""
        if self._set_state is None:
            return False
        vibration = _XInputVibration(
            wLeftMotorSpeed=max(0, min(255, int(large))) * 257,
            wRightMotorSpeed=max(0, min(255, int(small))) * 257,
        )
        result = self._set_state(ctypes.c_ulong(user_index), ctypes.byref(vibration))
        return result == ERROR_SUCCESS

    def connected(self) -> list:
        return [index for index in range(XUSER_MAX_COUNT) if self.read(index) is not None]


class PadPoller:
    """Polls one local pad and reports it.

    `on_report(PadState)` fires when the pad's state changes and every KEEPALIVE_SECONDS while it
    is anything but neutral; `on_presence(connected: bool)` fires when a pad appears or goes away,
    which is when the PC must be told to plug a virtual one in or pull it out.

    `slot` is the XInput user index, or None to follow whichever pad is plugged in first."""

    def __init__(self, on_report, on_presence=None, slot=None, poll_hz: float = DEFAULT_POLL_HZ,
                 xinput: XInput = None):
        self.xinput = xinput or XInput()
        self._on_report = on_report
        self._on_presence = on_presence or (lambda connected: None)
        self.slot = slot
        self.interval = 1.0 / max(1.0, poll_hz)
        self._stop = threading.Event()
        self._thread = None
        self.connected_slot = None
        self._winmm = None
        self._packet = None
        self._last_sent = None
        self._last_sent_at = 0.0

    @property
    def available(self) -> bool:
        return self.xinput.available

    def _raise_timer_resolution(self) -> None:
        """Ask Windows for a 1 ms scheduler tick while polling.

        Without it the default is 15.6 ms, so a poller asking for 8 ms actually runs at about
        64 Hz -- measured, not assumed -- which halves the pad's sample rate and adds about 8 ms
        to the average age of every reading. Released again in stop(), because it is a global
        setting and holding it costs power.
        """
        if not sys.platform.startswith("win") or self._winmm is not None:
            return
        try:
            winmm = ctypes.WinDLL("winmm")
            if winmm.timeBeginPeriod(1) == 0:
                self._winmm = winmm
        except (OSError, AttributeError):
            LOGGER.info("could not raise the timer resolution; the pad will poll more slowly")

    def _release_timer_resolution(self) -> None:
        winmm, self._winmm = self._winmm, None
        if winmm is not None:
            try:
                winmm.timeEndPeriod(1)
            except OSError:
                pass

    def start(self) -> None:
        if self._thread is not None or not self.available:
            return
        self._raise_timer_resolution()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pad-poller", daemon=True)
        self._thread.start()

    def rumble(self, large: int, small: int) -> bool:
        """What the PC asked for, sent to the pad actually plugged in here."""
        slot = self.connected_slot
        if slot is None:
            return False
        return self.xinput.rumble(slot, large, small)

    def stop_rumble(self) -> None:
        """Always safe to call. A pad left buzzing because a link dropped is the rumble version
        of a stuck key, so this is called on every way out."""
        slot = self.connected_slot
        if slot is not None:
            self.xinput.rumble(slot, 0, 0)

    def stop(self, timeout: float = 1.0) -> None:
        self.stop_rumble()
        self._stop.set()
        self._release_timer_resolution()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
        if self.connected_slot is not None:
            self.connected_slot = None
            self._on_presence(False)

    def _slots(self) -> range:
        return range(XUSER_MAX_COUNT) if self.slot is None else range(self.slot, self.slot + 1)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._tick(started)
            except Exception:                              # noqa: BLE001 - the poller must not die
                LOGGER.exception("pad poll")
            remaining = self.interval - (time.monotonic() - started)
            if remaining > 0:
                self._stop.wait(remaining)

    def _tick(self, now: float) -> None:
        reading = None
        slot = None
        for index in self._slots():
            reading = self.xinput.read(index)
            if reading is not None:
                slot = index
                break

        if reading is None:
            if self.connected_slot is not None:
                self.connected_slot = None
                self._packet = self._last_sent = None
                self._on_presence(False)
            return

        if slot != self.connected_slot:
            self.connected_slot = slot
            self._packet = self._last_sent = None
            self._on_presence(True)

        packet, state = reading
        changed = packet != self._packet or state != self._last_sent
        stale = self._last_sent is not None and now - self._last_sent_at >= KEEPALIVE_SECONDS
        if not changed and not (stale and state != PadState()):
            return
        self._packet = packet
        self._last_sent = state.copy()
        self._last_sent_at = now
        self._on_report(state)
