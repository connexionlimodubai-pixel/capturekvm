"""Pulling frames off the USB capture card.

The card is an ordinary UVC device, so Media Foundation sees it as a webcam and OpenCV's MSMF
back end opens it. Two settings decide whether a cheap HDMI grabber gives 1080p or a slideshow:
the pixel format must be MJPG, because a USB 2.0 card (the MS2109 class, which is most of the
sub-ten-dollar ones) only offers uncompressed YUY2 at small sizes and low rates, and the buffer
must be as short as the back end will allow, because every queued frame is a frame of lag.

The card's audio is a separate USB audio device and is deliberately not handled here: Windows's
own "Listen to this device" on the card's recording input routes it to the speakers with less
delay than anything this process could add. The README says so.
"""

import logging
import subprocess
import threading
import time

import cv2
import numpy as np

LOGGER = logging.getLogger("capturekvm.capture")

# Probing indices that hold no device makes MSMF warn once per index. The warnings say nothing a
# caller cannot see from the returned list, so they are turned down to errors.
try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except AttributeError:
    pass

PROBE_LIMIT = 8
REOPEN_SECONDS = 1.5

# What to ask a card for, best first. A USB 2.0 grabber takes the first; a USB 3.0 one (MS2130 and
# friends) takes it at 60.
FORMAT_LADDER = (
    ("MJPG", 1920, 1080, 60),
    ("MJPG", 1920, 1080, 30),
    ("MJPG", 1280, 720, 60),
    ("MJPG", 1280, 720, 30),
    ("YUY2", 1280, 720, 10),
    ("YUY2", 640, 480, 30),
)


def fourcc_code(text: str) -> int:
    """`VideoWriter_fourcc` moved onto the class in OpenCV 5; this works on both."""
    writer = getattr(cv2, "VideoWriter", None)
    if writer is not None and hasattr(writer, "fourcc"):
        return int(writer.fourcc(*text))
    return int(cv2.VideoWriter_fourcc(*text))        # noqa: N802 - OpenCV 4 spelling


def fourcc_text(code) -> str:
    try:
        value = int(code)
    except (TypeError, ValueError):
        return "?"
    if value <= 0:
        return "?"
    return "".join(chr((value >> shift) & 0xFF) for shift in (0, 8, 16, 24)).strip()


def pnp_camera_names() -> list:
    """The camera-like devices Windows knows about, as a hint beside the index list. Names cannot
    be mapped to OpenCV indices reliably, so they are shown rather than used."""
    script = (
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.PNPClass -in 'Camera','Image','Media' -and $_.Status -eq 'OK' } | "
        "Select-Object -ExpandProperty Name"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def list_devices(limit: int = PROBE_LIMIT) -> list:
    """Every index MSMF opens, with the format it settled on. Probing is the only honest way to
    enumerate: OpenCV exposes no device names, and an index that opens is one that works."""
    found = []
    for index in range(limit):
        capture = cv2.VideoCapture(index, cv2.CAP_MSMF)
        try:
            if not capture.isOpened():
                continue
            ok, frame = capture.read()
            found.append({
                "index": index,
                "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                "fps": round(float(capture.get(cv2.CAP_PROP_FPS)), 1),
                "fourcc": fourcc_text(capture.get(cv2.CAP_PROP_FOURCC)),
                "reads": bool(ok and frame is not None),
            })
        finally:
            capture.release()
    return found


def open_device(index: int, fourcc: str, width: int, height: int, fps: int):
    """One configured capture, or None. The order matters: MSMF picks the stream format from the
    FOURCC, so that goes first, then the size, then the rate."""
    capture = cv2.VideoCapture(index, cv2.CAP_MSMF)
    if not capture.isOpened():
        capture.release()
        return None
    try:
        if fourcc:
            capture.set(cv2.CAP_PROP_FOURCC, fourcc_code(fourcc))
        if width and height:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        if fps:
            capture.set(cv2.CAP_PROP_FPS, int(fps))
        # Every buffered frame is a frame of lag. MSMF may refuse; asking costs nothing.
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, frame = capture.read()
        if not ok or frame is None:
            capture.release()
            return None
        return capture
    except cv2.error:
        capture.release()
        return None


def negotiate(index: int, preferred=None):
    """Opens `index` with the first format that reads a frame, preferring `preferred` (a
    (fourcc, width, height, fps) tuple) and falling back down FORMAT_LADDER."""
    ladder = ([tuple(preferred)] if preferred else []) + [item for item in FORMAT_LADDER if item != tuple(preferred or ())]
    for fourcc, width, height, fps in ladder:
        capture = open_device(index, fourcc, width, height, fps)
        if capture is not None:
            actual = {
                "fourcc": fourcc_text(capture.get(cv2.CAP_PROP_FOURCC)),
                "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                "fps": round(float(capture.get(cv2.CAP_PROP_FPS)), 1),
                "asked": {"fourcc": fourcc, "width": width, "height": height, "fps": fps},
            }
            LOGGER.info("capture %d opened: %s", index, actual)
            return capture, actual
    return None, None


class CaptureWorker:
    """One capture card, read on its own thread.

    `on_frame(frame)` gets a BGR numpy array; it is called from the capture thread, so a UI must
    copy or marshal. `on_status(dict)` reports opening, the negotiated format, the measured rate
    and any failure. A card that is unplugged mid-run is reopened until `stop()`.
    """

    def __init__(self, on_frame, on_status=None, picture=None):
        # `picture` is a process.Picture, applied here rather than on the GUI thread: at 1080p
        # even a lookup table is milliseconds, and the window must not wait for it.
        self.picture = picture
        # Set by the app to see each frame as the card sent it, for measuring the range.
        self.on_raw = None
        self._on_frame = on_frame
        self._on_status = on_status or (lambda status: None)
        self._stop = threading.Event()
        self._thread = None
        self._request = None
        self._lock = threading.Lock()
        self.format = None
        self.measured_fps = 0.0
        self.frames = 0

    def start(self, index: int, fourcc: str = "MJPG", width: int = 1920, height: int = 1080, fps: int = 30) -> None:
        self.stop()
        with self._lock:
            self._request = (int(index), fourcc, int(width), int(height), int(fps))
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
        self.format = None
        self.measured_fps = 0.0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        with self._lock:
            index, fourcc, width, height, fps = self._request
        while not self._stop.is_set():
            self._on_status({"state": "opening", "index": index})
            capture, actual = negotiate(index, (fourcc, width, height, fps))
            if capture is None:
                self._on_status({"state": "failed", "index": index,
                                 "text": f"could not open capture device {index}"})
                if self._stop.wait(REOPEN_SECONDS):
                    break
                continue
            self.format = actual
            self._on_status({"state": "open", "index": index, "format": actual})
            try:
                self._pump(capture)
            finally:
                capture.release()
            if self._stop.is_set():
                break
            self._on_status({"state": "lost", "index": index, "text": "the card stopped sending frames"})
            if self._stop.wait(REOPEN_SECONDS):
                break
        self._on_status({"state": "stopped", "index": index})

    def _pump(self, capture) -> None:
        misses = 0
        counted = 0
        window_started = time.monotonic()
        while not self._stop.is_set():
            ok, frame = capture.read()
            if not ok or frame is None:
                misses += 1
                # A handful of empty reads is a mode change or a lost HDMI signal; a run of them
                # means the device is gone and the loop above should reopen it.
                if misses > 30:
                    return
                if self._stop.wait(0.03):
                    return
                continue
            misses = 0
            self.frames += 1
            counted += 1
            if self.on_raw is not None:
                self.on_raw(frame)
            picture = self.picture
            if picture is not None:
                try:
                    frame = picture.apply(frame)
                except Exception:                           # noqa: BLE001 - never lose the picture
                    LOGGER.exception("post-processing failed; passing the frame through")
                    self.picture = None
            self._on_frame(frame)
            now = time.monotonic()
            elapsed = now - window_started
            if elapsed >= 1.0:
                self.measured_fps = counted / elapsed
                counted = 0
                window_started = now
                self._on_status({"state": "rate", "fps": round(self.measured_fps, 1)})


def no_signal_frame(width: int = 1280, height: int = 720, text: str = "no signal") -> np.ndarray:
    """A placeholder so the window shows something truthful before the first frame arrives."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:] = (24, 20, 18)
    cv2.putText(frame, text, (int(width * 0.06), int(height * 0.52)),
                cv2.FONT_HERSHEY_SIMPLEX, width / 900.0, (120, 150, 170), 2, cv2.LINE_AA)
    return frame
