"""Reading a capture card the way OBS does: DirectShow for the frames, FFmpeg for the decoding.

OpenCV's DirectShow path cannot do this. For MJPG it always builds a DirectShow graph that decodes
through Windows's own MJPEG Decompressor filter and then flips bottom-up RGB, whatever it is asked;
that filter runs on one core, and when it cannot keep up at 1080p the card's frames wait in front
of it, which reads as lag rather than as a lower frame rate. A laptop running a card smoothly in
OBS and laggily here -- at about a third of its CPU, and better at 720p -- is that. OBS takes the
compressed frames from DirectShow and decodes them with FFmpeg, and so does this, through PyAV:
on the PC this was written on, 7.3 ms to decode a 1080p MJPG frame and 1.9 ms to make BGR of it.

Two things keep it from adding delay of its own:

* **Only the newest frame is ever decoded.** FFmpeg's DirectShow input queues what the card sends
  (up to `rtbufsize`, about eight 1080p frames by default), so a reader that falls behind falls
  further behind. A thread here does nothing but take packets off that queue -- cheap enough
  never to fall behind -- and keeps the newest; the capture thread decodes whichever is newest
  when it is ready for another. Every MJPG frame stands alone, so a frame skipped is just
  skipped.
* **Nothing ever blocks where it cannot be stopped.** FFmpeg's DirectShow read waits on
  `WaitForMultipleObjects(..., INFINITE)` and never checks an interrupt, so a blocking read on a
  card that has gone quiet cannot be cancelled, and closing the device under it frees memory a
  waiting thread is using. The reader runs in FFmpeg's non-blocking mode instead, which returns
  at once when no frame is waiting, and the device is closed only after that thread has stopped.

Devices are listed by FFmpeg's own `list_devices`, which prints each as `"Name" (video)` with an
`Alternative name "..."` beneath; the alternative name is unique, so two cards with the same name
cannot be confused, and it is what a device is opened by. FFmpeg lists DirectShow devices in
DirectShow's order, which is the order OpenCV numbers them in too.

PyAV is imported lazily, so a build without it still has OpenCV's two paths.
"""

import logging
import re
import threading
import time

LOGGER = logging.getLogger("capturekvm.ffcapture")

# How long the reader sleeps when FFmpeg has no frame waiting. A frame at 60 fps is 16.7 ms
# apart, so this adds at most a couple of milliseconds to any frame, at a few hundred cheap
# wake-ups a second.
POLL_SECONDS = 0.002

# A frame read gives up after this long, so a card that goes quiet is noticed and reopened by the
# capture loop, which counts misses, rather than waited on forever.
READ_TIMEOUT = 1.0

_DEVICE = re.compile(r'^"(?P<name>.*)" \((?P<kinds>[^)]*)\)$')
_ALTERNATIVE = re.compile(r'^\s*Alternative name "(?P<id>.*)"$')

# A first frame can take longer than a later one: the card has to start streaming.
FIRST_READ_TIMEOUT = 3.0

# FFmpeg's DirectShow queue limit; see FFmpegFeed.open for why it is raised. About sixteen raw
# 1080p frames, or a hundred-odd MJPG ones.
RTBUFSIZE = "64M"

_FOURCC_BY_CODEC = {"mjpeg": "MJPG", "h264": "H264", "hevc": "HEVC"}
_FOURCC_BY_PIXEL = {"yuyv422": "YUY2", "nv12": "NV12", "uyvy422": "UYVY", "bgr24": "RGB24"}

# What the format ladder's raw FOURCCs are called in FFmpeg. MJPG has no entry: it is not a pixel
# format, and DirectShow's size-and-rate filter already leaves MJPG as the only choice where it
# matters (see FFmpegFeed.open).
PIXEL_FORMAT_FOR = {"YUY2": "yuyv422", "NV12": "nv12", "UYVY": "uyvy422"}


def fourcc_number(text: str) -> int:
    """A FOURCC as OpenCV's CAP_PROP_FOURCC reports one, so callers read both paths alike."""
    text = (text or "").ljust(4)[:4]
    return sum(ord(char) << (8 * place) for place, char in enumerate(text))


def available() -> bool:
    try:
        import av                                   # noqa: F401 -- probed
    except Exception:                               # noqa: BLE001 -- absence is the answer
        return False
    return True


def parse_device_list(text: str) -> list:
    """The video devices in FFmpeg's `list_devices` output, as [{"name", "id"}], in its order.

    The output arrives in fragments -- the name, " (video", ")", a newline -- which are joined
    before this sees them. A device with no alternative name is opened by its friendly name."""
    devices, current = [], None
    for line in text.splitlines():
        line = line.rstrip()
        match = _DEVICE.match(line.strip())
        if match:
            kinds = [kind.strip() for kind in match.group("kinds").split(",")]
            current = {"name": match.group("name"), "id": match.group("name")} if "video" in kinds else None
            if current is not None:
                devices.append(current)
            continue
        match = _ALTERNATIVE.match(line)
        if match and current is not None:
            current["id"] = match.group("id")
            current = None
    return devices


def list_video_devices() -> list:
    """The DirectShow video devices FFmpeg can see, in DirectShow's order. Empty without PyAV."""
    try:
        import av
        import av.logging
    except Exception:                               # noqa: BLE001
        return []
    previous = av.logging.get_level()
    av.logging.set_level(av.logging.INFO)
    try:
        with av.logging.Capture() as logs:
            try:
                av.open("dummy", format="dshow", options={"list_devices": "true"}).close()
            except Exception:                       # noqa: BLE001 -- listing always "fails"
                pass
    finally:
        av.logging.set_level(previous)
    text = "".join(message for _, name, message in logs if name == "dshow")
    return parse_device_list(text)


class FFmpegFeed:
    """One card, opened through FFmpeg's DirectShow input, read as a cv2.VideoCapture would be.

    `read()` returns the newest frame as BGR, decoding nothing older; see the module docstring
    for why that and the non-blocking reader are the whole point. `skipped` counts the frames
    dropped undecoded because a newer one had already arrived.
    """

    def __init__(self, container, stream):
        self._container = container
        self._stream = stream
        self._codec = stream.codec_context
        self._lock = threading.Lock()
        self._fresh = threading.Event()
        self._latest = None
        self._closing = False
        self.failed = None
        self.skipped = 0
        self._shape = None
        # One packet in, one frame out. FFmpeg's MJPEG decoder has no frame threading today, but
        # if a build ever gave it some, frame threads hold frames back by design -- each decode
        # would return a frame from several packets ago, which is lag by another name.
        try:
            self._codec.thread_type = "SLICE"
        except Exception:                           # noqa: BLE001 -- an older PyAV: its default
            pass
        self._reader = threading.Thread(target=self._take_packets, name="ffmpeg-packets", daemon=True)
        self._reader.start()

    @classmethod
    def open(cls, device_id: str, width=None, height=None, fps=None, pixel_format=None):
        """Open `device_id` asking for a size and rate, or None if FFmpeg cannot.

        DirectShow's formats are filtered by size and rate, so a USB 2.0 card asked for 1080p at
        30 can only be given MJPG: uncompressed 1080p does about five. `pixel_format` asks for an
        uncompressed format by name, for the ladder's YUY2 rungs."""
        import av

        # FFmpeg's DirectShow input queues frames up to `rtbufsize` bytes, 3 MB by default, and
        # once that is 62-100% full it drops the frame that has just *arrived* and keeps the older
        # ones (shall_we_drop in dshow.c) -- the opposite of what a live picture wants. One raw
        # 1080p frame from a USB 3.0 card is 4 MB, more than the whole default. The reader below
        # empties the queue as fast as frames come, so a larger limit costs nothing but memory
        # that is never used, and means the newest frame is the one that survives a stall.
        options = {"rtbufsize": RTBUFSIZE}
        if width and height:
            options["video_size"] = f"{int(width)}x{int(height)}"
        if fps:
            options["framerate"] = str(int(fps))
        if pixel_format:
            options["pixel_format"] = pixel_format
        try:
            container = av.open(f"video={device_id}", format="dshow", options=options)
        except Exception as exc:                    # noqa: BLE001 -- any refusal: try the next format
            LOGGER.info("FFmpeg could not open %s with %s: %s", device_id, options, exc)
            return None
        try:
            # `flags` is a plain int in PyAV; its Flags enum does not combine with one.
            container.flags = int(container.flags) | int(av.container.Flags.non_block.value)
            stream = container.streams.video[0]
        except Exception as exc:                    # noqa: BLE001
            LOGGER.info("FFmpeg opened %s but it has no video stream: %s", device_id, exc)
            container.close()
            return None
        return cls(container, stream)

    # --- the reader thread ---

    def _take_packets(self):
        import av

        while not self._closing:
            try:
                for packet in self._container.demux(self._stream):
                    if self._closing:
                        break
                    if not packet.size:
                        continue                    # an end-of-stream marker, not a frame
                    with self._lock:
                        if self._latest is not None:
                            self.skipped += 1
                        self._latest = packet
                    self._fresh.set()
                else:
                    if not self._closing:
                        self.failed = "the card stopped sending"
                    break
            except av.error.BlockingIOError:
                time.sleep(POLL_SECONDS)
            except Exception as exc:                # noqa: BLE001 -- unplugged, or worse
                self.failed = str(exc) or type(exc).__name__
                break
        self._fresh.set()                           # wake a read waiting on a feed that has ended

    # --- what CaptureWorker calls ---

    def read(self, timeout: float = READ_TIMEOUT):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._fresh.wait(remaining):
                return False, None
            with self._lock:
                packet, self._latest = self._latest, None
                self._fresh.clear()
            if packet is not None:
                break
            # Woken with nothing to take: either the reader has ended, or it set the event for
            # a packet this read had already taken. Only the first is a failure.
            if self.failed is not None or not self._reader.is_alive():
                return False, None
        try:
            frames = self._codec.decode(packet)
        except Exception:                           # noqa: BLE001 -- a corrupt frame: skip it
            return False, None
        if not frames:
            return False, None
        frame = frames[-1]
        self._shape = (frame.width, frame.height)
        return True, frame.to_ndarray(format="bgr24")

    def grab(self):
        return self.failed is None

    def retrieve(self):
        return self.read()

    def isOpened(self):
        return self.failed is None

    def get(self, prop):
        import cv2

        codec = self._codec
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return (self._shape or (codec.width, codec.height))[0]
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return (self._shape or (codec.width, codec.height))[1]
        if prop == cv2.CAP_PROP_FPS:
            rate = self._stream.average_rate or self._stream.base_rate or self._stream.guessed_rate
            return float(rate) if rate else 0.0
        if prop == cv2.CAP_PROP_FOURCC:
            return fourcc_number(self.fourcc)
        return 0.0

    def take_skipped(self) -> int:
        """Frames dropped undecoded since the last call, for the capture loop's count."""
        with self._lock:
            skipped, self.skipped = self.skipped, 0
        return skipped

    @property
    def fourcc(self) -> str:
        """What the card is sending, as a FOURCC: MJPG, or the raw format's."""
        codec = self._codec
        if codec.name in _FOURCC_BY_CODEC:
            return _FOURCC_BY_CODEC[codec.name]
        pixel = getattr(codec, "pix_fmt", None) or ""
        return _FOURCC_BY_PIXEL.get(pixel, pixel.upper() or "?")

    def release(self):
        """Stop the reader first, then close: never close under a thread inside FFmpeg."""
        self._closing = True
        self._reader.join(2.0)
        try:
            self._container.close()
        except Exception:                           # noqa: BLE001 -- closing is best effort
            LOGGER.exception("closing the FFmpeg capture")
