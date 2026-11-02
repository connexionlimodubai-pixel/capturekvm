"""Virtual Xbox 360 pads on Windows, through ViGEmBus.

The bus driver is installed separately (see docs/GAMEPAD.md); this module only talks to its
user-mode library, ViGEmClient.dll, through ctypes. Nothing here is imported on any other
platform: `win_app/input_injector.py` reaches it only when it is asked to plug a pad in, and
`available()` is what decides whether the `gamepad` capability is offered at all.

`XUSB_REPORT` is the struct the driver takes, and it is also exactly what the wire carries
(core/gamepad.py), so a report crosses from a peer to the driver without being rescaled or
reinterpreted anywhere.

One lock guards every call. ViGEmClient is thread-safe for distinct targets but the bookkeeping
here is not, and reports arrive on a link's thread while a detach can come from the responder's.
"""

import ctypes
import logging
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

LOGGER = logging.getLogger("Beamer")

# How long a failed search for the library is remembered. Without this, every report on a machine
# with no driver would try four paths and log the same line again; with it permanent, a driver
# installed while Beamer runs would not be found until a restart.
RETRY_SECONDS = 30.0

# ViGEm/Common.h. Success is a value, not zero, which is easy to get wrong.
VIGEM_ERROR_NONE = 0x20000000

_ERROR_NAMES = {
    0x20000000: "none",
    0xE0000001: "bus not found: the ViGEmBus driver is not installed or not running",
    0xE0000002: "no free slot on the bus",
    0xE0000003: "invalid target",
    0xE0000004: "removal failed",
    0xE0000005: "already connected",
    0xE0000006: "target uninitialised",
    0xE0000007: "target not plugged in",
    0xE0000008: "bus version mismatch: the driver and ViGEmClient.dll disagree",
    0xE0000009: "bus access failed",
    0xE0000010: "callback already registered",
    0xE0000011: "callback not found",
    0xE0000012: "bus already connected",
    0xE0000013: "invalid bus handle",
    0xE0000014: "user index out of range",
    0xE0000015: "invalid parameter",
    0xE0000016: "not supported",
}

DLL_NAME = "ViGEmClient.dll"
DLL_ENV_VAR = "BEAMER_VIGEM_DLL"

MAX_PADS = 4


class XUSB_REPORT(ctypes.Structure):
    """ViGEm/Common.h. Field for field what `core.gamepad` puts on the wire."""

    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


def _candidate_paths():
    """Where ViGEmClient.dll might be, best first.

    The environment variable comes first so a user can point at one explicitly. Then the app's own
    directory, which is where the packagers put it. Then an installed `vgamepad`, which ships the
    same library and is the easiest way to get it. Bare `DLL_NAME` last, which lets Windows search
    its own path."""
    explicit = os.environ.get(DLL_ENV_VAR)
    if explicit:
        yield Path(explicit)

    here = Path(__file__).resolve().parent
    for directory in (here, here.parent, Path(getattr(sys, "_MEIPASS", here))):
        yield directory / DLL_NAME

    try:
        import vgamepad                                     # noqa: PLC0415 - optional, probed
    except Exception:                                       # noqa: BLE001 - absence is normal
        pass
    else:
        root = Path(vgamepad.__file__).resolve().parent
        for architecture in ("x64", "x86"):
            yield root / "win" / "vigem" / "client" / architecture / DLL_NAME

    yield Path(DLL_NAME)


class _Bus:
    """The connection to the driver and the targets plugged into it."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._dll = None
        self._client = None
        self._targets = {}                 # pad index: PVIGEM_TARGET
        self.dll_path = None
        self.error = None                  # why it is unavailable, for the status and the log
        self._failed_at = None             # when the search last came up empty

    # --- loading ---

    def _load_locked(self):
        if self._dll is not None:
            return self._dll
        if not sys.platform.startswith("win"):
            self.error = "virtual pads need Windows"
            return None
        if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_SECONDS:
            return None
        attempted = []
        for candidate in _candidate_paths():
            try:
                dll = ctypes.WinDLL(str(candidate))
            except OSError:
                attempted.append(str(candidate))
                continue
            self._declare(dll)
            self._dll = dll
            self.dll_path = str(candidate)
            self._failed_at = None
            LOGGER.info("ViGEmClient loaded from %s", self.dll_path)
            return dll
        self.error = (f"{DLL_NAME} was not found. Install ViGEmBus and put the library beside the "
                      f"app, or set {DLL_ENV_VAR} to it.")
        if self._failed_at is None:
            LOGGER.info("%s (looked in: %s)", self.error, ", ".join(attempted))
        self._failed_at = time.monotonic()
        return None

    @staticmethod
    def _declare(dll) -> None:
        dll.vigem_alloc.restype = ctypes.c_void_p
        dll.vigem_alloc.argtypes = []
        dll.vigem_free.restype = None
        dll.vigem_free.argtypes = [ctypes.c_void_p]
        dll.vigem_connect.restype = ctypes.c_uint
        dll.vigem_connect.argtypes = [ctypes.c_void_p]
        dll.vigem_disconnect.restype = None
        dll.vigem_disconnect.argtypes = [ctypes.c_void_p]
        dll.vigem_target_x360_alloc.restype = ctypes.c_void_p
        dll.vigem_target_x360_alloc.argtypes = []
        dll.vigem_target_free.restype = None
        dll.vigem_target_free.argtypes = [ctypes.c_void_p]
        dll.vigem_target_add.restype = ctypes.c_uint
        dll.vigem_target_add.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        dll.vigem_target_remove.restype = ctypes.c_uint
        dll.vigem_target_remove.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        # XUSB_REPORT goes by value, which is what the header says and what ctypes needs told.
        dll.vigem_target_x360_update.restype = ctypes.c_uint
        dll.vigem_target_x360_update.argtypes = [ctypes.c_void_p, ctypes.c_void_p, XUSB_REPORT]
        dll.vigem_target_is_attached.restype = wintypes.BOOL
        dll.vigem_target_is_attached.argtypes = [ctypes.c_void_p]

    def _connect_locked(self):
        if self._client is not None:
            return self._client
        dll = self._load_locked()
        if dll is None:
            return None
        client = dll.vigem_alloc()
        if not client:
            self.error = "ViGEmClient could not allocate its handle"
            return None
        result = dll.vigem_connect(client)
        if result != VIGEM_ERROR_NONE:
            dll.vigem_free(client)
            self.error = f"could not reach the ViGEmBus driver: {describe(result)}"
            LOGGER.warning("vigem_connect failed: %s", describe(result))
            return None
        self._client = client
        self.error = None
        LOGGER.info("Connected to the ViGEmBus driver")
        return client

    # --- what the injector calls ---

    def available(self) -> bool:
        """Whether a virtual pad can be plugged in. Connects on the first ask, because loading the
        library and reaching the driver is the only honest test, and the answer decides whether
        the `gamepad` capability is offered."""
        with self._lock:
            return self._connect_locked() is not None

    def attach(self, pad: int, kind: str = "x360") -> bool:
        if kind != "x360":
            # The vocabulary allows ds4, but a DS4 report is a different struct with a different
            # scale, and nothing on either side builds one. Declining is better than emulating
            # the wrong pad.
            LOGGER.info("A %r pad was asked for; only x360 is emulated", kind)
            return False
        with self._lock:
            if pad in self._targets:
                return True
            if not 0 <= pad < MAX_PADS:
                return False
            client = self._connect_locked()
            if client is None:
                return False
            target = self._dll.vigem_target_x360_alloc()
            if not target:
                self.error = "ViGEmClient could not allocate a pad"
                return False
            result = self._dll.vigem_target_add(client, target)
            if result != VIGEM_ERROR_NONE:
                self._dll.vigem_target_free(target)
                self.error = f"the virtual pad could not be plugged in: {describe(result)}"
                LOGGER.warning("vigem_target_add failed: %s", describe(result))
                return False
            self._targets[pad] = target
            return True

    def report(self, pad: int, buttons: int, lt: int, rt: int, lx: int, ly: int, rx: int, ry: int) -> bool:
        with self._lock:
            target = self._targets.get(pad)
            if target is None or self._client is None:
                return False
            payload = XUSB_REPORT(
                wButtons=buttons & 0xFFFF,
                bLeftTrigger=lt & 0xFF,
                bRightTrigger=rt & 0xFF,
                sThumbLX=_short(lx), sThumbLY=_short(ly),
                sThumbRX=_short(rx), sThumbRY=_short(ry),
            )
            result = self._dll.vigem_target_x360_update(self._client, target, payload)
            if result != VIGEM_ERROR_NONE:
                LOGGER.warning("vigem_target_x360_update failed: %s", describe(result))
                return False
            return True

    def detach(self, pad: int) -> None:
        with self._lock:
            target = self._targets.pop(pad, None)
            if target is None:
                return
            # Centre everything first: a target removed mid-trigger has been seen to leave the
            # last report standing in a game that was already reading it.
            if self._client is not None:
                try:
                    self._dll.vigem_target_x360_update(self._client, target, XUSB_REPORT())
                except Exception:                           # noqa: BLE001 - the removal matters more
                    pass
                try:
                    self._dll.vigem_target_remove(self._client, target)
                except Exception:
                    LOGGER.exception("vigem_target_remove")
            try:
                self._dll.vigem_target_free(target)
            except Exception:
                LOGGER.exception("vigem_target_free")

    def detach_all(self) -> None:
        with self._lock:
            for pad in list(self._targets):
                self.detach(pad)

    def shutdown(self) -> None:
        """Every target gone and the bus handle closed. Called when the app quits; leaving a
        target behind would leave a phantom controller in the PC's device list."""
        with self._lock:
            self.detach_all()
            if self._client is not None and self._dll is not None:
                try:
                    self._dll.vigem_disconnect(self._client)
                    self._dll.vigem_free(self._client)
                except Exception:
                    LOGGER.exception("vigem shutdown")
            self._client = None

    def pads(self) -> list:
        with self._lock:
            return sorted(self._targets)


def _short(value: int) -> int:
    return -32768 if value < -32768 else (32767 if value > 32767 else int(value))


def describe(result: int) -> str:
    return _ERROR_NAMES.get(result & 0xFFFFFFFF, f"error 0x{result & 0xFFFFFFFF:08X}")


_BUS = _Bus()

available = _BUS.available
attach = _BUS.attach
report = _BUS.report
detach = _BUS.detach
detach_all = _BUS.detach_all
shutdown = _BUS.shutdown
pads = _BUS.pads


def unavailable_reason():
    """Why no pad can be plugged in, or None when one can."""
    return None if _BUS.available() else (_BUS.error or "virtual pads are unavailable")
