"""The capture card's sound, played here.

A capture card carries the PC's HDMI audio as a USB audio device, separate from its video. This
plays that input straight to this machine's speakers. **It does not go over the network** -- it
arrives on the same cable as the picture, so the sound stays in step with it and the link carries
only key presses.

PortAudio through `sounddevice` rather than Qt: QtMultimedia lives in PySide6-Addons, which is
about 150 MB for this one feature, and PortAudio gives the thing that actually matters here --
an explicit block size, so the latency is a number you choose rather than one you discover.

One callback copies input frames to output frames in place. Nothing is queued, resampled or
mixed: if the two devices disagree about rate or channel count, the stream is not opened and the
reason is reported, because silently resampling is how audio ends up a quarter-second behind the
video.

Windows' own Sound - Recording - Listen to this device does the same job in the kernel and will
always beat this by a few milliseconds. This exists so it works without the user going to find
that checkbox, and so the app can say when it is not working.
"""

import logging
import threading

LOGGER = logging.getLogger("capturekvm.audio")

# Smaller is tighter but risks dropouts under load. 256 frames at 48 kHz is about 5.3 ms a block.
DEFAULT_BLOCK_FRAMES = 256
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2

# Host APIs worth preferring on Windows, best first. WASAPI is the modern low-latency one;
# WDM-KS goes straight to the driver and is sometimes the only one that sees a capture card.
HOST_API_PREFERENCE = ("Windows WASAPI", "Windows WDM-KS", "Windows DirectSound", "MME")

# What a capture card tends to call itself.
CARD_HINTS = ("usb", "capture", "hdmi", "video", "ms2109", "ms2130", "cam link", "av to usb")


def _sounddevice():
    import sounddevice

    return sounddevice


def available() -> bool:
    try:
        _sounddevice()
        return True
    except Exception:                                      # noqa: BLE001 - absence is not a crash
        LOGGER.info("sounddevice is unavailable", exc_info=True)
        return False


def _api_rank(name: str) -> int:
    for index, candidate in enumerate(HOST_API_PREFERENCE):
        if candidate.lower() in name.lower():
            return index
    return len(HOST_API_PREFERENCE)


def input_devices() -> list:
    """Every device with inputs, best host API first, and cards guessed at from their names.

    `looks_like_a_card` is a hint for ordering and nothing more: a name is not evidence, and the
    user picks in the end."""
    try:
        sounddevice = _sounddevice()
        apis = sounddevice.query_hostapis()
        found = []
        for index, device in enumerate(sounddevice.query_devices()):
            if device["max_input_channels"] < 1:
                continue
            api = apis[device["hostapi"]]["name"]
            name = device["name"]
            found.append({
                "index": index,
                "name": name,
                "api": api,
                "channels": device["max_input_channels"],
                "sample_rate": int(device["default_samplerate"]),
                "looks_like_a_card": any(hint in name.lower() for hint in CARD_HINTS),
            })
        found.sort(key=lambda item: (not item["looks_like_a_card"], _api_rank(item["api"]), item["index"]))
        return found
    except Exception:                                      # noqa: BLE001
        LOGGER.info("could not list audio inputs", exc_info=True)
        return []


def output_devices() -> list:
    try:
        sounddevice = _sounddevice()
        apis = sounddevice.query_hostapis()
        found = []
        for index, device in enumerate(sounddevice.query_devices()):
            if device["max_output_channels"] < 1:
                continue
            found.append({
                "index": index,
                "name": device["name"],
                "api": apis[device["hostapi"]]["name"],
                "channels": device["max_output_channels"],
                "sample_rate": int(device["default_samplerate"]),
            })
        found.sort(key=lambda item: (_api_rank(item["api"]), item["index"]))
        return found
    except Exception:                                      # noqa: BLE001
        LOGGER.info("could not list audio outputs", exc_info=True)
        return []


def default_output():
    try:
        index = _sounddevice().default.device[1]
        return None if index is None or index < 0 else index
    except Exception:                                      # noqa: BLE001
        return None


class Passthrough:
    """The card's input played to an output, on PortAudio's own thread.

    `on_status(dict)` reports opening, the format settled on, dropped blocks and any failure. It
    is called from the audio callback for drops, so it must not block -- a UI should marshal it.
    """

    def __init__(self, on_status=None):
        self._on_status = on_status or (lambda status: None)
        self._stream = None
        self._lock = threading.Lock()
        self.format = None
        self.underruns = 0
        self.overflows = 0
        self._reported_drop_at = 0.0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._stream is not None

    def start(self, input_index: int, output_index=None, sample_rate: int = DEFAULT_SAMPLE_RATE,
              channels: int = DEFAULT_CHANNELS, block_frames: int = DEFAULT_BLOCK_FRAMES) -> bool:
        """Opens the passthrough. False and a status when it cannot be opened."""
        self.stop()
        try:
            sounddevice = _sounddevice()
        except Exception as exc:                            # noqa: BLE001
            self._on_status({"state": "failed", "text": f"sounddevice is unavailable: {exc}"})
            return False

        if output_index is None:
            output_index = default_output()
        if output_index is None:
            self._on_status({"state": "failed", "text": "this machine has no audio output"})
            return False

        rate, count = self._agree(sounddevice, input_index, output_index, sample_rate, channels)
        if rate is None:
            return False

        def callback(indata, outdata, frames, time_info, status):
            # status carries PortAudio's own flags. Counted rather than logged per block: at
            # 5 ms a block a log line each would be its own performance problem.
            if status:
                if status.input_overflow:
                    self.overflows += 1
                if status.output_underflow:
                    self.underruns += 1
            outdata[:] = indata

        try:
            stream = sounddevice.Stream(
                device=(input_index, output_index),
                samplerate=rate,
                channels=count,
                dtype="int16",
                blocksize=block_frames,
                latency="low",
                callback=callback,
            )
            stream.start()
        except Exception as exc:                            # noqa: BLE001 - every PortAudio error
            self._on_status({"state": "failed", "text": f"the card's audio would not open: {exc}"})
            LOGGER.info("audio passthrough failed", exc_info=True)
            return False

        with self._lock:
            self._stream = stream
        self.format = {"sample_rate": rate, "channels": count, "block_frames": block_frames,
                       "latency_ms": round(stream.latency[0] * 1000 + stream.latency[1] * 1000, 1)}
        self._on_status({"state": "open", "format": self.format})
        LOGGER.info("audio passthrough open: %s", self.format)
        return True

    def _agree(self, sounddevice, input_index, output_index, sample_rate, channels):
        """A rate and channel count both devices accept, or (None, None) with a status.

        Nothing is resampled or re-mixed. A card that only offers 44.1 kHz into an output locked
        to 48 kHz is a real mismatch, and saying so beats quietly adding latency to hide it."""
        try:
            source = sounddevice.query_devices(input_index)
            sink = sounddevice.query_devices(output_index)
        except Exception as exc:                            # noqa: BLE001
            self._on_status({"state": "failed", "text": f"that audio device is gone: {exc}"})
            return None, None

        count = min(channels, source["max_input_channels"], sink["max_output_channels"])
        if count < 1:
            self._on_status({"state": "failed", "text": "that input or output has no channels"})
            return None, None

        wanted = [sample_rate, int(source["default_samplerate"]), int(sink["default_samplerate"]),
                  48000, 44100]
        for rate in dict.fromkeys(wanted):
            try:
                sounddevice.check_input_settings(device=input_index, channels=count, dtype="int16", samplerate=rate)
                sounddevice.check_output_settings(device=output_index, channels=count, dtype="int16", samplerate=rate)
            except Exception:                               # noqa: BLE001 - try the next rate
                continue
            return rate, count

        self._on_status({"state": "failed",
                         "text": f"no sample rate suits both {source['name']} and {sink['name']}"})
        return None, None

    def stop(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:                                   # noqa: BLE001
            LOGGER.info("closing the audio stream", exc_info=True)
        self.format = None
        self._on_status({"state": "stopped"})

    def drops(self) -> tuple:
        return self.overflows, self.underruns
