"""core/gamepad.py: the pad messages' builders and validators (docs/GAMEPAD.md)."""

import unittest

from core import gamepad


class Builders(unittest.TestCase):
    def test_attach_defaults_to_an_x360_on_pad_zero(self):
        self.assertEqual(
            gamepad.attach_msg(),
            {"type": "gamepad_attach", "data": {"pad": 0, "kind": "x360"}},
        )

    def test_attach_refuses_an_unknown_kind(self):
        with self.assertRaises(ValueError):
            gamepad.attach_msg(0, "n64")

    def test_attach_refuses_a_pad_index_off_the_end(self):
        with self.assertRaises(ValueError):
            gamepad.attach_msg(gamepad.MAX_PADS)

    def test_detach_names_its_pad(self):
        self.assertEqual(gamepad.detach_msg(2), {"type": "gamepad_detach", "data": {"pad": 2}})

    def test_a_report_carries_every_field_even_when_neutral(self):
        message = gamepad.report_msg()
        self.assertEqual(message["type"], "gamepad")
        self.assertEqual(
            message["data"],
            {"pad": 0, "buttons": 0, "lt": 0, "rt": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0},
        )

    def test_a_report_takes_a_seq_when_one_is_given(self):
        self.assertEqual(gamepad.report_msg(seq=7)["data"]["seq"], 7)

    def test_a_report_clamps_rather_than_refusing(self):
        # The sender has already scaled a stick; a rounding error at the end of its range should
        # not cost the whole frame.
        data = gamepad.report_msg(lx=-40000, ly=40000, lt=300, rt=-5, buttons=0x1FFFF)["data"]
        self.assertEqual(data["lx"], gamepad.AXIS_MIN)
        self.assertEqual(data["ly"], gamepad.AXIS_MAX)
        self.assertEqual(data["lt"], gamepad.TRIGGER_MAX)
        self.assertEqual(data["rt"], 0)
        self.assertEqual(data["buttons"], gamepad.BUTTON_MASK)

    def test_a_report_refuses_a_field_it_does_not_know(self):
        with self.assertRaises(ValueError):
            gamepad.report_msg(lz=1)


class Validators(unittest.TestCase):
    def report(self, **fields):
        data = {"pad": 0, "seq": 1}
        data.update(fields)
        return {"type": "gamepad", "data": data}

    def test_a_whole_report_reads_back(self):
        read = gamepad.read_report(self.report(buttons=gamepad.BTN_A, lt=255, lx=-32768, ry=100))
        self.assertEqual(read["pad"], 0)
        self.assertEqual(read["seq"], 1)
        self.assertEqual(read["buttons"], gamepad.BTN_A)
        self.assertEqual(read["lt"], 255)
        self.assertEqual(read["lx"], -32768)
        self.assertEqual(read["ry"], 100)
        self.assertEqual(read["rx"], 0)

    def test_a_missing_field_reads_as_neutral(self):
        read = gamepad.read_report({"type": "gamepad", "data": {"pad": 0, "seq": 3}})
        for name, value in gamepad.NEUTRAL.items():
            self.assertEqual(read[name], value, name)

    def test_a_report_without_a_seq_is_malformed(self):
        # Every sequenced message needs its number: without one there is nothing to acknowledge.
        self.assertIsNone(gamepad.read_report({"type": "gamepad", "data": {"pad": 0}}))

    def test_an_out_of_range_field_loses_the_whole_report(self):
        self.assertIsNone(gamepad.read_report(self.report(lx=40000)))
        self.assertIsNone(gamepad.read_report(self.report(lt=256)))
        self.assertIsNone(gamepad.read_report(self.report(buttons=0x10000)))

    def test_a_float_or_a_bool_is_not_an_integer_field(self):
        # JSON's 1.0 and true both compare equal to 1 in Python; neither is an integer field.
        self.assertIsNone(gamepad.read_report(self.report(lt=1.0)))
        self.assertIsNone(gamepad.read_report(self.report(lt=True)))

    def test_a_pad_index_off_the_end_is_malformed(self):
        self.assertIsNone(gamepad.read_report(self.report(pad=gamepad.MAX_PADS)))

    def test_another_type_is_not_a_report(self):
        self.assertIsNone(gamepad.read_report({"type": "keydown", "data": {"pad": 0, "seq": 1}}))
        self.assertIsNone(gamepad.read_report({"type": "gamepad", "data": "no"}))
        self.assertIsNone(gamepad.read_report("gamepad"))

    def test_attach_reads_back_and_defaults_to_x360(self):
        self.assertEqual(
            gamepad.read_attach({"type": "gamepad_attach", "data": {}}),
            {"pad": 0, "kind": "x360"},
        )
        self.assertEqual(
            gamepad.read_attach({"type": "gamepad_attach", "data": {"pad": 3, "kind": "ds4"}}),
            {"pad": 3, "kind": "ds4"},
        )

    def test_attach_refuses_a_kind_it_does_not_know(self):
        # Refused rather than taken as x360: a peer asking for a pad this side cannot emulate
        # should be told, not quietly given another.
        self.assertIsNone(gamepad.read_attach({"type": "gamepad_attach", "data": {"kind": "n64"}}))

    def test_detach_reads_back(self):
        self.assertEqual(gamepad.read_detach({"type": "gamepad_detach", "data": {}}), {"pad": 0})
        self.assertIsNone(gamepad.read_detach({"type": "gamepad_detach", "data": {"pad": -1}}))

    def test_unknown_fields_are_ignored(self):
        # Section 2: a later version may add fields without breaking this one.
        read = gamepad.read_report(self.report(buttons=gamepad.BTN_B, rumble=1))
        self.assertIsNotNone(read)
        self.assertEqual(read["buttons"], gamepad.BTN_B)


class Vocabulary(unittest.TestCase):
    def test_every_button_name_is_one_bit_inside_the_mask(self):
        for name, bit in gamepad.BUTTON_NAMES.items():
            self.assertEqual(bit & gamepad.BUTTON_MASK, bit, name)
            self.assertEqual(bin(bit).count("1"), 1, name)

    def test_button_bits_are_distinct(self):
        bits = list(gamepad.BUTTON_NAMES.values())
        self.assertEqual(len(bits), len(set(bits)))

    def test_button_names_reads_a_mask_back(self):
        mask = gamepad.BTN_A | gamepad.BTN_START | gamepad.BTN_DPAD_LEFT
        self.assertEqual(gamepad.button_names(mask), ["a", "dpad_left", "start"])

    def test_only_the_report_is_sequenced(self):
        self.assertEqual(gamepad.SEQUENCED_TYPES, {gamepad.MSG_GAMEPAD})
        self.assertEqual(
            gamepad.PAD_TYPES,
            {gamepad.MSG_GAMEPAD, gamepad.MSG_GAMEPAD_ATTACH, gamepad.MSG_GAMEPAD_DETACH},
        )

    def test_the_pad_types_are_not_upstream_input_types(self):
        # They are acknowledged like input but they are not input: nothing that walks
        # protocol.INPUT_TYPES should pick them up.
        from core import protocol

        self.assertFalse(gamepad.PAD_TYPES & protocol.INPUT_TYPES)


if __name__ == "__main__":
    unittest.main()
