"""The responder's half of the pad (docs/GAMEPAD.md), through a real link on loopback.

The rules worth testing are not about the fields -- test_gamepad.py covers those -- but about
ownership and release: only the owner drives the pad, a report is acknowledged whether or not it
was injected, and nothing may be left held when the owner goes away or the link dies.
"""

import unittest

from core import gamepad, protocol, receiver
from core.tests.responder_harness import B, C, CAPS, Initiator, Machine, entry, wait_for

PAD_CAPS = CAPS + [gamepad.CAP_GAMEPAD]

# The real grace period is seconds, which is right for a Wi-Fi blip and wrong for a test suite.
TEST_GRACE = 0.3


class PadLink(unittest.TestCase):
    def setUp(self):
        self._grace = receiver.PAD_GRACE_SECONDS
        receiver.PAD_GRACE_SECONDS = TEST_GRACE

    def tearDown(self):
        receiver.PAD_GRACE_SECONDS = self._grace

    def assert_silenced(self, machine, pad=0):
        """The half that must happen at once: nothing left held."""
        self.assertTrue(
            wait_for(lambda: ("pad", pad, 0, 0, 0, 0, 0, 0, 0) in machine.injector.seen()),
            "the pad must be zeroed the moment its owner goes")

    def assert_unplugged_after_grace(self, machine):
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {}, timeout=3.0),
                        "and unplugged once the grace period passes")
    def machine(self, caps=PAD_CAPS, peers=None):
        peers = peers if peers is not None else [entry(B, "Peer B"), entry(C, "Peer C")]
        made = Machine(peers, caps=caps).start()
        self.addCleanup(made.stop)
        return made

    def initiator(self, machine, peer=B, caps=PAD_CAPS):
        link = Initiator(machine, peer=peer)
        self.addCleanup(link.close)
        welcome = link.handshake(link.hello(caps=caps))
        return link, welcome

    def pad_calls(self, machine):
        return [call for call in machine.injector.seen() if call[0].startswith("pad")]

    # --- the happy path ---

    def test_the_capability_is_offered_and_a_pad_can_be_driven(self):
        machine = self.machine()
        link, welcome = self.initiator(machine)
        self.assertIn(gamepad.CAP_GAMEPAD, welcome["caps"])

        route = link.take(reach=[], resistance_px=0)
        self.assertEqual(link.answer(route)[0], protocol.MSG_ACCEPT)

        link.send(gamepad.attach_msg(0, gamepad.KIND_X360))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {0: "x360"}))

        seq = link.input(gamepad.report_msg(0, buttons=gamepad.BTN_A, rt=255, lx=-32768))
        self.assertIsNotNone(link.acked(seq), "a pad report must be acknowledged like any input")
        self.assertIn(("pad", 0, gamepad.BTN_A, 0, 255, -32768, 0, 0, 0), machine.injector.seen())

        link.send(gamepad.detach_msg(0))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {}))

    def test_a_report_without_an_attach_plugs_one_in(self):
        # A reconnect can land a report before its attach; dropping it would lose a pad already
        # being held.
        machine = self.machine()
        link, _ = self.initiator(machine)
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)

        seq = link.input(gamepad.report_msg(0, buttons=gamepad.BTN_B))
        self.assertIsNotNone(link.acked(seq))
        self.assertEqual(machine.injector.plugged(), {0: "x360"})
        self.assertIn(("pad_attach", 0, "x360"), machine.injector.seen())

    # --- ownership ---

    def test_a_peer_that_does_not_own_this_machine_drives_nothing(self):
        machine = self.machine()
        owner, _ = self.initiator(machine, peer=B)
        other, _ = self.initiator(machine, peer=C)
        route = owner.take(reach=[], resistance_px=0)
        self.assertEqual(owner.answer(route)[0], protocol.MSG_ACCEPT)

        other.send(gamepad.attach_msg(0))
        seq = other.input(gamepad.report_msg(0, buttons=gamepad.BTN_Y))
        # Acknowledged, so the link stays up -- an initiator drops a link whose oldest
        # unacknowledged message ages out, and "not driving this machine" is not a dead link.
        self.assertIsNotNone(other.acked(seq))
        self.assertEqual(self.pad_calls(machine), [], "a non-owner must inject nothing")

    def test_a_peer_offered_no_capability_drives_nothing(self):
        machine = self.machine(caps=CAPS)        # no gamepad
        link, welcome = self.initiator(machine)
        self.assertNotIn(gamepad.CAP_GAMEPAD, welcome["caps"])
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)

        link.send(gamepad.attach_msg(0))
        link.input(gamepad.report_msg(0, buttons=gamepad.BTN_A))
        self.assertFalse(wait_for(lambda: self.pad_calls(machine) != [], timeout=0.5))

    # --- release, which is the rule that matters ---

    def test_letting_go_unplugs_every_pad(self):
        machine = self.machine()
        link, _ = self.initiator(machine)
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)
        link.send(gamepad.attach_msg(0))
        link.send(gamepad.attach_msg(1))
        self.assertTrue(wait_for(lambda: set(machine.injector.plugged()) == {0, 1}))

        link.let_go()
        self.assert_silenced(machine)
        self.assert_unplugged_after_grace(machine)

    def test_a_dropped_link_unplugs_every_pad(self):
        machine = self.machine()
        link, _ = self.initiator(machine)
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)
        link.send(gamepad.attach_msg(0))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {0: "x360"}))

        link.close()
        self.assert_silenced(machine)
        self.assert_unplugged_after_grace(machine)

    def test_another_machine_taking_over_unplugs_the_first_one_s_pads(self):
        machine = self.machine()
        first, _ = self.initiator(machine, peer=B)
        second, _ = self.initiator(machine, peer=C)
        route = first.take(reach=[], resistance_px=0)
        first.answer(route)
        first.send(gamepad.attach_msg(0))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {0: "x360"}))

        # B hands this machine on to C, which is a let-go for B.
        first.let_go(target=C)
        self.assert_silenced(machine)
        self.assert_unplugged_after_grace(machine)

    # --- rumble, which goes the other way ---

    def test_the_game_s_rumble_reaches_the_peer_holding_the_pad(self):
        machine = self.machine()
        link, _ = self.initiator(machine, caps=PAD_CAPS + [gamepad.CAP_RUMBLE])
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)
        link.send(gamepad.attach_msg(0))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {0: "x360"}))

        machine.injector.fire_rumble(0, 200, 40)
        got = link.expect(gamepad.MSG_GAMEPAD_RUMBLE)
        self.assertIsNotNone(got, "a game setting the motors must reach the controller")
        self.assertEqual((got["large"], got["small"]), (200, 40))

    def test_a_peer_that_cannot_rumble_is_not_sent_any(self):
        machine = self.machine()
        link, _ = self.initiator(machine, caps=PAD_CAPS)      # no CAP_RUMBLE
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)
        link.send(gamepad.attach_msg(0))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {0: "x360"}))
        machine.injector.fire_rumble(0, 200, 40)
        self.assertIsNone(link.expect(gamepad.MSG_GAMEPAD_RUMBLE, timeout=0.6))

    def test_letting_go_tells_the_peer_to_stop(self):
        # A link lost mid-rumble must not leave the pad buzzing in someone's hands.
        machine = self.machine()
        link, _ = self.initiator(machine, caps=PAD_CAPS + [gamepad.CAP_RUMBLE])
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)
        link.send(gamepad.attach_msg(0))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {0: "x360"}))
        machine.injector.fire_rumble(0, 255, 255)
        self.assertIsNotNone(link.expect(gamepad.MSG_GAMEPAD_RUMBLE))

        link.let_go()
        stop = link.expect(gamepad.MSG_GAMEPAD_RUMBLE)
        self.assertIsNotNone(stop, "letting go must send a stop")
        self.assertEqual((stop["large"], stop["small"]), (0, 0))

    def test_an_unchanged_rumble_is_not_repeated(self):
        machine = self.machine()
        link, _ = self.initiator(machine, caps=PAD_CAPS + [gamepad.CAP_RUMBLE])
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)
        link.send(gamepad.attach_msg(0))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {0: "x360"}))
        for _ in range(5):
            machine.injector.fire_rumble(0, 128, 0)
        self.assertIsNotNone(link.expect(gamepad.MSG_GAMEPAD_RUMBLE))
        self.assertIsNone(link.expect(gamepad.MSG_GAMEPAD_RUMBLE, timeout=0.5),
                          "a game setting the same value every frame must not flood the link")

    # --- surviving a blip ---

    def test_a_dropped_link_silences_the_pad_but_leaves_it_plugged_in(self):
        """A Wi-Fi blip must not look to the game like the controller was unplugged.

        The sticks and triggers go to zero at once, because none of that may outlive the owner;
        the socket stays so a reconnect a second later finds the same device."""
        machine = self.machine()
        link, _ = self.initiator(machine)
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)
        link.send(gamepad.attach_msg(0))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {0: "x360"}))
        link.input(gamepad.report_msg(0, buttons=gamepad.BTN_A, rt=255))
        self.assertTrue(wait_for(lambda: any(c[0] == "pad" for c in machine.injector.seen())))

        receiver.PAD_GRACE_SECONDS = 30.0        # long enough that this test is about the blip
        link.close()
        self.assert_silenced(machine)
        self.assertEqual(machine.injector.plugged(), {0: "x360"},
                         "but it must stay plugged in for the grace period")

    def test_coming_back_keeps_the_same_virtual_pad(self):
        machine = self.machine()
        first, _ = self.initiator(machine)
        route = first.take(reach=[], resistance_px=0)
        first.answer(route)
        first.send(gamepad.attach_msg(0))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {0: "x360"}))
        attaches = [c for c in machine.injector.seen() if c[0] == "pad_attach"]
        receiver.PAD_GRACE_SECONDS = 30.0        # the reconnect must land inside the grace
        first.close()

        second, _ = self.initiator(machine)
        route = second.take(reach=[], resistance_px=0)
        second.answer(route)
        second.send(gamepad.attach_msg(0))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {0: "x360"}))
        self.assertNotIn(("pad_detach", 0), machine.injector.seen(),
                         "reconnecting inside the grace period must not unplug and replug")
        self.assertGreaterEqual(
            len([c for c in machine.injector.seen() if c[0] == "pad_attach"]), len(attaches))

    def test_stopping_the_responder_unplugs_at_once(self):
        # Shutting down is not a blip; nothing is coming back, and a phantom controller must
        # not be left in the device list.
        machine = Machine([entry(B, "Peer B")], caps=PAD_CAPS).start()
        link = Initiator(machine, peer=B)
        self.addCleanup(link.close)
        link.handshake(link.hello(caps=PAD_CAPS))
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)
        link.send(gamepad.attach_msg(0))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {0: "x360"}))
        machine.stop()
        self.assertEqual(machine.injector.plugged(), {})

    # --- what the driver says ---

    def test_a_pad_that_cannot_be_plugged_in_is_not_recorded(self):
        machine = self.machine()
        machine.injector.pad_attach_ok = False
        link, _ = self.initiator(machine)
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)

        link.send(gamepad.attach_msg(0))
        seq = link.input(gamepad.report_msg(0, buttons=gamepad.BTN_A))
        self.assertIsNotNone(link.acked(seq), "the link must survive a driver that refuses")
        self.assertEqual(machine.injector.plugged(), {})
        self.assertNotIn("pad", [call[0] for call in machine.injector.seen()],
                         "no report should reach a pad that was never plugged in")

    def test_a_malformed_report_injects_nothing(self):
        machine = self.machine()
        link, _ = self.initiator(machine)
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)
        link.send(gamepad.attach_msg(0))
        self.assertTrue(wait_for(lambda: machine.injector.plugged() == {0: "x360"}))

        link.seq += 1
        link.send({"type": "gamepad", "data": {"pad": 0, "seq": link.seq, "lx": 999999}})
        self.assertFalse(wait_for(lambda: ("pad", 0, 0, 0, 0, 999999, 0, 0, 0) in machine.injector.seen(),
                                  timeout=0.5))
        self.assertNotIn("pad", [call[0] for call in machine.injector.seen()])

    def test_keyboard_and_mouse_still_work_beside_a_pad(self):
        # The pad is an addition, not a replacement: upstream's input path must be untouched.
        machine = self.machine()
        link, _ = self.initiator(machine)
        route = link.take(reach=[], resistance_px=0)
        link.answer(route)
        link.send(gamepad.attach_msg(0))
        link.key(protocol.MSG_KEYDOWN, "a")
        link.input(gamepad.report_msg(0, buttons=gamepad.BTN_A))
        seq = link.move(4, -3)
        self.assertIsNotNone(link.acked(seq))
        calls = [call[0] for call in machine.injector.seen()]
        self.assertIn("key", calls)
        self.assertIn("move", calls)
        self.assertIn("pad", calls)


if __name__ == "__main__":
    unittest.main()
