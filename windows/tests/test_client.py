"""Unit tests for the Windows client's own modules.

The end-to-end check covers the link against a real responder, but it says nothing about the
parts that never touch a socket: the picture pipeline, the key and scroll mapping, what the
clipboard will and will not send, and what the beacon listener accepts.

The range detector earns most of this file. Its first version tested "nothing darker than 12",
which calls almost every desktop picture limited-range, and the only reason that was caught was
an ad-hoc script that no longer exists. These cases are that script, kept.

    cd windows && .venv\\Scripts\\python.exe -m unittest discover -s tests
"""

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "beamer-vigem"))
sys.path.insert(0, str(ROOT / "windows"))

import numpy as np  # noqa: E402

from capturekvm import capture, clipboard, discover, ffcapture, launcher, padwire, process, store  # noqa: E402
from core import pairing  # noqa: E402


def ramp(low: int, high: int, height: int = 240, width: int = 320) -> np.ndarray:
    """A luma ramp. Random noise will not do: the luma of random RGB clusters near mid grey, so
    a 'full range' frame made that way is not full range at all -- which is how the first
    version of these tests fooled itself."""
    row = np.linspace(low, high, width, dtype=np.uint8)
    return np.repeat(np.repeat(row[None, :, None], height, 0), 3, 2).copy()


class RangeDetection(unittest.TestCase):
    def verdict(self, frame):
        found = process.detect_range(frame, sample=2)
        return found["looks_limited"] and found["confident"]

    def test_limited_range_is_recognised(self):
        self.assertTrue(self.verdict(ramp(16, 235)))

    def test_limited_range_survives_compression_noise(self):
        noisy = np.clip(ramp(16, 235).astype(int)
                        + np.random.default_rng(0).integers(-3, 4, (240, 320, 3)), 0, 255).astype(np.uint8)
        self.assertTrue(self.verdict(noisy))

    def test_full_range_is_left_alone(self):
        self.assertFalse(self.verdict(ramp(0, 255)))

    def test_an_ordinary_picture_is_not_called_limited(self):
        # The failure the first version had: content that merely never reaches black is not
        # limited range, and expanding it would crush it.
        self.assertFalse(self.verdict(ramp(40, 200)))

    def test_a_dark_frame_refuses_to_judge(self):
        found = process.detect_range(ramp(10, 38), sample=2)
        self.assertFalse(found["confident"])
        self.assertIn("too little contrast", found["text"])

    def test_an_empty_frame_does_not_raise(self):
        self.assertFalse(process.detect_range(None)["confident"])


class Levels(unittest.TestCase):
    def test_expanding_maps_the_limited_ends_to_the_full_ones(self):
        picture = process.Picture(range_mode=process.RANGE_LIMITED_TO_FULL)
        self.assertEqual(int(picture.apply(np.full((2, 2, 3), 16, np.uint8))[0, 0, 0]), 0)
        self.assertEqual(int(picture.apply(np.full((2, 2, 3), 235, np.uint8))[0, 0, 0]), 255)

    def test_compressing_is_the_other_direction(self):
        picture = process.Picture(range_mode=process.RANGE_FULL_TO_LIMITED)
        self.assertEqual(int(picture.apply(np.zeros((2, 2, 3), np.uint8))[0, 0, 0]), 16)
        self.assertEqual(int(picture.apply(np.full((2, 2, 3), 255, np.uint8))[0, 0, 0]), 235)

    def test_defaults_change_nothing_and_cost_nothing(self):
        picture = process.Picture()
        self.assertFalse(picture.active)
        frame = np.zeros((4, 4, 3), np.uint8)
        # The same object, not an equal one: an untouched frame must not be copied.
        self.assertIs(picture.apply(frame), frame)

    def test_values_are_clamped_rather_than_wrapped(self):
        picture = process.Picture(brightness=100)
        out = picture.apply(np.full((2, 2, 3), 250, np.uint8))
        self.assertEqual(int(out[0, 0, 0]), 255)
        picture.update(brightness=-100)
        self.assertEqual(int(picture.apply(np.full((2, 2, 3), 5, np.uint8))[0, 0, 0]), 0)

    def test_every_stage_turns_itself_on(self):
        for key, value in (("saturation", 1.4), ("sharpen", 0.5), ("gamma", 1.6), ("contrast", 1.2)):
            with self.subTest(key):
                self.assertTrue(process.Picture(**{key: value}).active)


class ClipboardRules(unittest.TestCase):
    def test_a_peer_with_no_clipboard_gets_nothing(self):
        self.assertIsNone(clipboard.message_for(text="hi", png=b"\x89PNG", peer_caps=()))

    def test_a_text_only_peer_is_sent_no_image(self):
        message = clipboard.message_for(text="hi", png=b"\x89PNG", peer_caps=("clipboard",))
        self.assertEqual(message["data"].get("text"), "hi")
        self.assertNotIn("image", message["data"])

    def test_a_peer_that_takes_images_gets_both(self):
        png = bytes.fromhex("89504e470d0a1a0a")
        message = clipboard.message_for(text="hi", png=png,
                                        peer_caps=("clipboard", "clipboard_image"))
        self.assertIn("text", message["data"])
        self.assertIn("image", message["data"])

    def test_image_only_still_sends(self):
        png = bytes.fromhex("89504e470d0a1a0a")
        self.assertIsNotNone(clipboard.message_for(png=png, peer_caps=("clipboard_image",)))

    def test_describe_says_what_is_there(self):
        self.assertEqual(clipboard.describe(), "nothing")
        self.assertIn("characters", clipboard.describe(text="abc"))
        self.assertIn("image", clipboard.describe(png=b"x" * 2048))


class BeaconListener(unittest.TestCase):
    def setUp(self):
        self.discovery = discover.Discovery()

    def beacon(self, **fields):
        message = {"type": "beacon", "name": "Desk PC", "port": 24820, "pairing": 3}
        message.update(fields)
        return pairing.encode(message)

    def test_a_good_beacon_is_taken(self):
        self.assertTrue(self.discovery._take(self.beacon(pair="abc"), "192.168.1.5"))
        found = self.discovery.machines()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "Desk PC")
        self.assertEqual(found[0]["address"], "192.168.1.5")
        self.assertTrue(found[0]["showing_code"])

    def test_a_beacon_without_a_code_is_marked_as_such(self):
        # Beamer broadcasts only while a code is up, but a listener should not assume it.
        self.discovery._take(self.beacon(), "192.168.1.6")
        self.assertFalse(self.discovery.machines()[0]["showing_code"])

    def test_junk_on_the_port_is_ignored(self):
        for data in (b"", b"not json", b"{}", b'{"beamy":1}', b"x" * 4096):
            with self.subTest(data[:12]):
                self.assertFalse(self.discovery._take(data, "192.168.1.7"))
        self.assertEqual(self.discovery.machines(), [])

    def test_a_malformed_name_or_port_is_refused(self):
        self.assertFalse(self.discovery._take(self.beacon(name=""), "10.0.0.1"))
        self.assertFalse(self.discovery._take(self.beacon(name="x" * 200), "10.0.0.2"))
        self.assertFalse(self.discovery._take(self.beacon(port=0), "10.0.0.3"))
        self.assertFalse(self.discovery._take(self.beacon(port="24820"), "10.0.0.4"))
        self.assertEqual(self.discovery.machines(), [])

    def test_one_address_is_one_machine(self):
        self.discovery._take(self.beacon(name="First"), "192.168.1.8")
        self.discovery._take(self.beacon(name="Second"), "192.168.1.8")
        found = self.discovery.machines()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "Second")


class PadState(unittest.TestCase):
    def test_a_reading_compares_and_copies_by_value(self):
        one = padwire.PadState(buttons=padwire.BTN_A, lt=12)
        self.assertEqual(one, one.copy())
        two = one.copy()
        two.lt = 13
        self.assertNotEqual(one, two)

    def test_clear_makes_it_neutral(self):
        state = padwire.PadState(buttons=padwire.BTN_B, rx=-20000)
        state.clear()
        self.assertTrue(state.is_neutral())

    def test_a_report_is_built_by_the_shared_core_and_clamped(self):
        data = padwire.report(padwire.PadState(lx=-99999, rt=999), 0, seq=4)["data"]
        self.assertEqual(data["lx"], padwire.AXIS_MIN)
        self.assertEqual(data["rt"], padwire.TRIGGER_MAX)
        self.assertEqual(data["seq"], 4)


class PadCoalescing(unittest.TestCase):
    """A queued pad report must be replaced, not queued behind.

    A report is full state. Sending a backlog of them in turn replays stick positions the player
    has already left, which is lag that persists instead of a frame that is merely late -- the
    worst kind in a game, and the reason mouse moves have been coalesced all along.
    """

    def link(self):
        from capturekvm.link import BeamerLink
        peer = {"id": "AAECAwQFBgcICQoLDA0ODw", "token": "A" * 43, "host": "127.0.0.1",
                "port": 24820, "name": "PC", "platform": "windows"}
        made = BeamerLink(peer, bytes(range(1, 17)), "client")
        made.state = "up"
        made.peer_caps = (padwire.CAP_GAMEPAD,)
        return made

    def report(self, buttons, pad=0):
        return padwire.report(padwire.PadState(buttons=buttons), pad)

    def test_a_backlog_collapses_to_the_newest(self):
        made = self.link()
        for buttons in (padwire.BTN_A, padwire.BTN_B, padwire.BTN_X, padwire.BTN_Y):
            made.send_pad(self.report(buttons))
        self.assertEqual(len(made._outbox), 1)
        self.assertEqual(made._outbox[0]["data"]["buttons"], padwire.BTN_Y)
        self.assertEqual(made.pad_coalesced, 3)

    def test_different_pads_do_not_collapse_into_each_other(self):
        made = self.link()
        made.send_pad(self.report(padwire.BTN_A, pad=0))
        made.send_pad(self.report(padwire.BTN_B, pad=1))
        self.assertEqual(len(made._outbox), 2)

    def test_a_peer_without_the_capability_is_sent_nothing(self):
        made = self.link()
        made.peer_caps = ()
        made.send_pad(self.report(padwire.BTN_A))
        self.assertEqual(len(made._outbox), 0)

    def test_other_messages_are_not_disturbed(self):
        from core import protocol
        made = self.link()
        made.send_unsequenced(protocol.ping_msg(probe=1))
        made.send_pad(self.report(padwire.BTN_A))
        made.send_pad(self.report(padwire.BTN_B))
        kinds = [m["type"] for m in made._outbox]
        self.assertEqual(kinds, ["ping", "gamepad"])


class Fourcc(unittest.TestCase):
    def test_a_fourcc_survives_the_round_trip(self):
        for text in ("MJPG", "YUY2", "NV12"):
            with self.subTest(text):
                self.assertEqual(capture.fourcc_text(capture.fourcc_code(text)), text)

    def test_nonsense_reads_as_unknown(self):
        self.assertEqual(capture.fourcc_text(0), "?")
        self.assertEqual(capture.fourcc_text(None), "?")


class ModeResolution(unittest.TestCase):
    """One exe is both ends of the link, and `resolve` is the whole of how it decides which.

    Worth pinning because two of these rules are not obvious from the UI. Beamer relaunches
    itself for administrator mode and runs itself from the sign-in task as plain
    `exe --hidden --relaunched`, with no --mode: if those stopped landing in host mode they
    would meet the picker instead, at sign-in, with nobody looking. And an explicit --mode has
    to beat that rule, or `--mode receiver` could never be tried on a machine set up as a host.
    """

    def resolve(self, *arguments, remembered=""):
        return launcher.resolve(["CaptureKVM.exe", *arguments], remembered)

    def test_nothing_known_asks(self):
        self.assertEqual(self.resolve()[0], None)

    def test_a_remembered_mode_is_used(self):
        self.assertEqual(self.resolve(remembered="host")[0], launcher.HOST)
        self.assertEqual(self.resolve(remembered="receiver")[0], launcher.RECEIVER)

    def test_a_remembered_mode_that_is_not_one_asks(self):
        self.assertEqual(self.resolve(remembered="sideways")[0], None)

    def test_the_flag_beats_what_is_remembered(self):
        self.assertEqual(self.resolve("--mode", "host", remembered="receiver")[0], launcher.HOST)
        self.assertEqual(self.resolve("--mode=receiver", remembered="host")[0], launcher.RECEIVER)

    def test_pick_asks_again_even_with_a_mode_remembered(self):
        mode, _, pick = self.resolve("--pick", remembered="host")
        self.assertIsNone(mode)
        self.assertTrue(pick)

    def test_beamers_own_launches_find_host_mode(self):
        for arguments in (("--hidden",), ("--hidden", "--relaunched"),
                          ("--config", r"C:\settings.json"), (r"--config=C:\settings.json",),
                          ("--import-probe", "out.txt", "core"), ("--enable-autostart",),
                          ("--uninstall-cleanup",), ("--set-admin-mode", "on")):
            with self.subTest(arguments):
                self.assertEqual(self.resolve(*arguments)[0], launcher.HOST)

    def test_an_explicit_mode_beats_beamers_flags(self):
        mode, argv, _ = self.resolve("--mode", "receiver", "--hidden")
        self.assertEqual(mode, launcher.RECEIVER)
        self.assertEqual(argv, ["CaptureKVM.exe", "--hidden"])

    def test_our_flags_do_not_reach_the_mode_that_runs(self):
        # Beamer parses its argv with argparse, which exits on an argument it does not know, and
        # Qt reads argv too. Anything of ours left in there would take the app down with it.
        _, argv, _ = self.resolve("--pick", "--mode", "host", "--hidden", "--config", "s.json")
        self.assertEqual(argv, ["CaptureKVM.exe", "--hidden", "--config", "s.json"])

    def test_a_mode_that_is_not_one_is_refused(self):
        for arguments in (("--mode", "sideways"), ("--mode",), ("--mode=",)):
            with self.subTest(arguments):
                with self.assertRaises(ValueError):
                    self.resolve(*arguments)


class RememberedMode(unittest.TestCase):
    def setUp(self):
        self.file = Path(tempfile.mkdtemp()) / "config.json"
        self.store = store.Store(self.file)

    def tearDown(self):
        shutil.rmtree(self.file.parent, ignore_errors=True)

    def test_unset_means_ask(self):
        self.assertEqual(self.store.mode, "")

    def test_a_directshow_chosen_before_ffmpeg_existed_moves_to_auto_once(self):
        self.store.set_setting("capture_api", "dshow")
        self.assertTrue(store.Store(self.file).adopt_ffmpeg())
        again = store.Store(self.file)
        self.assertEqual(again.setting("capture_api"), "auto")
        again.set_setting("capture_api", "dshow")          # chosen now, knowing FFmpeg is there
        self.assertFalse(store.Store(self.file).adopt_ffmpeg())
        self.assertEqual(store.Store(self.file).setting("capture_api"), "dshow")

    def test_other_choices_are_left_alone(self):
        self.store.set_setting("capture_api", "msmf")
        self.assertFalse(self.store.adopt_ffmpeg())
        self.assertEqual(self.store.setting("capture_api"), "msmf")

    def test_a_mode_survives_a_reload(self):
        self.store.mode = "host"
        self.assertEqual(store.Store(self.file).mode, "host")

    def test_clearing_it_goes_back_to_asking(self):
        self.store.mode = "receiver"
        self.store.mode = ""
        self.assertEqual(store.Store(self.file).mode, "")

    def test_junk_in_the_file_is_not_trusted(self):
        # Hand-edited or written by an older version: anything but the two names means ask.
        self.file.write_text(json.dumps({"mode": "administrator"}), encoding="utf-8")
        self.assertEqual(store.Store(self.file).mode, "")

    def test_junk_cannot_be_stored(self):
        self.store.mode = "sideways"
        self.assertEqual(self.store.mode, "")

    # --set-mode: how the installer records the answer to its "which end is this?" page.

    def set_mode(self, *arguments):
        return launcher.set_mode(["CaptureKVM.exe", *arguments], self.store)

    def test_the_installer_can_remember_host(self):
        self.assertEqual(self.set_mode("--set-mode", "host"), 0)
        self.assertEqual(store.Store(self.file).mode, "host")

    def test_the_installer_can_remember_receiver(self):
        self.assertEqual(self.set_mode("--set-mode=receiver"), 0)
        self.assertEqual(store.Store(self.file).mode, "receiver")

    def test_ask_clears_what_was_remembered(self):
        # "Decide when it first starts" on an upgrade over a machine that had answered before.
        self.store.mode = "host"
        self.assertEqual(self.set_mode("--set-mode", "ask"), 0)
        self.assertEqual(store.Store(self.file).mode, "")

    def test_set_mode_refuses_what_is_not_a_mode(self):
        self.store.mode = "receiver"
        for arguments in (("--set-mode", "sideways"), ("--set-mode",)):
            with self.subTest(arguments):
                self.assertEqual(self.set_mode(*arguments), 2)
        self.assertEqual(store.Store(self.file).mode, "receiver", "a refused value changes nothing")

    def test_set_mode_starts_nothing(self):
        # The installer waits on this process; it must not open a picker or a mode's window.
        with unittest.mock.patch.object(launcher, "ask") as ask, \
                unittest.mock.patch.object(launcher, "run") as run, \
                unittest.mock.patch.object(store, "Store", return_value=self.store):
            code = launcher.main(["CaptureKVM.exe", "--set-mode", "host"])
        self.assertEqual(code, 0)
        ask.assert_not_called()
        run.assert_not_called()
        self.assertEqual(self.store.mode, "host")


DSHOW = capture.cv2.CAP_DSHOW
MSMF = capture.cv2.CAP_MSMF
FFMPEG = capture.FFMPEG


class FakeCamera:
    """cv2.VideoCapture, for a PC described by `world`: (index, api) -> "reads", "opens", or
    absent. "opens" is the failure that matters most for finding cards: a device the API accepts
    and then never gets a frame from. It reports MJPG 1080p, so a DirectShow open that checks the
    format it got back is satisfied."""

    world = {}
    asked = []

    def __init__(self, index, api):
        self.key = (index, api)
        FakeCamera.asked.append(self.key)
        self.state = FakeCamera.world.get(self.key)

    def isOpened(self):
        return self.state in ("opens", "reads")

    def read(self):
        if self.state == "reads":
            return True, np.zeros((4, 4, 3), np.uint8)
        return False, None

    def grab(self):
        return self.state == "reads"

    def retrieve(self):
        return self.read()

    def get(self, prop):
        cv2 = capture.cv2
        return {cv2.CAP_PROP_FRAME_WIDTH: 1920, cv2.CAP_PROP_FRAME_HEIGHT: 1080,
                cv2.CAP_PROP_FPS: 30.0, cv2.CAP_PROP_FOURCC: capture.fourcc_code("MJPG")}.get(prop, 0)

    def set(self, prop, value):
        return True

    def release(self):
        pass


class CardDiscovery(unittest.TestCase):
    """Finding the card, which the first version could not do for a card every other app saw.

    It probed through Media Foundation alone. Most capture software -- OBS, VLC, the makers' own
    tools -- uses DirectShow, and cheap HDMI grabbers are exactly the devices Media Foundation
    tends to refuse or open without ever delivering a frame. None of that can be reproduced
    without the hardware, so the hardware is described instead and the decisions are checked.
    """

    def look(self, world, **kwargs):
        FakeCamera.world = world
        FakeCamera.asked = []
        with unittest.mock.patch.object(capture.cv2, "VideoCapture", FakeCamera), \
                unittest.mock.patch.object(capture.ffcapture, "list_video_devices", lambda: []):
            return capture.list_devices(**kwargs)

    def test_a_card_media_foundation_will_not_open_is_found_through_directshow(self):
        found = self.look({(0, DSHOW): "reads"})
        self.assertEqual([(d["index"], d["backend"], d["reads"]) for d in found],
                         [(0, "DirectShow", True)])

    def test_a_card_that_sends_nothing_through_one_api_is_taken_through_the_other(self):
        found = self.look({(0, DSHOW): "opens", (0, MSMF): "reads"})
        self.assertEqual([(d["backend"], d["reads"]) for d in found], [("Media Foundation", True)])

    def test_directshow_is_preferred_when_both_work(self):
        found = self.look({(0, DSHOW): "reads", (0, MSMF): "reads"})
        self.assertEqual(found[0]["backend"], "DirectShow")
        self.assertNotIn((0, MSMF), FakeCamera.asked, "a card already reading needs no second API")

    def test_a_card_neither_api_can_read_is_still_listed(self):
        # Better listed as "no picture yet" than missing: no picture is a cable or HDMI-source
        # question, a missing card is a driver question, and they need different answers.
        found = self.look({(0, MSMF): "opens"})
        self.assertEqual([(d["backend"], d["reads"]) for d in found], [("Media Foundation", False)])

    def test_a_card_held_by_another_app_does_not_hide_the_next_one(self):
        # Camera 0 is busy, so it opens under neither API; the card is camera 1.
        found = self.look({(1, DSHOW): "reads"})
        self.assertEqual([d["index"] for d in found], [1])

    def test_the_scan_stops_at_the_end_of_the_list(self):
        self.look({(0, DSHOW): "reads"})
        self.assertEqual(sorted({index for index, _ in FakeCamera.asked}), [0, 1, 2],
                         "two empty indices are the end of Windows's list")

    def negotiate(self, world, api):
        FakeCamera.world = world
        with unittest.mock.patch.object(capture.cv2, "VideoCapture", FakeCamera), \
                unittest.mock.patch.object(capture.ffcapture, "list_video_devices", lambda: []):
            return capture.negotiate(1, ("MJPG", 1920, 1080, 30), api)

    def test_starting_a_card_keeps_to_the_api_it_was_found_through(self):
        # Index 1 through the other API is not guaranteed to be the same device; on a PC with a
        # webcam it can be the webcam. Better to fail and say so than to show the wrong picture.
        capture_, actual = self.negotiate({(1, DSHOW): "opens", (1, MSMF): "reads"}, DSHOW)
        self.assertIsNone(capture_)
        self.assertIsNone(actual)

    def test_a_setting_from_before_the_choice_tries_both(self):
        capture_, actual = self.negotiate({(1, DSHOW): "opens", (1, MSMF): "reads"}, None)
        self.assertIsNotNone(capture_)
        self.assertEqual((actual["backend"], actual["api"]), ("Media Foundation", MSMF))


class FakeDirectShow:
    """OpenCV's DirectShow capture as far as formats go, transcribed from
    modules/videoio/src/cap_dshow.cpp (OpenCV 4.x), so the order of settings can be tried without
    a card.

    The parts that matter, from the source:
      * setProperty(FRAME_WIDTH / FRAME_HEIGHT) records the value; once both are known, and the
        size *or the FOURCC* differs from the device's, the device is set up again with
        setupDeviceFourcc(m_width, m_height, m_fourcc). Afterwards m_fourcc goes back to -1.
      * setProperty(FOURCC) records it and takes m_width/m_height from the current size, so it
        sets the device up at once.
      * setProperty(FPS) remembers the rate (setIdealFramerate) and sets the device up again with
        setupDevice(m_index, w, h) -- no FOURCC.
      * A set-up with no FOURCC asks for RGB24 and, refused, takes the first type in videoInput's
        list that the device offers at that size: RGB24, RGB32, RGB555, RGB565, YUY2, ... MJPG.
      * getFPS() returns the rate *asked for*; getFourcc() the type actually set up.
    """

    TYPES = ("RGB24", "RGB32", "RGB555", "RGB565", "YUY2", "YVYU", "YUYV", "IYUV", "UYVY", "YV12",
             "YVU9", "Y411", "Y41P", "Y211", "AYUV", "MJPG")
    # An MS2109-class card: YUY2 at every size, but over USB 2.0 only MJPG keeps 1080p at speed.
    OFFERS = {("YUY2", 640, 480), ("YUY2", 1280, 720), ("YUY2", 1920, 1080),
              ("MJPG", 640, 480), ("MJPG", 1280, 720), ("MJPG", 1920, 1080)}

    def __init__(self, index=0, api=None, offers=None, start=("YUY2", 640, 480)):
        self.offers = set(offers or self.OFFERS)
        self.video_type, self.width, self.height = start
        self.frame_time = -1.0                       # videoInput's requestedFrameTime
        self.m_width = self.m_height = -1
        self.m_fourcc = -1
        self.m_width_set = self.m_height_set = -1

    def fps(self):
        return 1e7 / self.frame_time if self.frame_time > 0 else 0

    def _setup(self, width, height, fourcc):
        tried = fourcc if fourcc != -1 else "RGB24"
        if (tried, width, height) in self.offers:
            self.video_type = tried
        else:
            self.video_type = next(kind for kind in self.TYPES if (kind, width, height) in self.offers)
        self.width, self.height = width, height

    def set(self, prop, value):
        cv2 = capture.cv2
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            self.m_width = round(value)
        elif prop == cv2.CAP_PROP_FRAME_HEIGHT:
            self.m_height = round(value)
        elif prop == cv2.CAP_PROP_FOURCC:
            self.m_fourcc = capture.fourcc_text(value)
            self.m_width, self.m_height = self.width, self.height
        elif prop == cv2.CAP_PROP_FPS:
            fps = round(value)
            if fps != self.fps():
                self.frame_time = 1e7 / fps
                if self.m_width_set > 0 and self.m_height_set > 0:
                    self._setup(self.m_width_set, self.m_height_set, -1)
                else:
                    self._setup(self.width, self.height, -1)
            return True
        else:
            return False
        if self.m_width > 0 and self.m_height > 0:
            if (self.m_width, self.m_height) != (self.width, self.height) or self.m_fourcc != self.video_type:
                self._setup(self.m_width, self.m_height, self.m_fourcc)
            self.m_width_set, self.m_height_set = self.m_width, self.m_height
            self.m_width = self.m_height = self.m_fourcc = -1
        return True

    def get(self, prop):
        cv2 = capture.cv2
        return {cv2.CAP_PROP_FRAME_WIDTH: self.width, cv2.CAP_PROP_FRAME_HEIGHT: self.height,
                cv2.CAP_PROP_FPS: self.fps(),
                cv2.CAP_PROP_FOURCC: capture.fourcc_code(self.video_type)}.get(prop, 0)

    def isOpened(self):
        return True

    def read(self):
        return True, np.zeros((self.height, self.width, 3), np.uint8)

    def release(self):
        pass


class DirectShowFormatOrder(unittest.TestCase):
    """Why 0.4.1 and 0.4.2 crawled on DirectShow, and that they no longer do.

    A model of OpenCV's own code is the next best thing to the card, and the model is only worth
    trusting if it reproduces the failure: so the first test shows the old order landing on YUY2.
    """

    def configure(self, order, **device):
        card = FakeDirectShow(**device)
        cv2 = capture.cv2
        steps = {"fourcc": (cv2.CAP_PROP_FOURCC, capture.fourcc_code("MJPG")),
                 "width": (cv2.CAP_PROP_FRAME_WIDTH, 1920), "height": (cv2.CAP_PROP_FRAME_HEIGHT, 1080),
                 "fps": (cv2.CAP_PROP_FPS, 30)}
        for name in order:
            card.set(*steps[name])
        return card

    def test_the_old_order_left_the_card_on_yuy2(self):
        card = self.configure(("fourcc", "width", "height", "fps"))
        self.assertEqual((card.video_type, card.width, card.height), ("YUY2", 1920, 1080))
        self.assertEqual(round(card.fps()), 30, "and went on reporting the 30 it was asked for")

    def test_this_order_gets_mjpg_at_1080p_and_30(self):
        card = FakeDirectShow()
        capture._configure(card, DSHOW, "MJPG", 1920, 1080, 30)
        self.assertEqual((card.video_type, card.width, card.height, round(card.fps())),
                         ("MJPG", 1920, 1080, 30))

    def test_it_holds_when_the_card_starts_out_at_1080p(self):
        # A card already at the size asked for skips the size step's set-up entirely.
        card = FakeDirectShow(start=("YUY2", 1920, 1080))
        capture._configure(card, DSHOW, "MJPG", 1920, 1080, 30)
        self.assertEqual((card.video_type, card.width, card.height), ("MJPG", 1920, 1080))

    def test_media_foundation_keeps_its_own_order(self):
        calls = []
        recorder = unittest.mock.Mock(set=lambda prop, value: calls.append(prop) or True)
        capture._configure(recorder, MSMF, "MJPG", 1920, 1080, 30)
        cv2 = capture.cv2
        self.assertEqual(calls[:4], [cv2.CAP_PROP_FOURCC, cv2.CAP_PROP_FRAME_WIDTH,
                                     cv2.CAP_PROP_FRAME_HEIGHT, cv2.CAP_PROP_FPS])

    def test_a_card_that_cannot_do_mjpg_at_1080p_moves_down_the_ladder(self):
        # Refusing the quiet downgrade is what keeps a slideshow off the screen: rather than YUY2
        # at 1080p, the next rung -- MJPG at 720p -- is tried and taken.
        offers = FakeDirectShow.OFFERS - {("MJPG", 1920, 1080)}
        with unittest.mock.patch.object(capture.cv2, "VideoCapture",
                                        lambda index, api: FakeDirectShow(offers=offers)):
            capture_, actual = capture.negotiate(0, ("MJPG", 1920, 1080, 30), DSHOW)
        self.assertEqual((actual["fourcc"], actual["width"], actual["height"]), ("MJPG", 1280, 720))

    def test_the_api_choice_narrows_the_search(self):
        self.assertEqual([api for _, api in capture.backends_for("dshow")], [DSHOW])
        self.assertEqual([api for _, api in capture.backends_for("msmf")], [MSMF])
        self.assertEqual([api for _, api in capture.backends_for("ffmpeg")], [FFMPEG])
        self.assertEqual([api for _, api in capture.backends_for("auto")], [FFMPEG, DSHOW, MSMF])
        self.assertEqual(capture.backends_for("something old"), capture.BACKENDS)


class FFmpegDeviceList(unittest.TestCase):
    """FFmpeg names the cards; these are the exact pieces libavdevice/dshow.c prints them in."""

    @staticmethod
    def printed(*devices):
        # dshow.c, listing devices: the name, " (" and the media types, ")", a newline, then the
        # alternative name on a line of its own -- five separate av_log calls per device.
        pieces = []
        for name, kinds, unique in devices:
            pieces.append('"%s"' % name)
            pieces.extend([" (%s" % kinds[0]] + [", %s" % kind for kind in kinds[1:]] + [")"]
                          if kinds else [" (none)"])
            pieces.append("\n")
            pieces.append('  Alternative name "%s"\n' % unique)
        return "".join(pieces)

    def test_video_devices_come_out_in_order_with_their_unique_names(self):
        text = self.printed(("USB3.0 Capture", ["video"], "@device_pnp_\\\\?\\usb#vid_345f&pid_2130#a"),
                            ("Integrated Camera", ["video"], "@device_pnp_\\\\?\\usb#vid_04f2#b"),
                            ("Microphone (USB3.0 Capture)", ["audio"], "@device_cm_{33D9}\\wave_{1}"))
        found = ffcapture.parse_device_list(text)
        self.assertEqual([device["name"] for device in found], ["USB3.0 Capture", "Integrated Camera"])
        self.assertTrue(found[0]["id"].startswith("@device_pnp_"), "opened by the name that is unique")

    def test_two_cards_with_one_name_stay_two_cards(self):
        text = self.printed(("USB Video", ["video"], "@device_pnp_one"), ("USB Video", ["video"], "@device_pnp_two"))
        self.assertEqual([device["id"] for device in ffcapture.parse_device_list(text)],
                         ["@device_pnp_one", "@device_pnp_two"])

    def test_a_device_with_video_among_other_media_counts(self):
        text = self.printed(("Cam Link", ["audio", "video"], "@x"), ("Dead thing", [], "@y"))
        self.assertEqual([device["name"] for device in ffcapture.parse_device_list(text)], ["Cam Link"])

    def test_this_pc_can_be_asked_without_failing(self):
        # A real call into FFmpeg: whatever this PC has, listing returns a list and never raises.
        self.assertIsInstance(ffcapture.list_video_devices(), list)


class FakePacket:
    def __init__(self, number, size=1000):
        self.number, self.size = number, size


class FakeFrame:
    width, height = 4, 2

    def __init__(self, number):
        self.number = number

    def to_ndarray(self, format):
        assert format == "bgr24"
        return np.full((self.height, self.width, 3), self.number % 256, np.uint8)


class FakeCodec:
    name, pix_fmt, width, height, thread_type = "mjpeg", "yuvj422p", 1920, 1080, "FRAME"

    def __init__(self):
        self.decoded = []

    def decode(self, packet):
        self.decoded.append(packet.number)
        return [FakeFrame(packet.number)]


class FakeStream:
    average_rate = base_rate = guessed_rate = 30

    def __init__(self):
        self.codec_context = FakeCodec()


class FakeDshowInput:
    """FFmpeg's DirectShow input in non-blocking mode, as dshow_read_packet behaves: it hands over
    what the card has sent and raises EAGAIN, at once, when nothing is waiting. `unplug` makes the
    next read fail the way a removed device does."""

    def __init__(self):
        self.lock = threading.Lock()
        self.arrived = []
        self.reading = 0
        self.closed = False
        self.closed_while_reading = False
        self.unplugged = False
        self.flags = 0

    def deliver(self, *numbers, size=1000):
        with self.lock:
            self.arrived.extend(FakePacket(number, size) for number in numbers)

    def demux(self, stream):
        import av
        self.reading += 1
        try:
            while True:
                with self.lock:
                    if self.closed:
                        raise AssertionError("read after close")
                    if self.unplugged:
                        raise OSError(5, "I/O error")
                    packet = self.arrived.pop(0) if self.arrived else None
                if packet is None:
                    raise av.error.BlockingIOError(11, "Resource temporarily unavailable")
                yield packet
        finally:
            self.reading -= 1

    def close(self):
        self.closed_while_reading = self.reading > 0
        self.closed = True


def wait_until(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.002)


class FFmpegNewestFrameOnly(unittest.TestCase):
    """The point of the FFmpeg path: a frame that is already old is never decoded or shown, and
    nothing in it can wait forever or close under a thread that is still reading."""

    def feed(self):
        source = FakeDshowInput()
        feed = ffcapture.FFmpegFeed(source, FakeStream())
        self.addCleanup(feed.release)
        return source, feed

    def test_only_the_newest_of_queued_frames_is_decoded(self):
        source, feed = self.feed()
        source.deliver(1, 2, 3, 4, 5)
        wait_until(lambda: feed.skipped == 4)
        ok, frame = feed.read()
        self.assertTrue(ok)
        self.assertEqual(int(frame[0, 0, 0]), 5)
        self.assertEqual(feed._codec.decoded, [5], "frames 1-4 were never decoded")
        self.assertEqual(feed.take_skipped(), 4)
        self.assertEqual(feed.take_skipped(), 0, "and the count starts again")

    def test_a_frame_is_shown_once(self):
        source, feed = self.feed()
        source.deliver(7)
        self.assertTrue(feed.read()[0])
        self.assertEqual(feed.read(timeout=0.05), (False, None), "nothing new: no frame, not the old one")

    def test_a_quiet_card_times_out_instead_of_hanging(self):
        _, feed = self.feed()
        started = time.monotonic()
        self.assertEqual(feed.read(timeout=0.1), (False, None))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_an_unplugged_card_fails_at_once(self):
        source, feed = self.feed()
        source.unplugged = True
        wait_until(lambda: feed.failed is not None)
        started = time.monotonic()
        self.assertEqual(feed.read(timeout=5), (False, None))
        self.assertLess(time.monotonic() - started, 1.0, "a dead feed does not wait out the timeout")
        self.assertFalse(feed.isOpened())

    def test_end_of_stream_markers_are_not_frames(self):
        source, feed = self.feed()
        source.deliver(1, size=0)
        source.deliver(2)
        ok, frame = feed.read()
        self.assertEqual(int(frame[0, 0, 0]), 2)
        self.assertEqual(feed._codec.decoded, [2])

    def test_release_stops_the_reader_before_closing(self):
        source, feed = self.feed()
        source.deliver(1)
        wait_until(lambda: feed._latest is not None)
        feed.release()
        self.assertTrue(source.closed)
        self.assertFalse(source.closed_while_reading)
        self.assertFalse(feed._reader.is_alive())

    def test_frames_are_decoded_one_in_one_out(self):
        _, feed = self.feed()
        self.assertEqual(feed._codec.thread_type, "SLICE", "frame threads would hand back old frames")

    def test_it_reports_itself_as_opencv_would(self):
        _, feed = self.feed()
        cv2 = capture.cv2
        self.assertEqual(capture.fourcc_text(feed.get(cv2.CAP_PROP_FOURCC)), "MJPG")
        self.assertEqual((feed.get(cv2.CAP_PROP_FRAME_WIDTH), feed.get(cv2.CAP_PROP_FRAME_HEIGHT)), (1920, 1080))
        self.assertEqual(feed.get(cv2.CAP_PROP_FPS), 30.0)


class FFmpegRealDecode(unittest.TestCase):
    """The same feed over a real FFmpeg demuxer and decoder: MJPG made here, read back as BGR."""

    def recording(self):
        """A few MJPG frames, blue on the left and red on the right, in a file FFmpeg can read."""
        import av
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = folder / "card.avi"
        picture = np.zeros((48, 64, 3), np.uint8)
        picture[:, :32] = (255, 0, 0)          # BGR: blue on the left
        picture[:, 32:] = (0, 0, 255)          # red on the right
        with av.open(str(path), "w") as out:
            stream = out.add_stream("mjpeg", rate=30)
            stream.width, stream.height, stream.pix_fmt = 64, 48, "yuvj420p"
            for _ in range(6):
                frame = av.VideoFrame.from_ndarray(picture, format="bgr24").reformat(format="yuvj420p")
                for packet in stream.encode(frame):
                    out.mux(packet)
            for packet in stream.encode(None):
                out.mux(packet)
        return path

    def test_open_asks_directshow_for_the_format_and_reads_without_blocking(self):
        # FFmpegFeed.open, all of it, with only av.open pointed at the file instead of a card.
        # Setting the non-blocking flag the obvious way (`flags |= Flags.non_block`) raises in
        # PyAV 19, and open() read that as "no video stream": no card would ever have opened.
        import av
        path, calls, real_open = self.recording(), [], av.open

        def card(url, format=None, options=None):
            calls.append((url, format, dict(options or {})))
            return real_open(str(path))

        with unittest.mock.patch.object(av, "open", card):
            feed = ffcapture.FFmpegFeed.open("@device_pnp_card", 1920, 1080, 30)
        self.assertIsNotNone(feed)
        self.addCleanup(feed.release)
        self.assertEqual(calls, [("video=@device_pnp_card", "dshow",
                                  {"video_size": "1920x1080", "framerate": "30",
                                   "rtbufsize": ffcapture.RTBUFSIZE})])
        self.assertTrue(feed._container.flags & av.container.Flags.non_block.value)
        self.assertTrue(feed.read()[0])

    def test_mjpg_frames_come_back_as_bgr(self):
        import av
        container = av.open(str(self.recording()))
        container.flags = int(container.flags) | int(av.container.Flags.non_block.value)
        feed = ffcapture.FFmpegFeed(container, container.streams.video[0])
        try:
            ok, frame = feed.read()
            self.assertTrue(ok)
            self.assertEqual(frame.shape, (48, 64, 3))
            self.assertGreater(int(frame[24, 8, 0]), 200, "blue stayed blue")
            self.assertGreater(int(frame[24, 56, 2]), 200, "red stayed red")
            self.assertEqual(feed.fourcc, "MJPG")
            wait_until(lambda: feed.failed is not None)    # the file ends: the card "stops sending"
        finally:
            feed.release()


class FakeFeed:
    """ffcapture.FFmpegFeed as capture.py sees it. `accepts(device_id, width, height, fps)`
    decides which opens succeed; every attempt is kept in `opened`."""

    opened = []
    accepts = staticmethod(lambda device_id, width, height, fps: True)

    def __init__(self, device_id, width, height):
        self.device_id = device_id
        self.width, self.height = width or 1280, height or 720
        self.released = False
        self.fourcc = "MJPG"

    @classmethod
    def open(cls, device_id, width=None, height=None, fps=None, pixel_format=None):
        cls.opened.append((device_id, width, height, fps, pixel_format))
        return cls(device_id, width, height) if cls.accepts(device_id, width, height, fps) else None

    def read(self, timeout=1.0):
        return True, np.zeros((2, 2, 3), np.uint8)

    def take_skipped(self):
        return 1

    def get(self, prop):
        cv2 = capture.cv2
        return {cv2.CAP_PROP_FRAME_WIDTH: self.width, cv2.CAP_PROP_FRAME_HEIGHT: self.height,
                cv2.CAP_PROP_FPS: 30.0}.get(prop, 0)

    def release(self):
        self.released = True


CARDS = [{"name": "USB3.0 Capture", "id": "@device_pnp_card"}, {"name": "Integrated Camera", "id": "@device_pnp_cam"}]


class FFmpegFirst(unittest.TestCase):
    """FFmpeg is tried before OpenCV, and OpenCV's DirectShow is still there when it fails."""

    def setUp(self):
        FakeFeed.opened = []
        FakeFeed.accepts = staticmethod(lambda device_id, width, height, fps: True)
        FakeCamera.asked = []
        for target, value in ((capture.ffcapture, ("list_video_devices", lambda: list(CARDS))),
                              (capture.ffcapture, ("FFmpegFeed", FakeFeed)),
                              (capture.cv2, ("VideoCapture", FakeCamera))):
            patcher = unittest.mock.patch.object(target, *value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_card_ffmpeg_reads_is_listed_through_ffmpeg_by_name(self):
        FakeCamera.world = {(0, DSHOW): "reads", (1, DSHOW): "reads"}
        found = capture.list_devices()
        self.assertEqual([(d["index"], d["backend"], d["name"]) for d in found],
                         [(0, "FFmpeg", "USB3.0 Capture"), (1, "FFmpeg", "Integrated Camera")])
        self.assertFalse([key for key in FakeCamera.asked if key[0] in (0, 1)], "OpenCV was not needed")

    def test_a_card_ffmpeg_cannot_open_is_found_through_directshow(self):
        FakeFeed.accepts = staticmethod(lambda device_id, width, height, fps: device_id != "@device_pnp_card")
        FakeCamera.world = {(0, DSHOW): "reads"}
        found = capture.list_devices()
        self.assertEqual([(d["index"], d["backend"]) for d in found], [(0, "DirectShow"), (1, "FFmpeg")])

    def test_ffmpeg_is_asked_for_the_size_and_rate_and_nothing_older_is_tried_first(self):
        feed, actual = capture.negotiate(0, ("MJPG", 1920, 1080, 30), FFMPEG)
        self.assertEqual(FakeFeed.opened, [("@device_pnp_card", 1920, 1080, 30, None)])
        self.assertEqual((actual["backend"], actual["api"], actual["fourcc"], actual["name"]),
                         ("FFmpeg", FFMPEG, "MJPG", "USB3.0 Capture"))
        self.assertEqual(actual["asked"]["fps"], 30)

    def test_the_ladder_goes_on_inside_ffmpeg(self):
        FakeFeed.accepts = staticmethod(lambda device_id, width, height, fps: (width, fps) == (1280, 60))
        feed, actual = capture.negotiate(0, ("MJPG", 1920, 1080, 60), FFMPEG)
        self.assertEqual((actual["backend"], actual["width"]), ("FFmpeg", 1280))
        self.assertEqual([attempt[1:4] for attempt in FakeFeed.opened],
                         [(1920, 1080, 60), (1920, 1080, 30), (1280, 720, 60)])

    def test_raw_rungs_name_their_pixel_format(self):
        FakeFeed.accepts = staticmethod(lambda device_id, width, height, fps: width == 640)
        capture.negotiate(0, ("MJPG", 1920, 1080, 30), FFMPEG)
        self.assertEqual(FakeFeed.opened[-1], ("@device_pnp_card", 640, 480, 30, "yuyv422"))

    def test_a_card_ffmpeg_will_not_open_falls_back_to_directshow_at_the_same_index(self):
        FakeFeed.accepts = staticmethod(lambda device_id, width, height, fps: False)
        FakeCamera.world = {(0, DSHOW): "reads", (0, MSMF): "reads"}
        feed, actual = capture.negotiate(0, ("MJPG", 1920, 1080, 30), FFMPEG)
        self.assertEqual((actual["backend"], actual["api"]), ("DirectShow", DSHOW))
        self.assertNotIn((0, MSMF), FakeCamera.asked, "the other numbering is still not guessed at")

    def test_the_capture_loop_reads_ffmpeg_without_draining_and_counts_its_skips(self):
        worker = capture.CaptureWorker(lambda frame, at=None: None)
        worker.start(0, "MJPG", 1920, 1080, 30, FFMPEG)
        wait_until(lambda: worker.frames >= 10)
        opened_as = worker.format
        worker.stop()
        self.assertFalse(worker._draining)
        self.assertEqual(opened_as["backend"], "FFmpeg")
        self.assertGreaterEqual(worker.dropped_stale, 10, "the feed's own skip count is reported")


class Draining(unittest.TestCase):
    """DirectShow keeps one newest frame and its grab() never waits, so draining it only counted
    skips that never happened; Media Foundation queues, so it still drains."""

    def run_worker(self, api):
        FakeCamera.world = {(0, api): "reads"}
        worker = capture.CaptureWorker(lambda frame, at=None: None)
        with unittest.mock.patch.object(capture.cv2, "VideoCapture", FakeCamera):
            worker.start(0, "MJPG", 1920, 1080, 30, api)
            deadline = time.monotonic() + 3
            while worker.frames < 20 and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.stop()
        return worker

    def test_directshow_is_read_without_draining(self):
        worker = self.run_worker(DSHOW)
        self.assertGreaterEqual(worker.frames, 20)
        self.assertFalse(worker._draining)
        self.assertEqual(worker.dropped_stale, 0)

    def test_media_foundation_still_drains(self):
        self.assertTrue(self.run_worker(MSMF)._draining)


class FrameHandOff(unittest.TestCase):
    """Frames leave the capture thread ready to draw, so the window thread only draws."""

    def test_a_frame_arrives_in_the_painters_own_format_with_its_colours(self):
        from PySide6.QtGui import QColor, QImage
        from capturekvm.app import FrameSlot
        frame = np.zeros((2, 3, 3), np.uint8)
        frame[0, 0] = (255, 0, 0)        # BGR: blue
        frame[1, 2] = (0, 0, 255)        # BGR: red
        slot = FrameSlot()
        slot.put(frame)
        (image, buffer), _ = slot.take()
        self.assertEqual(image.format(), QImage.Format_RGB32)
        self.assertEqual((image.width(), image.height()), (3, 2))
        self.assertEqual(image.pixelColor(0, 0), QColor(0, 0, 255))
        self.assertEqual(image.pixelColor(2, 1), QColor(255, 0, 0))
        self.assertIsNone(buffer.base, "the image wraps a buffer of its own, not OpenCV's")


class EmptyCardList(unittest.TestCase):
    """When nothing opens, saying why. The three causes look identical from inside the app and are
    told apart by what Windows itself reports."""

    def privacy(self, values):
        def read(hive, path):
            for suffix, value in values.items():
                if path.endswith(suffix):
                    return value
            raise OSError("no such value")
        return capture.camera_blocked(read)

    def test_all_switches_allowed_is_not_a_block(self):
        self.assertIsNone(self.privacy({"webcam": "Allow", "NonPackaged": "Allow"}))

    def test_switches_windows_never_wrote_are_not_a_block(self):
        self.assertIsNone(self.privacy({}))

    def test_desktop_apps_turned_off_is_found(self):
        # The case that looks like "it works in the Camera app": a Store app keeps its access.
        reason = self.privacy({"webcam": "Allow", "NonPackaged": "Deny"})
        self.assertIn("desktop apps", reason)

    def test_the_explanation_follows_the_cause(self):
        blocked = capture.explain_no_devices(["USB Video"], "camera access is turned off")
        self.assertIn("Privacy & security", blocked)

        busy = capture.explain_no_devices(["USB Video"], None)
        self.assertIn("Windows sees USB Video", busy)
        self.assertIn("Another app", busy)

        missing = capture.explain_no_devices([], None)
        self.assertIn("does not see a capture card", missing)


if __name__ == "__main__":
    unittest.main()
