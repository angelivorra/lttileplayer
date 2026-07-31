#!/usr/bin/env python3
"""Tests del mapeo de botones MIDI del reproductor."""

import unittest
from pathlib import Path

import mido

from lgpt_player import match_button, match_pot, parse_button_spec, \
    parse_pot_target


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
        self.pots = [
            (parse_button_spec("cc:9:16"), (2, "lp_cutoff"), 0),
            (parse_button_spec("cc:9:17"), (2, "lp_res"), 4),
            (parse_button_spec("cc:9:18"), (None, "volume"), 7),  # canal via MIDI
        ]

    def test_pot_match(self):
        msg = mido.Message("control_change", channel=9, control=16, value=80)
        self.assertEqual(match_pot(self.pots, msg), (2, "lp_cutoff", 0))
        msg = mido.Message("control_change", channel=9, control=17, value=80)
        self.assertEqual(match_pot(self.pots, msg), (2, "lp_res", 4))

    def test_pot_channel_from_midi(self):
        msg = mido.Message("control_change", channel=9, control=18, value=80)
        self.assertEqual(match_pot(self.pots, msg), (9 % 8, "volume", 7))

    def test_pot_no_match(self):
        msg = mido.Message("control_change", channel=9, control=19, value=80)
        self.assertIsNone(match_pot(self.pots, msg))
        msg = mido.Message("control_change", channel=0, control=16, value=80)
        self.assertIsNone(match_pot(self.pots, msg))
        msg = mido.Message("note_on", channel=9, note=16, velocity=100)
        self.assertIsNone(match_pot(self.pots, msg))


class TestParsePotTarget(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_pot_target("2:lp_cutoff"), (2, "lp_cutoff"))
        self.assertEqual(parse_pot_target("0:volume"), (0, "volume"))

    def test_invalid(self):
        self.assertIsNone(parse_pot_target(""))
        self.assertIsNone(parse_pot_target("8:volume"))   # canal fuera de rango
        self.assertIsNone(parse_pot_target("2:"))
        self.assertIsNone(parse_pot_target("x:volume"))
        self.assertIsNone(parse_pot_target(None))


class TestWavRecorder(unittest.TestCase):
    def test_records_wav(self):
        import numpy as np
        import soundfile as sf
        from lgpt_player import WavRecorder
        path = "/tmp/lgpt_recorder_test.wav"
        try:
            rec = WavRecorder(path, 44100)
            t = np.arange(4410, dtype=np.float32) / 44100
            block = np.stack([np.sin(t), np.cos(t)], axis=1)
            rec.write(block)
            rec.write(block)
            rec.close()
            data, sr = sf.read(path, dtype="float32")
            self.assertEqual(sr, 44100)
            self.assertEqual(len(data), 8820)
            np.testing.assert_allclose(data[:4410, 0], block[:, 0], atol=1e-3)
        finally:
            Path(path).unlink(missing_ok=True)


class TestPython311Compat(unittest.TestCase):
    """El código debe parsear con la gramática de Python 3.11 (la Pi)."""

    def test_sources_parse_as_311(self):
        import ast
        root = Path(__file__).resolve().parent.parent
        for name in ("lgpt_engine.py", "lgpt_parser.py",
                     "lgpt_player.py", "lgpt_setup.py"):
            src = (root / name).read_text()
            ast.parse(src, filename=name, feature_version=(3, 11))


if __name__ == "__main__":
    unittest.main()
