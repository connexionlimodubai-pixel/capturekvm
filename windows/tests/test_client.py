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
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "beamer-vigem"))
sys.path.insert(0, str(ROOT / "windows"))

import numpy as np  # noqa: E402

from capturekvm import capture, clipboard, discover, launcher, padwire, process, store  # noqa: E402
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


DSHOW = capture.cv2.CAP_DSHOW
MSMF = capture.cv2.CAP_MSMF


JPEG = capture.cv2.imencode(".jpg", np.full((8, 8, 3), 128, np.uint8))[1].reshape(1, -1)


class FakeCamera:
    """cv2.VideoCapture, for a PC described by `world`: (index, api) -> a state, or absent.

    "opens" is the failure that matters most for finding cards: a device the API accepts and then
    never gets a frame from. "reads" is a working card whose driver ignores a request for undecoded
    frames; "jpeg" honours it; "junk" half honours it and hands back bytes that are no picture.
    """

    world = {}
    asked = []

    def __init__(self, index, api):
        self.key = (index, api)
        FakeCamera.asked.append(self.key)
        self.state = FakeCamera.world.get(self.key)
        self.undecoded = False
        self.settings = []

    def isOpened(self):
        return self.state in ("opens", "reads", "jpeg", "junk")

    def read(self):
        if self.state in ("reads", "jpeg", "junk"):
            if self.undecoded and self.state == "jpeg":
                return True, JPEG.copy()
            if self.undecoded and self.state == "junk":
                return True, np.full((1, 64), 7, np.uint8)
            return True, np.zeros((4, 4, 3), np.uint8)
        return False, None

    def grab(self):
        return self.state in ("reads", "jpeg", "junk")

    def retrieve(self):
        return self.read()

    def get(self, prop):
        cv2 = capture.cv2
        return {cv2.CAP_PROP_FRAME_WIDTH: 1920, cv2.CAP_PROP_FRAME_HEIGHT: 1080,
                cv2.CAP_PROP_FPS: 30.0, cv2.CAP_PROP_FOURCC: capture.fourcc_code("MJPG")}.get(prop, 0)

    def set(self, prop, value):
        self.settings.append((prop, value))
        if prop == capture.cv2.CAP_PROP_CONVERT_RGB:
            self.undecoded = not value
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
        with unittest.mock.patch.object(capture.cv2, "VideoCapture", FakeCamera):
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
        with unittest.mock.patch.object(capture.cv2, "VideoCapture", FakeCamera):
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


class DecodingAndApiChoice(unittest.TestCase):
    """DirectShow's MJPG, decoded here instead of by Windows's MJPEG Decompressor filter and then
    flipped from bottom-up RGB; and the switch that compares the two APIs on one card."""

    def open(self, state, api, fourcc="MJPG"):
        FakeCamera.world = {(0, api): state}
        with unittest.mock.patch.object(capture.cv2, "VideoCapture", FakeCamera):
            feed = capture.open_device(0, fourcc, 1920, 1080, 30, api)
            picture = feed.read() if feed else (False, None)
        return feed, picture

    def test_directshow_hands_over_compressed_frames_when_the_driver_agrees(self):
        feed, (ok, image) = self.open("jpeg", DSHOW)
        self.assertTrue(feed.jpeg)
        self.assertTrue(ok)
        self.assertEqual(image.shape, (8, 8, 3), "decoded here, into an ordinary picture")

    def test_a_driver_that_ignores_the_request_is_read_as_before(self):
        feed, (ok, image) = self.open("reads", DSHOW)
        self.assertFalse(feed.jpeg)
        self.assertEqual(image.shape, (4, 4, 3))

    def test_a_driver_that_sends_junk_goes_back_to_decoding_itself(self):
        # Half honouring the request is the case to fear: bytes that are neither a picture nor a
        # JPEG would show nothing at all. The first frame decides, and the request is withdrawn.
        feed, (ok, image) = self.open("junk", DSHOW)
        self.assertFalse(feed.jpeg)
        self.assertEqual(feed.capture.settings[-1], (capture.cv2.CAP_PROP_CONVERT_RGB, 1))
        self.assertEqual(image.shape, (4, 4, 3))

    def test_media_foundation_is_left_to_decode_for_itself(self):
        # Its decoder may be the GPU's; nothing here would beat that.
        feed, _ = self.open("jpeg", MSMF)
        self.assertFalse(feed.jpeg)
        self.assertNotIn(capture.cv2.CAP_PROP_CONVERT_RGB, [prop for prop, _ in feed.capture.settings])

    def test_uncompressed_formats_are_never_asked_for_undecoded(self):
        feed, _ = self.open("jpeg", DSHOW, fourcc="YUY2")
        self.assertFalse(feed.jpeg)

    def test_the_status_bar_is_told_who_decodes(self):
        FakeCamera.world = {(0, DSHOW): "jpeg"}
        with unittest.mock.patch.object(capture.cv2, "VideoCapture", FakeCamera):
            feed, actual = capture.negotiate(0, ("MJPG", 1920, 1080, 30), DSHOW)
        self.assertEqual((actual["backend"], actual["decoded"]), ("DirectShow", "here"))

    def test_draining_does_not_decode_the_frames_it_throws_away(self):
        # grab() skips stale frames; only the one finally retrieved is worth 11 ms of decoding.
        feed, _ = self.open("jpeg", DSHOW)
        with unittest.mock.patch.object(capture.cv2, "imdecode", wraps=capture.cv2.imdecode) as decode:
            for _ in range(3):
                feed.grab()
            feed.retrieve()
        self.assertEqual(decode.call_count, 1)

    def test_the_api_choice_narrows_the_search(self):
        self.assertEqual([api for _, api in capture.backends_for("dshow")], [DSHOW])
        self.assertEqual([api for _, api in capture.backends_for("msmf")], [MSMF])
        self.assertEqual([api for _, api in capture.backends_for("auto")], [DSHOW, MSMF])
        self.assertEqual(capture.backends_for("something old"), capture.BACKENDS)


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
