"""Unit tests for the Windows client's own modules.

The end-to-end check covers the link against a real responder, but it says nothing about the
parts that never touch a socket: the picture pipeline, the key and scroll mapping, what the
clipboard will and will not send, and what the beacon listener accepts.

The range detector earns most of this file. Its first version tested "nothing darker than 12",
which calls almost every desktop picture limited-range, and the only reason that was caught was
an ad-hoc script that no longer exists. These cases are that script, kept.

    cd windows && .venv\\Scripts\\python.exe -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "beamer-vigem"))
sys.path.insert(0, str(ROOT / "windows"))

import numpy as np  # noqa: E402

from capturekvm import capture, clipboard, discover, padwire, process  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
