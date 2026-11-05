"""Pulling frames off the USB capture card.

The card is an ordinary UVC device, so Windows sees it as a webcam. It is read the way OBS reads it
-- DirectShow for the frames, FFmpeg for the decoding (see ffcapture) -- and failing that through
OpenCV, by either of Windows's two camera APIs (see BACKENDS). Two settings decide whether a cheap HDMI
grabber gives 1080p or a slideshow: the pixel format must be MJPG, because a USB 2.0 card (the
MS2109 class, which is most of the sub-ten-dollar ones) only offers uncompressed YUY2 at small
sizes and low rates, and the buffer must be as short as the back end will allow, because every
queued frame is a frame of lag.

The card's audio is a separate USB audio device and is deliberately not handled here: Windows's
own "Listen to this device" on the card's recording input routes it to the speakers with less
delay than anything this process could add. The README says so.
"""

import logging
import subprocess
import threading
import time
import winreg

import cv2
import numpy as np

from capturekvm import ffcapture

LOGGER = logging.getLogger("capturekvm.capture")

# Probing indices that hold no device makes the back ends warn once per index. The warnings say
# nothing a caller cannot see from the returned list, so they are turned down to errors.
try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except AttributeError:
    pass

PROBE_LIMIT = 8
REOPEN_SECONDS = 1.5

# The ways a camera is read, in the order they are tried.
#
# FFmpeg first, when it is there. It reads through DirectShow too, but takes the card's MJPG as it
# arrives and decodes it itself, as OBS does, where OpenCV's DirectShow hands it to Windows's
# one-core MJPEG Decompressor -- the lag that remained after 0.4.3 (ffcapture says more). It is not
# an OpenCV back end: FFMPEG is only this file's name for it, and is never given to
# cv2.VideoCapture. FFmpeg numbers DirectShow devices in DirectShow's own order, so its index n
# and DirectShow's index n are the same card, which is what lets negotiate fall back from one to
# the other.
#
# Then OpenCV's DirectShow. It is what OBS, VLC, AMCap and the card makers' own tools use, so a card that
# shows a picture in any of those shows one here. Media Foundation is the newer API, and cheap
# HDMI grabbers are the devices it most often fails on: an index that will not open, or one that
# opens and never delivers a frame. The first version of this file used Media Foundation alone,
# and a card that worked in every other app on the PC did not appear here at all.
FFMPEG = int(cv2.CAP_FFMPEG)
HAVE_FFMPEG = ffcapture.available()
BACKENDS = ((("FFmpeg", FFMPEG),) if HAVE_FFMPEG else ()) + (
    ("DirectShow", cv2.CAP_DSHOW),
    ("Media Foundation", cv2.CAP_MSMF),
)
BACKEND_NAMES = {api: name for name, api in BACKENDS}

# The choice offered in the window, as the "capture_api" setting stores it. Auto tries each in
# BACKENDS order; the others use one way only, which is how they are compared on a card.
API_CHOICES = (("auto", "Auto"),) + ((("ffmpeg", "FFmpeg (like OBS)"),) if HAVE_FFMPEG else ()) + (
    ("dshow", "DirectShow"), ("msmf", "Media Foundation"))
_CHOICE_API = {"ffmpeg": FFMPEG, "dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF}


def backends_for(choice: str) -> tuple:
    wanted = _CHOICE_API.get(choice)
    chosen = tuple(item for item in BACKENDS if item[1] == wanted)
    return chosen or BACKENDS

# Windows's camera privacy switches. Any one of them at "Deny" refuses the camera to every desktop
# app, this one included, while a Store app such as Windows Camera may still show the card, which
# looks exactly like "it works everywhere else".
_CONSENT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"
CAMERA_PRIVACY = (
    (winreg.HKEY_LOCAL_MACHINE, _CONSENT, "camera access is turned off for this PC"),
    (winreg.HKEY_CURRENT_USER, _CONSENT, "camera access is turned off"),
    (winreg.HKEY_CURRENT_USER, _CONSENT + r"\NonPackaged",
     "\"Let desktop apps access your camera\" is turned off"),
)

# How many already-queued frames to throw away before showing one. Both back ends buffer, and
# CAP_PROP_BUFFERSIZE=1 is a request they may ignore, so the depth is drained rather than assumed.
# Bounded so a device producing faster than real time cannot spin this loop.
MAX_DRAIN = 4

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
    """The USB video devices Windows has installed, as a hint beside the index list. Names cannot
    be mapped to OpenCV indices reliably, so they are shown rather than used.

    Found by driver, not by device class. Webcams and HDMI grabbers alike run on Windows's
    `usbvideo` driver; the classes this used to match also hold every scanner, printer and sound
    card, which then turned up as possible capture cards. A device whose status is not OK keeps it
    in brackets, because "the driver failed" is the answer to a different question.
    """
    script = (
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.Service -eq 'usbvideo' } | "
        "ForEach-Object { $_.Status + '|' + $_.Name }"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in done.stdout.splitlines():
        status, _, name = line.strip().partition("|")
        if name:
            names.append(name if status == "OK" else f"{name} ({status})")
    return names


def _read_privacy(hive, path):
    with winreg.OpenKey(hive, path) as key:
        return winreg.QueryValueEx(key, "Value")[0]


def camera_blocked(read=_read_privacy):
    """Which privacy switch is keeping the camera from desktop apps, or None if none is.

    `read` is the registry read, swapped out by the tests. A switch Windows has never written is
    not a refusal, so a missing value counts as allowed."""
    for hive, path, what in CAMERA_PRIVACY:
        try:
            value = read(hive, path)
        except OSError:
            continue
        if str(value).strip().lower() == "deny":
            return what
    return None


def explain_no_devices(windows_sees: list, blocked) -> str:
    """Why the list is empty, in the order the causes are worth checking.

    The three causes look the same from in here, where nothing opens. They differ in what Windows
    itself reports, which is what this goes by: a privacy switch at Deny, a usbvideo device that is
    installed but that neither back end could open, or no usbvideo device at all.
    """
    if blocked:
        return (f"Windows is blocking the camera: {blocked}.\n\n"
                "Settings → Privacy & security → Camera: turn on camera access and "
                "\"Let desktop apps access your camera\", then press Rescan cards.")
    if windows_sees:
        return (f"Windows sees {', '.join(windows_sees)}, but it would not open.\n\n"
                "Another app is probably using it. Only one app can read a capture card at a time: "
                "close OBS, the Camera app or the card's own software, then press Rescan cards.")
    return ("Windows does not see a capture card.\n\n"
            "Check the USB cable and try another port. When Windows has it, it is listed under "
            "Cameras in Device Manager.")


def _probe(index: int, api: int, ffmpeg_devices=None):
    """What one back end makes of one index: None if it will not open, else the format it settled
    on and whether a frame actually came."""
    if api == FFMPEG:
        return _probe_ffmpeg(index, ffmpeg_devices or [])
    capture = cv2.VideoCapture(index, api)
    try:
        if not capture.isOpened():
            return None
        ok, frame = capture.read()
        return {
            "index": index,
            "api": int(api),
            "backend": BACKEND_NAMES.get(api, str(api)),
            "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": round(float(capture.get(cv2.CAP_PROP_FPS)), 1),
            "fourcc": fourcc_text(capture.get(cv2.CAP_PROP_FOURCC)),
            "reads": bool(ok and frame is not None),
        }
    except cv2.error:
        return None
    finally:
        capture.release()


def _describe_ffmpeg(index: int, feed, device: dict) -> dict:
    return {
        "index": index,
        "api": FFMPEG,
        "backend": BACKEND_NAMES.get(FFMPEG, "FFmpeg"),
        "name": device["name"],
        "width": int(feed.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(feed.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "fps": round(float(feed.get(cv2.CAP_PROP_FPS)), 1),
        "fourcc": feed.fourcc,
    }


def _probe_ffmpeg(index: int, devices: list):
    """FFmpeg's view of the index'th DirectShow camera, opened at whatever it offers first."""
    if index >= len(devices):
        return None
    feed = ffcapture.FFmpegFeed.open(devices[index]["id"])
    if feed is None:
        return None
    try:
        ok, frame = feed.read(ffcapture.FIRST_READ_TIMEOUT)
        seen = _describe_ffmpeg(index, feed, devices[index])
        seen["reads"] = bool(ok and frame is not None)
        return seen
    except Exception:                                   # noqa: BLE001 -- then OpenCV gets its turn
        LOGGER.exception("probing %s through FFmpeg", devices[index]["name"])
        return None
    finally:
        feed.release()


def _ffmpeg_devices(apis) -> list:
    """FFmpeg's device list, fetched once per scan or open, and only when FFmpeg is to be tried."""
    if FFMPEG not in apis:
        return []
    try:
        return ffcapture.list_video_devices()
    except Exception:                                   # noqa: BLE001 -- OpenCV's list still stands
        LOGGER.exception("listing devices through FFmpeg")
        return []


def list_devices(limit: int = PROBE_LIMIT, backends=BACKENDS) -> list:
    """One entry per camera index, through whichever back end works for it.

    For each index the back ends are tried in order: the first that delivers a frame wins, and
    failing that the first that at least opened it, so a card is offered once, through the API
    that actually gets its picture. Probing is the only honest way to enumerate: OpenCV gives no
    names, and an index that reads is one that works.

    Indices are positions in Windows's list of cameras, so they run 0, 1, 2 without gaps, and two
    empty in a row is the end of the list. Two rather than one, so a camera another app is holding,
    which opens under neither API, does not hide a card at the index after it.
    """
    found, misses = [], 0
    ffmpeg_devices = _ffmpeg_devices([api for _, api in backends])
    for index in range(limit):
        best = None
        for _, api in backends:
            seen = _probe(index, api, ffmpeg_devices)
            if seen is None:
                continue
            if seen["reads"]:
                best = seen
                break
            best = best or seen
        if best is None:
            misses += 1
            if misses >= 2:
                break
            continue
        misses = 0
        found.append(best)
    return found


def _configure(capture, api: int, fourcc: str, width: int, height: int, fps: int) -> None:
    """Ask for a format, a size and a rate, in the order the back end needs them.

    Media Foundation picks the stream from the FOURCC, so that goes first, then the size, then
    the rate.

    DirectShow is the other way round, and getting it wrong is what made 0.4.1 and 0.4.2 crawl.
    In OpenCV's DirectShow code (modules/videoio/src/cap_dshow.cpp) setting the FOURCC re-sets the
    device up at its current size and then forgets the FOURCC it was given; the next size change
    re-sets the device up again with no format at all, and with no format it takes the first of a
    list that runs RGB24, RGB32, RGB555, RGB565, YUY2 ... and reaches MJPG sixteenth. A rate
    change re-sets it up with no format either. So FOURCC, width, height, rate -- this file's
    order until now -- left a USB 2.0 card on uncompressed YUY2 at 1080p: about five frames a
    second, while the API went on reporting the 30 it was asked for.

    The FOURCC therefore goes last on DirectShow. It re-sets the device up at the size already
    asked for, keeping the requested rate, which DirectShow remembers across set-ups.
    """
    def size():
        if width and height:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))

    def rate():
        if fps:
            capture.set(cv2.CAP_PROP_FPS, int(fps))

    def format_():
        if fourcc:
            capture.set(cv2.CAP_PROP_FOURCC, fourcc_code(fourcc))

    steps = (size, rate, format_) if api == cv2.CAP_DSHOW else (format_, size, rate)
    for step in steps:
        step()
    # Every buffered frame is a frame of lag. Either back end may refuse; asking costs nothing.
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)


def open_device(index: int, fourcc: str, width: int, height: int, fps: int, api: int = cv2.CAP_DSHOW):
    """One configured capture, or None.

    On DirectShow the format it ended up with is read back, and a capture that did not get the
    one asked for is refused rather than used: a card quietly running YUY2 where MJPG was asked
    is the five-frames-a-second failure above, and refusing it lets the format ladder try the next
    rung instead of showing a slideshow. DirectShow's FOURCC read-back is the format it actually
    set up (OpenCV keeps it as the device's video type), which is not true of its frame rate.
    """
    capture = cv2.VideoCapture(index, api)
    if not capture.isOpened():
        capture.release()
        return None
    try:
        _configure(capture, api, fourcc, width, height, fps)
        if api == cv2.CAP_DSHOW and fourcc:
            got = fourcc_text(capture.get(cv2.CAP_PROP_FOURCC))
            if got.upper() != fourcc.upper():
                LOGGER.info("capture %d: asked DirectShow for %s, it set up %s; trying the next format",
                            index, fourcc, got)
                capture.release()
                return None
        ok, frame = capture.read()
        if not ok or frame is None:
            capture.release()
            return None
        return capture
    except cv2.error:
        capture.release()
        return None


def open_ffmpeg(index: int, fourcc: str, width: int, height: int, fps: int, devices: list):
    """FFmpeg's capture of the index'th DirectShow camera at one rung of the ladder, or None.

    No read-back check is needed here, unlike OpenCV's DirectShow: FFmpeg only opens a format
    that has the size and rate asked for, and refuses otherwise. It may give raw video where MJPG
    was asked, on a USB 3.0 card that can carry raw at that size and rate, which is better still."""
    if index >= len(devices):
        return None
    feed = ffcapture.FFmpegFeed.open(devices[index]["id"], width, height, fps,
                                     ffcapture.PIXEL_FORMAT_FOR.get((fourcc or "").upper()))
    if feed is None:
        return None
    ok, frame = feed.read(ffcapture.FIRST_READ_TIMEOUT)
    if not ok or frame is None:
        feed.release()
        return None
    return feed


def negotiate(index: int, preferred=None, api=None):
    """Opens `index` with the first format that reads a frame, preferring `preferred` (a
    (fourcc, width, height, fps) tuple) and falling back down FORMAT_LADDER.

    With `api`, the back end the device list found this card through, only that one is used.
    Trying the other would be trying a different list: the two APIs usually number cameras the
    same way but are not obliged to, and on a PC with a webcam as well, "index 1 through the other
    API" can be the webcam. Without `api`, from a setting saved before there was a choice, each
    back end is tried in BACKENDS order.
    """
    ladder = ([tuple(preferred)] if preferred else []) + [item for item in FORMAT_LADDER if item != tuple(preferred or ())]
    if api == FFMPEG:
        # The one exception to "only that API": FFmpeg's index n is DirectShow's index n, so a
        # card FFmpeg will not open is still worth DirectShow's try. Without PyAV in the build,
        # a saved FFmpeg choice comes to DirectShow alone.
        apis = ([FFMPEG] if HAVE_FFMPEG else []) + [cv2.CAP_DSHOW]
    else:
        apis = [int(api)] if api else [backend for _, backend in BACKENDS]
    devices = _ffmpeg_devices(apis)
    for backend in apis:
        for fourcc, width, height, fps in ladder:
            if backend == FFMPEG:
                if index >= len(devices):
                    break
                feed = open_ffmpeg(index, fourcc, width, height, fps, devices)
                if feed is not None:
                    actual = _describe_ffmpeg(index, feed, devices[index])
                    actual["asked"] = {"fourcc": fourcc, "width": width, "height": height, "fps": fps}
                    LOGGER.info("capture %d opened: %s", index, actual)
                    return feed, actual
                continue
            capture = open_device(index, fourcc, width, height, fps, backend)
            if capture is not None:
                actual = {
                    "fourcc": fourcc_text(capture.get(cv2.CAP_PROP_FOURCC)),
                    "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                    "fps": round(float(capture.get(cv2.CAP_PROP_FPS)), 1),
                    "backend": BACKEND_NAMES.get(backend, str(backend)),
                    "api": backend,
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
        self.dropped_stale = 0      # queued frames thrown away to show a newer one
        # Whether to skip queued frames to reach the newest. The setting; whether it is acted on
        # depends on the API the card opened through, see _run.
        self.drain = True
        self._draining = True

    def start(self, index: int, fourcc: str = "MJPG", width: int = 1920, height: int = 1080, fps: int = 30,
              api=None) -> None:
        self.stop()
        with self._lock:
            self._request = (int(index), fourcc, int(width), int(height), int(fps), api)
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
            index, fourcc, width, height, fps, api = self._request
        while not self._stop.is_set():
            self._on_status({"state": "opening", "index": index})
            capture, actual = negotiate(index, (fourcc, width, height, fps), api)
            if capture is None:
                through = f" through {BACKEND_NAMES[api]}" if api in BACKEND_NAMES else ""
                self._on_status({"state": "failed", "index": index,
                                 "text": f"Could not open capture device {index}{through}.\n\n"
                                         "If another app is showing the card, close it: only one app "
                                         "can read a capture card at a time. Then press Rescan cards."})
                if self._stop.wait(REOPEN_SECONDS):
                    break
                continue
            self.format = actual
            # DirectShow keeps only the newest frame -- its callback overwrites one buffer, and
            # retrieve() waits for the next -- so there is no queue to skip ahead in. Its grab()
            # also returns at once without waiting for anything, which _newest reads as "frames
            # are queued" and counted four phantom skips a frame. FFmpeg's feed keeps only the
            # newest itself, and counts what it skips. Draining is for Media Foundation, whose
            # reader does queue.
            self._draining = self.drain and actual.get("api") not in (cv2.CAP_DSHOW, FFMPEG)
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

    def _newest(self, capture, frame_interval: float):
        """(ok, frame, stale) -- the freshest frame the device has, and how many were skipped.

        `grab()` returns as soon as a frame is ready. When several are already queued each call
        comes back almost instantly; once caught up, it blocks for roughly a frame interval. That
        difference is the only signal available for how deep the queue is, so it is what this
        reads: keep grabbing while it is cheap, then retrieve the last one.

        Without it the picture is as old as the deepest buffer in the chain, which for a KVM is
        the difference between usable and not."""
        if not self._draining:
            ok, frame = capture.read()
            take_skipped = getattr(capture, "take_skipped", None)
            return ok, frame, take_skipped() if callable(take_skipped) else 0

        quick = max(0.001, frame_interval / 3.0)
        stale = 0
        while True:
            started = time.monotonic()
            if not capture.grab():
                return False, None, stale
            if time.monotonic() - started > quick or stale >= MAX_DRAIN:
                break
            stale += 1
        ok, frame = capture.retrieve()
        return ok, frame, stale

    def _pump(self, capture) -> None:
        misses = 0
        counted = 0
        window_started = time.monotonic()
        while not self._stop.is_set():
            ok, frame, stale = self._newest(capture, 1.0 / max(1.0, self.measured_fps or 30.0))
            self.dropped_stale += stale
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
            captured_at = time.perf_counter()
            picture = self.picture
            if picture is not None:
                try:
                    frame = picture.apply(frame)
                except Exception:                           # noqa: BLE001 - never lose the picture
                    LOGGER.exception("post-processing failed; passing the frame through")
                    self.picture = None
            self._on_frame(frame, captured_at)
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
