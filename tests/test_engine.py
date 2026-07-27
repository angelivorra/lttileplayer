#!/usr/bin/env python3
"""Tests headless del motor LGPT (sin tarjeta de audio).

Ejecutar con: .venv/bin/python -m unittest discover -s tests -v
o con pytest: .venv/bin/python -m pytest tests/
"""

import math
import unittest
from pathlib import Path

import numpy as np

from lgpt_engine import (
    Engine,
    Sample,
    TICKS_PER_STEP,
    SAMPLE_RATE,
)
from lgpt_parser import LGPTProject

SONGS_DIR = Path("/home/angel/LGPT/songs")
SONGS = ["lgpt_abduccion", "lgpt_Bulebule", "lgpt_Energia",
         "lgpt_Sartenazo.VERSION1"]


def make_project(tempo="120") -> LGPTProject:
    """Proyecto sintético mínimo: canal 0 toca la phrase 0 en bucle.

    Estructura: song row 0 -> chain 0 -> phrase 0 (16 pasos). No toca
    disco: el sample se inyecta después en el banco del engine.
    """
    p = LGPTProject(Path("/nonexistent"))
    p.root = object()                      # evita que Engine llame a load()
    p.project = {"tempo": tempo, "master": "100", "transpose": "0"}
    p.song = bytearray([0xFF] * (8 * 256))
    p.song[0] = 0                          # canal 0, fila 0 -> chain 0
    p.chains = bytearray([0xFF] * (255 * 16))
    p.chains[0] = 0                        # chain 0 paso 0 -> phrase 0
    p.transposes = bytearray(255 * 16)
    p.notes = bytearray([0xFF] * (255 * 16))
    p.instruments = bytearray([0xFF] * (255 * 16))
    p.cmd1 = ["----"] * (255 * 16)
    p.param1 = [0] * (255 * 16)
    p.cmd2 = ["----"] * (255 * 16)
    p.param2 = [0] * (255 * 16)
    p.tables = {}
    p.grooves = bytearray()
    p.instrument_bank = {
        0: {"type": "Sample",
            "params": {"sample": "test.wav", "volume": "128", "pan": "127"}},
    }
    return p


def make_engine(tempo="120") -> Engine:
    engine = Engine(make_project(tempo))
    # Sample sintético: seno de 1s a 44100 Hz
    t = np.arange(SAMPLE_RATE, dtype=np.float32) / SAMPLE_RATE
    data = (0.5 * np.sin(2 * np.pi * 440 * t))[:, None].astype(np.float32)
    engine.bank.samples["test.wav"] = Sample(data, SAMPLE_RATE)
    engine.start()
    return engine


def note_row(p: LGPTProject, step: int, note=60, instr=0):
    p.notes[step] = note
    p.instruments[step] = instr


class TestTiming(unittest.TestCase):
    def test_samples_per_tick_formula(self):
        engine = make_engine("120")
        # SyncMaster::SetTempo del upstream
        expected = 60.0 * SAMPLE_RATE * 2.0 / 120 / 8.0 / TICKS_PER_STEP
        self.assertAlmostEqual(engine.samples_per_tick, expected)
        self.assertAlmostEqual(engine.samples_per_tick, 918.75)

    def test_step_advance_every_6_ticks(self):
        # La fila 0 dura 6 ticks (0-5); el avance ocurre en el tick 6
        engine = make_engine("120")
        for _ in range(TICKS_PER_STEP + 1):
            engine._process_tick()
        self.assertEqual(engine.channels[0].phrase_pos, 1)
        for _ in range(TICKS_PER_STEP):
            engine._process_tick()
        self.assertEqual(engine.channels[0].phrase_pos, 2)

    def test_render_is_sample_accurate(self):
        # Renderizando un step completo (6 ticks) la phrase debe avanzar
        engine = make_engine("120")
        note_row(engine.project, 0)
        samples_per_step = engine.samples_per_tick * TICKS_PER_STEP  # 5512.5
        rendered = 0
        while rendered < math.ceil(samples_per_step) + 1:
            engine.render(512)
            rendered += 512
        self.assertEqual(engine.channels[0].phrase_pos, 1)
        # Sin deriva: los ticks procesados cuadran con los samples renderizados
        expected_ticks = rendered / engine.samples_per_tick
        self.assertAlmostEqual(engine.tick_count, expected_ticks, delta=1.0)


class TestVoices(unittest.TestCase):
    def test_note_triggers_voice_and_sound(self):
        engine = make_engine()
        note_row(engine.project, 0)
        engine._process_tick()             # time_to_start 1 -> 0: trigger
        self.assertIsNotNone(engine.channels[0].voice)
        out = engine.render(512)
        self.assertGreater(float(np.abs(out).max()), 0.01)

    def test_transpose_of_chain(self):
        engine = make_engine()
        note_row(engine.project, 0)
        engine.project.transposes[0] = 12  # chain 0 paso 0: +12 semitonos
        engine._process_tick()
        voice = engine.channels[0].voice
        self.assertIsNotNone(voice)
        self.assertEqual(voice.note, 72)
        self.assertAlmostEqual(voice.base_speed, 2.0, places=5)

    def test_kill(self):
        # KILL 00 mata la voz en el mismo tick en que se procesa (como el
        # upstream: timeToLive = param+1 y la cuenta atrás corre ese tick)
        engine = make_engine()
        note_row(engine.project, 0)
        engine.project.cmd1[1] = "KILL"    # KILL 0000 en el paso 1
        engine.project.param1[1] = 0
        for _ in range(TICKS_PER_STEP):
            engine._process_tick()
        self.assertIsNotNone(engine.channels[0].voice)
        engine._process_tick()             # avanza al paso 1 y ejecuta KILL
        self.assertIsNone(engine.channels[0].voice)

    def test_volm_instant(self):
        engine = make_engine()
        note_row(engine.project, 0)
        engine.project.cmd1[0] = "VOLM"
        engine.project.param1[0] = 0x0000  # volumen 0 inmediato
        engine._process_tick()
        voice = engine.channels[0].voice
        self.assertIsNotNone(voice)
        self.assertEqual(voice.vol_cur, 0.0)
        out = engine.render(512)
        self.assertEqual(float(np.abs(out).max()), 0.0)

    def test_dlay(self):
        engine = make_engine()
        note_row(engine.project, 0)
        engine.project.cmd1[0] = "DLAY"
        engine.project.param1[0] = 2       # retrasa 2+1 = 3 ticks
        engine.start()                     # re-lee la fila 0 con el DLAY
        engine._process_tick()
        self.assertIsNone(engine.channels[0].voice)
        engine._process_tick()
        self.assertIsNone(engine.channels[0].voice)
        engine._process_tick()
        self.assertIsNotNone(engine.channels[0].voice)

    def test_stop_command(self):
        engine = make_engine()
        note_row(engine.project, 0)
        engine.project.cmd1[0] = "STOP"
        engine._process_tick()
        self.assertFalse(engine.playing)
        self.assertTrue(engine.finished)
        out = engine.render(512)
        self.assertEqual(float(np.abs(out).max()), 0.0)

    def test_table_volm(self):
        engine = make_engine()
        note_row(engine.project, 0)
        engine.project.cmd1[0] = "TABL"
        engine.project.param1[0] = 0
        engine.project.tables[0] = {
            "cmd1": ["VOLM"] + ["----"] * 15,
            "param1": [0] * 16,            # VOLM 0000 -> volumen 0
            "cmd2": ["----"] * 16, "param2": [0] * 16,
            "cmd3": ["----"] * 16, "param3": [0] * 16,
        }
        engine._process_tick()             # trigger + tabla fila 0
        voice = engine.channels[0].voice
        self.assertIsNotNone(voice)
        self.assertEqual(voice.vol_cur, 0.0)

    def test_midi_cc_events(self):
        engine = make_engine()
        note_row(engine.project, 0)
        engine._process_tick()
        engine.push_event("cc", 0, 7, 0)   # volumen canal 0 a 0
        out = engine.render(512)
        self.assertEqual(float(np.abs(out).max()), 0.0)


@unittest.skipUnless(SONGS_DIR.is_dir(), "canciones no disponibles")
class TestRealSongs(unittest.TestCase):
    def test_render_all_songs(self):
        for name in SONGS:
            with self.subTest(song=name):
                engine = Engine(SONGS_DIR / name)
                engine.start()
                blocks = int(15 * SAMPLE_RATE / 512)
                peak = 0.0
                energy = 0.0
                for _ in range(blocks):
                    out = engine.render(512)
                    self.assertFalse(np.isnan(out).any())
                    peak = max(peak, float(np.abs(out).max()))
                    energy += float((out ** 2).mean())
                self.assertGreater(energy, 0.0, "salida silenciosa")
                self.assertLessEqual(peak, 1.0, "clipping")


if __name__ == "__main__":
    unittest.main()
