#!/usr/bin/env python3
"""Tests del mapeo de botones MIDI del reproductor."""

import unittest

import mido

from lgpt_player import match_button, match_pot, parse_button_spec


class TestParseButtonSpec(unittest.TestCase):
    def test_note(self):
        self.assertEqual(parse_button_spec("note:0:36"), ("note_on", 0, 36))
        self.assertEqual(parse_button_spec("note:9:127"), ("note_on", 9, 127))

    def test_cc(self):
        self.assertEqual(
            parse_button_spec("cc:2:41"), ("control_change", 2, 41))

    def test_invalid(self):
        self.assertIsNone(parse_button_spec(""))
        self.assertIsNone(parse_button_spec("note:0"))
        self.assertIsNone(parse_button_spec("foo:0:1"))
        self.assertIsNone(parse_button_spec("note:x:1"))
        self.assertIsNone(parse_button_spec(None))


class TestMatchButton(unittest.TestCase):
    def setUp(self):
        self.mapping = {
            "up": parse_button_spec("note:0:36"),
            "down": parse_button_spec("note:0:37"),
            "play": parse_button_spec("cc:0:41"),
            "stop": None,                    # sin asignar
        }

    def test_note_on(self):
        msg = mido.Message("note_on", channel=0, note=36, velocity=100)
        self.assertEqual(match_button(self.mapping, msg), "up")
        msg = mido.Message("note_on", channel=0, note=37, velocity=100)
        self.assertEqual(match_button(self.mapping, msg), "down")

    def test_note_off_ignored(self):
        msg = mido.Message("note_off", channel=0, note=36, velocity=0)
        self.assertIsNone(match_button(self.mapping, msg))
        msg = mido.Message("note_on", channel=0, note=36, velocity=0)
        self.assertIsNone(match_button(self.mapping, msg))

    def test_wrong_channel_or_number(self):
        msg = mido.Message("note_on", channel=1, note=36, velocity=100)
        self.assertIsNone(match_button(self.mapping, msg))
        msg = mido.Message("note_on", channel=0, note=38, velocity=100)
        self.assertIsNone(match_button(self.mapping, msg))

    def test_cc(self):
        msg = mido.Message("control_change", channel=0, control=41, value=127)
        self.assertEqual(match_button(self.mapping, msg), "play")
        # El release (valor 0) no dispara
        msg = mido.Message("control_change", channel=0, control=41, value=0)
        self.assertIsNone(match_button(self.mapping, msg))


class TestMatchPot(unittest.TestCase):
    def setUp(self):
        self.pots = {
            "cutoff": parse_button_spec("cc:9:16"),
            "volume": parse_button_spec("cc:9:17"),
            "pan": None,
            "pitch": None,
        }

    def test_pot_match(self):
        msg = mido.Message("control_change", channel=9, control=16, value=80)
        self.assertEqual(match_pot(self.pots, msg), "cutoff")
        msg = mido.Message("control_change", channel=9, control=17, value=80)
        self.assertEqual(match_pot(self.pots, msg), "volume")

    def test_pot_no_match(self):
        msg = mido.Message("control_change", channel=9, control=18, value=80)
        self.assertIsNone(match_pot(self.pots, msg))
        msg = mido.Message("control_change", channel=0, control=16, value=80)
        self.assertIsNone(match_pot(self.pots, msg))
        msg = mido.Message("note_on", channel=9, note=16, velocity=100)
        self.assertIsNone(match_pot(self.pots, msg))


if __name__ == "__main__":
    unittest.main()
