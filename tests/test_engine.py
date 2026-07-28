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
    parse_midi_instrument,
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
        0x80: {"type": "Midi",
               "params": {"channel": "3", "volume": "255", "note length": "0"}},
    }
    return p


class MidiCollector:
    """Sink MidiOut de prueba: registra todos los eventos."""

    def __init__(self):
        self.events = []

    def note_on(self, channel, note, velocity):
        self.events.append(("note_on", channel, note, velocity))

    def note_off(self, channel, note):
        self.events.append(("note_off", channel, note))

    def cc(self, channel, control, value):
        self.events.append(("cc", channel, control, value))

    def program_change(self, channel, program):
        self.events.append(("program_change", channel, program))


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

    def test_param_events(self):
        engine = make_engine()
        note_row(engine.project, 0)
        engine._process_tick()
        engine.push_event("param", 0, "volume", 0)
        out = engine.render(512)
        self.assertEqual(float(np.abs(out).max()), 0.0)
        # pitch: una octava arriba duplica la velocidad de la voz
        voice = engine.channels[0].voice
        base = voice.base_speed
        engine.push_event("param", 0, "pitch", 127)
        engine.render(16)
        self.assertAlmostEqual(
            engine.channels[0].cc_pitch, 2.0 ** (63 / 64), places=5)
        self.assertAlmostEqual(voice.base_speed, base)   # no cambia la base


class TestMidiOut(unittest.TestCase):
    def make_midi_engine(self):
        engine = make_engine()
        engine.midi_out = MidiCollector()
        return engine

    def test_note_on_off(self):
        engine = self.make_midi_engine()
        note_row(engine.project, 0, note=60, instr=0x80)
        engine._process_tick()             # trigger: CC7 + note on
        self.assertEqual(
            engine.midi_out.events,
            [("cc", 3, 7, 127), ("note_on", 3, 60, 127)])
        note_row(engine.project, 1, note=62, instr=0x80)
        for _ in range(TICKS_PER_STEP + 1):
            engine._process_tick()
        # Nueva nota: note off de la anterior antes del note on
        events = engine.midi_out.events
        off_idx = events.index(("note_off", 3, 60))
        on_idx = events.index(("note_on", 3, 62, 127))
        self.assertLess(off_idx, on_idx)

    def test_mdcc(self):
        engine = self.make_midi_engine()
        note_row(engine.project, 0, note=60, instr=0x80)
        engine.project.cmd1[1] = "MDCC"
        engine.project.param1[1] = (74 << 8) | 100   # CC74 = 100
        for _ in range(TICKS_PER_STEP + 1):
            engine._process_tick()
        self.assertIn(("cc", 3, 74, 100), engine.midi_out.events)

    def test_mdpg(self):
        engine = self.make_midi_engine()
        note_row(engine.project, 0, note=60, instr=0x80)
        engine.project.cmd1[1] = "MDPG"
        engine.project.param1[1] = 42
        for _ in range(TICKS_PER_STEP + 1):
            engine._process_tick()
        self.assertIn(("program_change", 3, 42), engine.midi_out.events)

    def test_volm_midi_envia_cc7(self):
        engine = self.make_midi_engine()
        note_row(engine.project, 0, note=60, instr=0x80)
        engine.project.cmd1[1] = "VOLM"
        engine.project.param1[1] = 0x0080  # 128 // 2 = 64
        for _ in range(TICKS_PER_STEP + 1):
            engine._process_tick()
        self.assertIn(("cc", 3, 7, 64), engine.midi_out.events)

    def test_note_length(self):
        engine = self.make_midi_engine()
        engine.project.instrument_bank[0x80]["params"]["note length"] = "2"
        engine.midi_instruments = {
            0x80: parse_midi_instrument(
                0x80, engine.project.instrument_bank[0x80]["params"])
        }
        note_row(engine.project, 0, note=60, instr=0x80)
        engine._process_tick()             # trigger
        self.assertNotIn(("note_off", 3, 60), engine.midi_out.events)
        engine._process_tick()             # ticks 2 -> 1
        engine._process_tick()             # ticks 1 -> 0: note off
        self.assertIn(("note_off", 3, 60), engine.midi_out.events)

    def test_panic_apaga_notas(self):
        engine = self.make_midi_engine()
        note_row(engine.project, 0, note=60, instr=0x80)
        engine._process_tick()
        engine.panic()
        self.assertIn(("note_off", 3, 60), engine.midi_out.events)

    def test_mdcc_ignorado_con_instrumento_sample(self):
        # Como en el upstream: MDCC solo sale si el canal tiene un
        # instrumento MIDI activo
        engine = self.make_midi_engine()
        note_row(engine.project, 0, note=60, instr=0)   # instrumento Sample
        engine.project.cmd1[1] = "MDCC"
        engine.project.param1[1] = (74 << 8) | 100
        for _ in range(TICKS_PER_STEP + 1):
            engine._process_tick()
        self.assertEqual(engine.midi_out.events, [])


class TestAudioDelay(unittest.TestCase):
    def test_audio_delayed_but_midi_immediate(self):
        engine = make_engine()
        engine.set_audio_delay(2 * 512 / SAMPLE_RATE)  # 2 bloques
        engine.midi_out = MidiCollector()
        note_row(engine.project, 0, note=60, instr=0x80)
        b1 = engine.render(512)
        # El MIDI sale al momento; el audio todavía es silencio
        self.assertTrue(engine.midi_out.events)
        self.assertEqual(float(np.abs(b1).max()), 0.0)
        b2 = engine.render(512)
        self.assertEqual(float(np.abs(b2).max()), 0.0)

    def test_delay_matches_offset(self):
        delay_samples = 512
        e1 = make_engine()
        note_row(e1.project, 0)
        e2 = make_engine()
        note_row(e2.project, 0)
        e2.set_audio_delay(delay_samples / SAMPLE_RATE)
        plain = np.concatenate([e1.render(512) for _ in range(8)])
        delayed = np.concatenate([e2.render(512) for _ in range(8)])
        self.assertEqual(
            float(np.abs(delayed[:delay_samples]).max()), 0.0)
        np.testing.assert_allclose(
            delayed[delay_samples:], plain[:-delay_samples], atol=1e-6)


class TestGroove(unittest.TestCase):
    def make_groove_engine(self, pattern):
        engine = make_engine()
        engine.groove_data = bytearray([0xFF] * (0x20 * 16))
        engine.groove_data[:len(pattern)] = pattern
        engine.start()                     # re-inicia con el nuevo groove
        return engine

    def step_ticks(self, engine, n_steps):
        """Intervalos en ticks entre avances consecutivos de la phrase."""
        advances = []
        prev = engine.channels[0].phrase_pos
        for tick in range(1, 128):
            engine._process_tick()
            pos = engine.channels[0].phrase_pos
            if pos != prev:
                advances.append(tick)
                prev = pos
                if len(advances) >= n_steps + 1:
                    break
        return [b - a for a, b in zip(advances, advances[1:])]

    def test_default_groove_is_straight(self):
        engine = make_engine()
        self.assertEqual(self.step_ticks(engine, 4), [6, 6, 6, 6])

    def test_swing_groove(self):
        # Patrón [3, 1]: el avance N ocurre al terminar el slot N, así que
        # los intervalos son el patrón rotado: [1, 3, 1, 3, ...]
        engine = self.make_groove_engine(bytearray([3, 1]))
        self.assertEqual(self.step_ticks(engine, 5), [1, 3, 1, 3, 1])

    def test_bulebule_groove(self):
        # Groove real de Bulebule: [7, 5, 6, 5] (rotado por fase: empieza
        # en el segundo slot)
        engine = self.make_groove_engine(bytearray([7, 5, 6, 5]))
        self.assertEqual(self.step_ticks(engine, 5), [5, 6, 5, 7, 5])

    def test_grov_command(self):
        engine = make_engine()
        engine.project.cmd1[0] = "GROV"
        engine.project.param1[0] = 1
        engine.groove_data[16] = 3         # groove 1 = [3, 3]
        engine.groove_data[17] = 3
        engine._process_tick()             # procesa el GROV de la fila 0
        ch = engine.channels[0]
        self.assertEqual(ch.groove, 1)
        self.assertEqual(ch.g_ticks, 3)


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


    def test_bulebule_groove_timing(self):
        # El groove 0 de Bulebule es [7,5,6,5]: los intervalos entre steps
        # deben seguir ese patrón
        engine = Engine(SONGS_DIR / "lgpt_Bulebule")
        engine.start()
        self.assertEqual(list(engine.groove_data[:4]), [7, 5, 6, 5])
        ch = engine.channels[0]
        advances = []
        prev = ch.phrase_pos
        for tick in range(1, 400):
            engine._process_tick()
            if ch.phrase_pos != prev:
                advances.append(tick)
                prev = ch.phrase_pos
                if len(advances) >= 6:
                    break
        intervals = [b - a for a, b in zip(advances, advances[1:])]
        self.assertEqual(intervals, [5, 6, 5, 7, 5])


if __name__ == "__main__":
    unittest.main()
