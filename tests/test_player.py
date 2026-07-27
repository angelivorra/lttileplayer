#!/usr/bin/env python3
"""Tests del mapeo de botones MIDI del reproductor."""

import unittest

import mido

from lgpt_player import match_button, parse_button_spec


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


if __name__ == "__main__":
    unittest.main()
