"""Post-processing for what the capture card gives us.

A cheap HDMI grabber hurts the picture in three separate ways, and they want different answers:

**Washed-out or crushed blacks.** The commonest and the most visible. HDMI usually carries limited
range (16-235) while a PC screen expects full range (0-255). A card that mislabels which it is
sending leaves black looking grey, or crushes shadow detail away. `detect_range` measures it
instead of guessing, and a 256-entry lookup table fixes it for about a millisecond a frame.

**Softness and blocking.** A USB 2.0 card has to fit 1080p30 down a 480 Mbit/s pipe, so it sends
MJPEG at a low quality factor. The detail is gone and nothing brings it back, but an unsharp mask
makes what is left read better, especially text.

**Dull colour.** Compression hits chroma hardest. A saturation lift is a blunt instrument but it
does help.

Everything is off by default and costs nothing when off. Order matters: levels first, because the
others should work on corrected tones rather than correct themselves around a cast.

Runs on the capture thread, so the GUI never waits for it. At 1080p30 the budget is 33 ms a frame
and `benchmark` reports what each stage actually spends.
"""

import logging
import time

import cv2
import numpy as np

LOGGER = logging.getLogger("capturekvm.process")

# What limited-range HDMI uses: 16 is black, 235 is white.
LIMITED_BLACK = 16
LIMITED_WHITE = 235
FULL_BLACK = 0
FULL_WHITE = 255

RANGE_NONE = "none"
RANGE_LIMITED_TO_FULL = "limited_to_full"
RANGE_FULL_TO_LIMITED = "full_to_limited"
RANGE_MODES = (RANGE_NONE, RANGE_LIMITED_TO_FULL, RANGE_FULL_TO_LIMITED)

DEFAULTS = {
    "range_mode": RANGE_NONE,
    "brightness": 0,        # -100 to 100, added after the range fix
    "contrast": 1.0,        # 0.5 to 2.0, around mid grey
    "gamma": 1.0,           # 0.4 to 2.5
    "saturation": 1.0,      # 0.0 to 2.0
    "sharpen": 0.0,         # 0.0 to 1.5, unsharp mask amount
    "sharpen_radius": 1.0,  # gaussian sigma in pixels
}


def _clamp(value, low, high):
    return low if value < low else (high if value > high else value)


class Picture:
    """The settings and the pipeline. Rebuilds its lookup table only when something changes."""

    def __init__(self, **settings):
        self.settings = dict(DEFAULTS)
        self.update(**settings)
        self.last_ms = 0.0

    def update(self, **settings) -> None:
        for key, value in settings.items():
            if key in DEFAULTS:
                self.settings[key] = value
        self._lut = self._build_lut()

    @property
    def active(self) -> bool:
        """Whether anything would change. A frame is returned untouched when nothing would."""
        s = self.settings
        return (s["range_mode"] != RANGE_NONE or s["brightness"] != 0 or s["contrast"] != 1.0
                or s["gamma"] != 1.0 or s["saturation"] != 1.0 or s["sharpen"] > 0.0)

    def _build_lut(self):
        """One 256-entry table for range, brightness, contrast and gamma together.

        Folded into a single table on purpose: each as its own pass would be three more trips
        over two million pixels, and composing them costs nothing here."""
        s = self.settings
        x = np.arange(256, dtype=np.float32)

        if s["range_mode"] == RANGE_LIMITED_TO_FULL:
            y = (x - LIMITED_BLACK) * (FULL_WHITE - FULL_BLACK) / (LIMITED_WHITE - LIMITED_BLACK)
        elif s["range_mode"] == RANGE_FULL_TO_LIMITED:
            y = x * (LIMITED_WHITE - LIMITED_BLACK) / 255.0 + LIMITED_BLACK
        else:
            y = x.copy()

        contrast = _clamp(float(s["contrast"]), 0.1, 4.0)
        if contrast != 1.0:
            y = (y - 128.0) * contrast + 128.0

        brightness = _clamp(float(s["brightness"]), -128, 128)
        if brightness:
            y = y + brightness

        gamma = _clamp(float(s["gamma"]), 0.1, 5.0)
        if gamma != 1.0:
            y = 255.0 * np.power(np.clip(y, 0, 255) / 255.0, 1.0 / gamma)

        return np.clip(y, 0, 255).astype(np.uint8)

    def apply(self, frame: np.ndarray) -> np.ndarray:
        """The processed frame, or the same object when nothing is switched on.

        The input belongs to OpenCV and is reused on the next read, so every stage that changes
        anything produces a new array rather than writing through it."""
        if not self.active or frame is None:
            return frame
        started = time.perf_counter()
        s = self.settings

        if self._lut is not None and not np.array_equal(self._lut, np.arange(256, dtype=np.uint8)):
            frame = cv2.LUT(frame, self._lut)

        saturation = _clamp(float(s["saturation"]), 0.0, 3.0)
        if saturation != 1.0:
            # Blend towards luma rather than converting to HSV and back: two colour-space
            # conversions at 1080p cost more than the whole rest of the pipeline.
            grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            grey = cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR)
            frame = cv2.addWeighted(frame, saturation, grey, 1.0 - saturation, 0.0)

        amount = _clamp(float(s["sharpen"]), 0.0, 3.0)
        if amount > 0.0:
            radius = _clamp(float(s["sharpen_radius"]), 0.3, 5.0)
            blurred = cv2.GaussianBlur(frame, (0, 0), radius)
            frame = cv2.addWeighted(frame, 1.0 + amount, blurred, -amount, 0.0)

        self.last_ms = (time.perf_counter() - started) * 1000.0
        return frame


def detect_range(frame: np.ndarray, sample: int = 8) -> dict:
    """Measure where black and white actually sit, so the range can be judged rather than assumed.

    Every `sample`-th row and column is read, which is plenty for a histogram and keeps this off
    the frame budget. The verdict is a suggestion: a genuinely dark scene has no white in it, and
    no measurement can tell that apart from a card clipping the top end."""
    if frame is None or frame.size == 0:
        return {"black": 0, "white": 255, "looks_limited": False, "confident": False,
                "text": "no frame to measure"}
    small = frame[::sample, ::sample]
    grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    black = int(np.percentile(grey, 0.1))
    white = int(np.percentile(grey, 99.9))
    spread = white - black

    # What identifies limited range is a *clamp* near 16 and 235, not merely the absence of
    # anything darker. Ordinary desktop content reaches true black and true white constantly, so
    # a test of "nothing below 12" calls almost everything limited. These windows look for the
    # floor and ceiling themselves, with a few counts of slack for compression noise.
    floor_at_16 = LIMITED_BLACK - 5 <= black <= LIMITED_BLACK + 6
    ceiling_at_235 = LIMITED_WHITE - 6 <= white <= LIMITED_WHITE + 5
    reaches_full = black <= 6 or white >= 249

    looks_limited = floor_at_16 and ceiling_at_235 and not reaches_full
    # Only meaningful when the picture spans a decent range; a dark menu proves nothing either way.
    confident = spread > 80

    if not confident:
        text = (f"black {black}, white {white} -- too little contrast on screen to judge. "
                "Show something with real blacks and whites, then measure again.")
    elif looks_limited:
        text = (f"black {black}, white {white} -- clamped at the limited-range ends. "
                "The card is sending 16-235 and calling it 0-255; expanding it will fix grey blacks.")
    elif reaches_full:
        text = f"black {black}, white {white} -- reaches full range already; leave it alone."
    else:
        text = (f"black {black}, white {white} -- neither clamped at 16/235 nor reaching 0/255. "
                "Probably just the picture; expanding would crush it.")
    return {"black": black, "white": white, "looks_limited": looks_limited,
            "confident": confident, "text": text}


def benchmark(picture: "Picture", width: int = 1920, height: int = 1080, rounds: int = 12) -> dict:
    """What each stage costs on a frame this size, measured rather than guessed.

    Uses noise, which is the worst case for the blur: a flat test card would compress the cache
    behaviour into something unrepresentative."""
    rng = np.random.default_rng(1)
    frame = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)

    def timed(function) -> float:
        function(frame.copy())                              # warm the caches
        started = time.perf_counter()
        for _ in range(rounds):
            function(frame.copy())
        return (time.perf_counter() - started) * 1000.0 / rounds

    lut = picture._build_lut()
    results = {
        "levels_ms": timed(lambda f: cv2.LUT(f, lut)),
        "saturation_ms": timed(lambda f: cv2.addWeighted(
            f, 1.3, cv2.cvtColor(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR), -0.3, 0.0)),
        "sharpen_ms": timed(lambda f: cv2.addWeighted(f, 1.5, cv2.GaussianBlur(f, (0, 0), 1.0), -0.5, 0.0)),
    }
    results["all_ms"] = timed(picture.apply)
    results["frame_budget_ms_at_30fps"] = 1000.0 / 30
    return results
