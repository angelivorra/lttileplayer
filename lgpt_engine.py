#!/usr/bin/env python3
"""
Motor de audio para proyectos LittleGPTracker (LGPT).

Réplica en Python/numpy del comportamiento del reproductor original
(LittleGPTracker/sources), limitada a lo que usan las canciones del proyecto:

  - Secuenciador song -> chain -> phrase, ticks sample-accurate y
    grooves por canal (patrones de longitud de step en ticks, GROV).
  - Voces de sample: pitch por nota (root note + fine tune), volumen con
    rampas (VOLM), pan con la panlaw original, loop oneshot/forward,
    recorte por 'end', crush/downsample, filtro del upstream.
  - Comandos: VOLM, KILL, DLAY, LEGA, TABL, STOP, HOP.
  - Tablas (1 fila por tick, 3 columnas de comandos).
  - Instrumentos MIDI (0x80-0x8F) y comandos MDCC/MDPG/MVEL: se emiten a
    un sink MidiOut (puerto MIDI real en el reproductor).
  - Modificadores en directo por canal (para MIDI CC): volumen, pan,
    pitch y cutoff.

El motor no depende de sounddevice: render() devuelve bloques float32
estéreo y sirve tanto para tests headless como para un callback de audio.
Todos los eventos externos entran por una cola y se aplican en el hilo
que llama a render(): no hay locks en el camino de audio.
"""

from __future__ import annotations

import math
import queue
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

from lgpt_parser import LGPTProject

SAMPLE_RATE = 44100
CHANNEL_COUNT = 8
TICKS_PER_STEP = 6          # AUDIO_SLICES_PER_STEP del upstream
KRATE = 100                 # KRATE_SAMPLE_COUNT del upstream

# Pan law original (LittleGPTracker/sources/Application/Instruments/
# SampleInstrumentDatas.h), 255 entradas en punto fijo 16.16.
# Gain L = panlaw[pan] / 65536, gain R = panlaw[254 - pan] / 65536.
_PANLAW_HEX = (
    0x0000, 0x0808, 0x0B5B, 0x0DE9, 0x1010, 0x11F5, 0x13AC, 0x153F, 0x16B7, 0x1818, 0x1965, 0x1AA3, 0x1BD2, 0x1CF5, 0x1E0D, 0x1F1B, 0x2020,
    0x211D, 0x2213, 0x2302, 0x23EA, 0x24CD, 0x25AB, 0x2684, 0x2758, 0x2828, 0x28F3, 0x29BB, 0x2A7F, 0x2B40, 0x2BFD, 0x2CB7, 0x2D6E, 0x2E23,
    0x2ED4, 0x2F83, 0x3030, 0x30DA, 0x3182, 0x3228, 0x32CB, 0x336D, 0x340C, 0x34AA, 0x3546, 0x35E0, 0x3678, 0x370F, 0x37A4, 0x3838, 0x38CA,
    0x395B, 0x39EA, 0x3A78, 0x3B04, 0x3B90, 0x3C1A, 0x3CA2, 0x3D2A, 0x3DB0, 0x3E36, 0x3EBA, 0x3F3D, 0x3FBF, 0x4040, 0x40C0, 0x413F, 0x41BD,
    0x423A, 0x42B6, 0x4332, 0x43AC, 0x4426, 0x449E, 0x4516, 0x458D, 0x4604, 0x4679, 0x46EE, 0x4762, 0x47D5, 0x4848, 0x48BA, 0x492B, 0x499B,
    0x4A0B, 0x4A7A, 0x4AE9, 0x4B57, 0x4BC4, 0x4C31, 0x4C9D, 0x4D08, 0x4D73, 0x4DDE, 0x4E47, 0x4EB1, 0x4F19, 0x4F81, 0x4FE9, 0x5050, 0x50B7,
    0x511D, 0x5182, 0x51E7, 0x524C, 0x52B0, 0x5313, 0x5377, 0x53D9, 0x543C, 0x549D, 0x54FF, 0x5560, 0x55C0, 0x5620, 0x5680, 0x56DF, 0x573E,
    0x579C, 0x57FA, 0x5858, 0x58B5, 0x5912, 0x596F, 0x59CB, 0x5A27, 0x5A82, 0x5ADD, 0x5B38, 0x5B92, 0x5BEC, 0x5C46, 0x5C9F, 0x5CF8, 0x5D51,
    0x5DA9, 0x5E01, 0x5E59, 0x5EB0, 0x5F07, 0x5F5E, 0x5FB4, 0x600A, 0x6060, 0x60B6, 0x610B, 0x6160, 0x61B4, 0x6209, 0x625D, 0x62B1, 0x6304,
    0x6357, 0x63AA, 0x63FD, 0x6450, 0x64A2, 0x64F4, 0x6545, 0x6597, 0x65E8, 0x6639, 0x6689, 0x66DA, 0x672A, 0x677A, 0x67C9, 0x6819, 0x6868,
    0x68B7, 0x6906, 0x6954, 0x69A3, 0x69F1, 0x6A3E, 0x6A8C, 0x6AD9, 0x6B27, 0x6B74, 0x6BC0, 0x6C0D, 0x6C59, 0x6CA5, 0x6CF1, 0x6D3D, 0x6D88,
    0x6DD4, 0x6E1F, 0x6E69, 0x6EB4, 0x6EFF, 0x6F49, 0x6F93, 0x6FDD, 0x7027, 0x7070, 0x70B9, 0x7103, 0x714C, 0x7194, 0x71DD, 0x7225, 0x726E,
    0x72B6, 0x72FE, 0x7345, 0x738D, 0x73D4, 0x741B, 0x7462, 0x74A9, 0x74F0, 0x7537, 0x757D, 0x75C3, 0x7609, 0x764F, 0x7695, 0x76DA, 0x7720,
    0x7765, 0x77AA, 0x77EF, 0x7834, 0x7878, 0x78BD, 0x7901, 0x7945, 0x7989, 0x79CD, 0x7A11, 0x7A54, 0x7A98, 0x7ADB, 0x7B1E, 0x7B61, 0x7BA4,
    0x7BE7, 0x7C29, 0x7C6C, 0x7CAE, 0x7CF0, 0x7D32, 0x7D74, 0x7DB6, 0x7DF7, 0x7E39, 0x7E7A, 0x7EBB, 0x7EFC, 0x7F3D, 0x7F7E, 0x7FBF, 0x8000,
)
PANLAW = np.asarray(_PANLAW_HEX, dtype=np.float32) / 65536.0


# --------------------------------------------------------------------------
# Carga de samples e instrumentos
# --------------------------------------------------------------------------

@dataclass
class Sample:
    data: np.ndarray      # (n, canales) float32
    sr: int


class SampleBank:
    """Carga los WAV del directorio samples/ de un proyecto."""

    def __init__(self, project_dir: Path):
        self.samples: dict[str, Sample] = {}
        sample_dir = project_dir / "samples"
        if not sample_dir.is_dir():
            return
        for wav in sorted(sample_dir.glob("*.wav")):
            try:
                data, sr = sf.read(str(wav), dtype="float32", always_2d=True)
            except Exception as exc:  # WAV ilegible: se ignora con aviso
                print(f"[engine] no se puede cargar {wav.name}: {exc}")
                continue
            self.samples[wav.name] = Sample(np.ascontiguousarray(data), sr)

    def get(self, name: str) -> Optional[Sample]:
        return self.samples.get(name)


@dataclass
class InstrumentDef:
    """Parámetros normalizados de un instrumento de tipo Sample."""

    index: int
    sample_name: str
    volume: int = 0x80          # 0-255
    pan: int = 0x7F             # 0-254
    root_note: int = 60
    fine_tune: float = 0.0      # semitonos
    loop: bool = False
    start: int = 0
    end: int = 0                # 0 => longitud completa
    crush: int = 16             # bits; 16 = sin crush
    drive: int = 0xFF
    downsample: int = 0         # 0 = off; n => hold de 2^n
    cutoff: int = 0xFF
    reso: int = 0
    filter_mix: int = 0         # "filter type" 0-255
    filter_scream: bool = False
    attenuate: int = 0xFF
    table: int = -1

    @property
    def filtering(self) -> bool:
        """Regla del upstream: el filtro solo actúa si cut<255 o res>0."""
        return self.cutoff < 0xFF or self.reso > 0


def parse_instrument(index: int, params: dict) -> InstrumentDef:
    def int_or(name: str, default: int) -> int:
        try:
            return int(params.get(name, default))
        except (TypeError, ValueError):
            return default

    fine = (int_or("fine tune", 0x7F) - 0x7F) / 0x80
    return InstrumentDef(
        index=index,
        sample_name=params.get("sample", ""),
        volume=int_or("volume", 0x80),
        pan=int_or("pan", 0x7F),
        root_note=int_or("root note", 60),
        fine_tune=fine,
        loop=params.get("loopmode", "none") == "loop",
        start=int_or("start", 0),
        end=int_or("end", 0),
        crush=int_or("crush", 16),
        drive=int_or("crushdrive", 0xFF),
        downsample=int_or("downsample", 0),
        cutoff=int_or("filter cut", 0xFF),
        reso=int_or("filter res", 0),
        filter_mix=int_or("filter type", 0),
        filter_scream=params.get("filter mode", "original") == "scream",
        attenuate=int_or("attenuate", 0xFF),
        table=int_or("table", -1),
    )


@dataclass
class MidiDef:
    """Parámetros de un instrumento MIDI (0x80-0x8F) del banco."""

    index: int
    channel: int = 0            # canal MIDI 0-15
    note_length: int = 0        # ticks; 0 = hasta note off
    volume: int = 255
    table: int = -1


def parse_midi_instrument(index: int, params: dict) -> MidiDef:
    def int_or(name: str, default: int) -> int:
        try:
            return int(params.get(name, default))
        except (TypeError, ValueError):
            return default

    return MidiDef(
        index=index,
        channel=int_or("channel", 0) & 0x0F,
        note_length=int_or("note length", 0),
        volume=int_or("volume", 255),
        table=int_or("table", -1),
    )


class MidiOut:
    """Sink de eventos MIDI del engine. El player lo conecta a un puerto
    real; en tests se usa un colector. Todos los métodos reciben el canal
    MIDI (0-15) del instrumento."""

    def note_on(self, channel: int, note: int, velocity: int):
        pass

    def note_off(self, channel: int, note: int):
        pass

    def cc(self, channel: int, control: int, value: int):
        pass

    def program_change(self, channel: int, program: int):
        pass


# --------------------------------------------------------------------------
# Voz de sample
# --------------------------------------------------------------------------

class Voice:
    """Estado de reproducción de un sample en un canal (monofonía por canal).

    Réplica del render de SampleInstrument.cpp: interpolación lineal,
    downsample (cuantización de la posición de lectura), crush con predrive,
    volumen con rampa k-rate (VOLM), glide logarítmico (LEGA), filtro LP
    del upstream, attenuate y pan con la panlaw original.

    Las rampas se actualizan cada KRATE=100 samples como en el original;
    dentro de un bloque se calculan de forma analítica (escalera) con numpy.
    """

    __slots__ = (
        "data", "n_channels", "note", "pos", "end", "loop",
        "loop_start", "loop_end", "loop_len",
        "base_speed", "cc_pitch",
        "lega_ratio", "lega_target", "lega_step",
        "vol_cur", "vol_target", "vol_kinc",
        "pan", "cc_vol", "cc_pan", "cc_cutoff",
        "crush", "drive_gain", "ds_shift", "attenuate",
        "f_active", "f_mix", "f_scream", "f_cut_base", "f_reso_base",
        "f_speed", "f_height", "f_delay",
        "k_rem", "active", "_samples_per_tick",
    )

    def __init__(self, sample: Sample, idef: InstrumentDef, note: int,
                 out_sr: int, samples_per_tick: float):
        self.data = sample.data
        self.n_channels = sample.data.shape[1]
        self.note = note
        n = len(sample.data)
        self.end = min(idef.end, n) if 0 < idef.end else n
        self.end = max(self.end, 2)
        self.loop = idef.loop
        self.loop_start = min(idef.start if idef.start > 0 else 0, self.end - 1)
        # upstream usa loopstart=0 y 'end' como fin de la ventana de loop
        self.loop_end = self.end
        self.loop_len = max(self.loop_end - self.loop_start, 1)
        self.pos = float(min(idef.start, self.end - 1))

        semis = (note - idef.root_note) + idef.fine_tune
        self.base_speed = (sample.sr / out_sr) * 2.0 ** (semis / 12.0)
        self.cc_pitch = 1.0           # multiplicador (MIDI CC)

        self.lega_ratio = 1.0         # glide actual (multiplicador de speed)
        self.lega_target = 1.0
        self.lega_step: Optional[float] = None  # None = glide inactivo

        self.vol_cur = float(idef.volume)     # dominio 0-255
        self.vol_target = float(idef.volume)
        self.vol_kinc = 0.0                   # incremento por k-update

        self.pan = min(idef.pan, 254)
        self.cc_vol = 1.0             # 0-1 (MIDI CC)
        self.cc_pan: Optional[int] = None     # 0-254 o None (MIDI CC)
        self.cc_cutoff = 1.0          # multiplicador de cutoff (MIDI CC)

        self.crush = idef.crush
        self.drive_gain = idef.drive / 255.0
        self.ds_shift = idef.downsample
        self.attenuate = idef.attenuate / 255.0

        self.f_active = idef.filtering
        self.f_mix = idef.filter_mix / 255.0
        self.f_scream = idef.filter_scream
        self.f_cut_base = idef.cutoff / 255.0
        self.f_reso_base = idef.reso / 255.0
        self.f_speed = [0.0] * self.n_channels
        self.f_height = [0.0] * self.n_channels
        self.f_delay = [0.0] * self.n_channels

        self.k_rem = KRATE            # samples hasta el próximo k-update
        self.active = True

        self._samples_per_tick = samples_per_tick

    # -- comandos ---------------------------------------------------------

    def set_volm(self, value: int):
        """VOLM ssvv: rampa de volumen hacia vv; ss en unidades de 4 ticks."""
        target = float(value & 0xFF)
        ss = value >> 8
        if ss == 0:
            self.vol_cur = self.vol_target = target
            self.vol_kinc = 0.0
        else:
            ramp_samples = ss * 4.0 * self._samples_per_tick
            self.vol_kinc = (target - self.vol_cur) * KRATE / ramp_samples
            self.vol_target = target

    def set_lega(self, value: int, last_note: int):
        """LEGA sspp: glide logarítmico. pp=0 => glide desde la última nota."""
        pitch = value & 0xFF
        if pitch > 127:
            pitch -= 256
        ss = value >> 8
        if pitch == 0:
            init = 2.0 ** ((last_note - self.note) / 12.0)
            target = 1.0
        else:
            init = self.lega_ratio
            target = 2.0 ** (pitch / 12.0)
        self.lega_ratio = init
        self.lega_target = target
        if ss == 0 or init == target:
            self.lega_ratio = target
            self.lega_step = None
        else:
            step = 1.0 + 0.5 / ss
            self.lega_step = step if target > init else 1.0 / step

    # -- render ------------------------------------------------------------

    def _kupdates(self, n: int) -> np.ndarray:
        """Nº de k-updates completados en cada sample del bloque (escalera)."""
        i = np.arange(n)
        return np.maximum((i - self.k_rem) // KRATE + 1, 0)

    def render(self, mix: np.ndarray, off: int, n: int):
        if not self.active or n <= 0:
            return

        updates = self._kupdates(n)

        # Speed por sample (constante salvo glide LEGA activo)
        if self.lega_step is not None:
            ratio = self.lega_ratio * self.lega_step ** updates
            if self.lega_step > 1.0:
                ratio = np.minimum(ratio, self.lega_target)
            else:
                ratio = np.maximum(ratio, self.lega_target)
            speed = self.base_speed * self.cc_pitch * ratio
            pos_arr = self.pos + np.cumsum(speed) - speed[0]
            self.lega_ratio = float(ratio[-1])
            if ratio[-1] == self.lega_target:
                self.lega_step = None
            end_pos = pos_arr[-1] + speed[-1]
        else:
            speed = self.base_speed * self.cc_pitch * self.lega_ratio
            pos_arr = self.pos + speed * np.arange(n)
            end_pos = self.pos + speed * n

        # Posición de lectura (con loop forward si aplica)
        if self.loop:
            rel = pos_arr - self.loop_start
            read = self.loop_start + np.mod(rel, self.loop_len)
        else:
            read = pos_arr
        i0 = read.astype(np.int64)
        frac = (read - i0).astype(np.float32)
        i0_raw = i0
        if self.ds_shift:
            i0 = i0 & ~((1 << self.ds_shift) - 1)
        i1 = i0 + 1
        if self.loop:
            i1 = np.where(i1 >= self.loop_end, self.loop_start, i1)
        size = len(self.data)
        np.clip(i0, 0, size - 1, out=i0)
        np.clip(i1, 0, size - 1, out=i1)

        x = self.data[i0] * (1.0 - frac)[:, None] + self.data[i1] * frac[:, None]
        if not self.loop:
            x[i0_raw >= self.end - 1] = 0.0

        # Crush (predrive + reducción de bits), dominio float [-1,1] ~ 16 bits
        x *= self.drive_gain
        if self.crush < 16:
            step = 2.0 ** (1 - self.crush)
            x = np.round(x / step) * step

        # Volumen con rampa k-rate (dominio 0-255 -> 0-1)
        v = self.vol_cur + self.vol_kinc * updates
        if self.vol_kinc > 0.0:
            v = np.minimum(v, self.vol_target)
        elif self.vol_kinc < 0.0:
            v = np.maximum(v, self.vol_target)
        x *= (v * (1.0 / 255.0) * self.cc_vol)[:, None]

        # Filtro del upstream (bucle por sample; solo voces filtradas)
        if self.f_active:
            self._render_filter(x)

        x *= self.attenuate

        # Pan con la panlaw original y mezcla
        pan = self.cc_pan if self.cc_pan is not None else self.pan
        pan = min(max(int(pan), 0), 254)
        gl = PANLAW[pan]
        gr = PANLAW[254 - pan]
        mix[off:off + n, 0] += x[:, 0] * gl
        if self.n_channels == 1:
            mix[off:off + n, 1] += x[:, 0] * gr
        else:
            mix[off:off + n, 1] += x[:, 1] * gr

        # Actualización de estado
        self.pos = end_pos
        self.vol_cur = float(v[-1])
        self.k_rem -= n
        while self.k_rem <= 0:
            self.k_rem += KRATE
        if not self.loop and self.pos >= self.end - 1:
            self.active = False

    def _render_filter(self, x: np.ndarray):
        """Filtro LP del upstream (Filters.cpp + bucle inline de
        SampleInstrument.cpp), replicado en float.

        Diferencia: el original usa punto fijo int32 que desborda y envuelve
        en modo 'scream'; aquí se satura height a [-2,2] para evitar la
        explosión numérica. Aproximación pendiente de validación a oído.
        """
        cut = self.f_cut_base * self.cc_cutoff
        cut = min(max(cut, 0.0), 1.0)
        freq = cut * cut
        reso = 1.0 - (1.0 - self.f_reso_base) ** 3
        dirt = 100.0 * (1.0 - cut) + 5000.0 * cut
        mix_inv = 1.0 - self.f_mix
        f_mix = self.f_mix
        scream = self.f_scream
        n = x.shape[0]
        for c in range(self.n_channels):
            xs = x[:, c]
            sp = self.f_speed[c]
            hg = self.f_height[c]
            dl = self.f_delay[c]
            for i in range(n):
                s = xs[i]
                lpin = s * mix_inv
                hpin = -s * f_mix
                if scream:
                    if sp > 1.0:
                        sp = 2.0 / 3.0
                    elif sp < -1.0:
                        sp = -2.0 / 3.0
                    sp *= dirt
                sp = sp * reso + (lpin - hg) * freq
                hg = hg + sp + dl - hpin
                if hg > 2.0:
                    hg = 2.0
                elif hg < -2.0:
                    hg = -2.0
                dl = hpin
                xs[i] = hg
            self.f_speed[c] = sp
            self.f_height[c] = hg
            self.f_delay[c] = dl


# --------------------------------------------------------------------------
# Tablas
# --------------------------------------------------------------------------

class TablePlayback:
    """Reproducción de una tabla en un canal (TablePlayback.cpp del upstream).

    Avanza una fila por tick en cada una de las 3 columnas de comandos.
    HOP posicional por columna con contador (param alto = repeticiones,
    param bajo = fila destino). Groove propio por tabla (por defecto el
    groove 255 = un paso por tick; se cambia con GROV en la tabla).
    """

    _COLS = (("cmd1", "param1"), ("cmd2", "param2"), ("cmd3", "param3"))

    __slots__ = ("table", "pos", "hop_count", "hopped", "active",
                 "groove", "g_pos", "g_ticks")

    def __init__(self):
        self.table: Optional[dict] = None
        self.pos = [0, 0, 0]
        self.hop_count = [[0] * 16 for _ in range(3)]
        self.hopped = [False, False, False]
        self.active = False
        # Estado de groove de la tabla (groove_=255 = un paso por tick)
        self.groove = 0xFF
        self.g_pos = 0
        self.g_ticks = 0

    def start(self, table: dict):
        self.table = table
        self.pos = [0, 0, 0]
        self.hop_count = [[0] * 16 for _ in range(3)]
        self.groove = 0xFF
        self.g_pos = 0
        self.g_ticks = 0
        self.active = True

    def stop(self):
        self.active = False
        self.table = None

    def _update_groove(self, groove_data) -> bool:
        """Groove::UpdateGroove(reverse=true): True si toca avanzar fila."""
        self.g_ticks += 1
        if self.groove == 0xFF:
            if self.g_ticks == 1:
                self.g_ticks = 0
                return True
            return False
        if self.g_ticks == groove_data[self.groove * 16 + self.g_pos]:
            self.g_pos = (self.g_pos + 1) % 16
            if groove_data[self.groove * 16 + self.g_pos] == 0xFF:
                self.g_pos = 0
            self.g_ticks = 0
            return True
        return False

    def step(self, ch: "Channel", engine: "Engine"):
        if not self.active or self.table is None:
            return
        t = self.table
        if self.g_ticks == 0:
            for c, (ck, pk) in enumerate(self._COLS):
                cmds = t[ck]
                params = t[pk]
                cmd = cmds[self.pos[c]]
                param = params[self.pos[c]]
                hopped = False
                if cmd == "HOP ":
                    count = param >> 8
                    if self.hop_count[c][self.pos[c]] == 0:
                        self.hop_count[c][self.pos[c]] = count
                    else:
                        self.hop_count[c][self.pos[c]] -= 1
                    if self.hop_count[c][self.pos[c]] != 0 or count == 0:
                        self.pos[c] = param & 0xF
                    else:
                        self.pos[c] = (self.pos[c] + 1) % 16
                    hopped = True
                    cmd = cmds[self.pos[c]]
                    param = params[self.pos[c]]
                self.hopped[c] = hopped
                if cmd == "KILL":
                    ch.time_to_live = (param & 0xFF) + 1
                elif cmd == "GROV":
                    self.groove = param & 0x1F
                    self.g_pos = 0
                    self.g_ticks = 0
                elif cmd not in ("----", "HOP "):
                    engine._instrument_command(ch, cmd, param)
        if self._update_groove(engine.groove_data):
            for c, (ck, _pk) in enumerate(self._COLS):
                if t[ck][self.pos[c]] != "HOP " or not self.hopped[c]:
                    self.pos[c] = (self.pos[c] + 1) % 16
                self.hopped[c] = False


# --------------------------------------------------------------------------
# Presets de efectos (cadena por canal, a la salida del delay)
# --------------------------------------------------------------------------

class SuboctaveFx:
    """Audio Divider: suboctava del bajo (denominador 1 -> 4 al subir)."""

    def __init__(self, sr: int):
        try:
            from ladspa_fx import LadspaStereoDivider
            self.plugin = LadspaStereoDivider(sr)
        except Exception:
            self.plugin = None

    def apply(self, buf: np.ndarray, amount: float):
        if self.plugin is not None:
            self.plugin.set(1.0 + amount * 3.0)
            self.plugin.run(buf)
        else:
            np.tanh(buf * (1.0 + 20.0 * amount), out=buf)


class AcidLpFx:
    """C* AutoFilter LP: barrido 3800 -> 780 Hz (50% log) con resonancia
    y compensación de volumen progresiva hasta +40%."""

    COMP = 0.40

    def __init__(self, sr: int):
        self.sr = sr
        self.plugin = None
        for cls in ("LadspaStereoAutoFilter", "LadspaStereoSVF"):
            try:
                mod = __import__("ladspa_fx")
                self.plugin = getattr(mod, cls)(sr)
                break
            except Exception:
                continue
        self.coef: Optional[tuple] = None
        self.state = [0.0] * 8

    def apply(self, buf: np.ndarray, amount: float):
        freq = 3800.0 * (160.0 / 3800.0) ** (amount * 0.5)
        res = 0.85 * amount * 0.5
        if self.plugin is not None:
            self.plugin.set(freq_hz=freq, res=res)
            self.plugin.run(buf)
        else:
            self._biquad(buf, amount, freq, res)
        buf *= 1.0 + self.COMP * amount

    def _biquad(self, buf: np.ndarray, cut: float, freq: float, res: float):
        if self.coef is None or self.coef[0] != cut or self.coef[1] != res:
            q = 0.5 + res * 9.5
            w0 = 2.0 * math.pi * freq / self.sr
            alpha = math.sin(w0) / (2.0 * q)
            cosw = math.cos(w0)
            a0 = 1.0 + alpha
            b0 = (1.0 - cosw) * 0.5 / a0
            b1 = (1.0 - cosw) / a0
            a1 = -2.0 * cosw / a0
            a2 = (1.0 - alpha) / a0
            self.coef = (cut, res, b0, b1, b0, a1, a2)
        _c, _r, b0, b1, b2, a1, a2 = self.coef
        st = self.state
        for side in range(2):
            x1, x2, y1, y2 = st[side * 4:side * 4 + 4]
            xs = buf[:, side]
            for i in range(len(xs)):
                x = xs[i]
                y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
                x2, x1 = x1, x
                y2, y1 = y1, y
                if y > 4.0:
                    y = 4.0
                elif y < -4.0:
                    y = -4.0
                xs[i] = y
            st[side * 4:side * 4 + 4] = [x1, x2, y1, y2]


class SatanFx:
    """Barry's Satan Maximiser: knee 0 -> -60 dB (destrucción progresiva)."""

    KNEE_MAX = -60.0

    def __init__(self, sr: int):
        try:
            from ladspa_fx import LadspaStereoSatan
            self.plugin = LadspaStereoSatan(sr)
        except Exception:
            self.plugin = None

    def apply(self, buf: np.ndarray, amount: float):
        if self.plugin is not None:
            self.plugin.set(self.KNEE_MAX * amount)
            self.plugin.run(buf)
        else:
            np.tanh(buf * (1.0 + 30.0 * amount), out=buf)


# Presets disponibles para los pots (target = "canal:nombre"). El orden
# de este dict es el orden de la cadena: suboctava/drive -> filtro.
EFFECT_PRESETS = {
    "suboctave": SuboctaveFx,
    "satan": SatanFx,
    "acid_lp": AcidLpFx,
}


# --------------------------------------------------------------------------
# Canal del secuenciador
# --------------------------------------------------------------------------

class Channel:
    __slots__ = (
        "idx", "song_pos", "chain_pos", "phrase_pos", "chain", "phrase",
        "playing", "time_to_start", "time_to_live", "voice",
        "last_instr", "last_note", "table",
        "cc_vol", "cc_pan", "cc_pitch", "cc_cutoff",
        "kind", "midi_def", "midi_note", "midi_ticks", "midi_vel",
        "groove", "g_pos", "g_ticks",
        "fx_amounts", "fx_objs",
    )

    def __init__(self, idx: int):
        self.idx = idx
        self.song_pos = 0
        self.chain_pos = 0
        self.phrase_pos = 0
        self.chain = 0xFF
        self.phrase = 0xFF
        self.playing = False
        self.time_to_start = 0
        self.time_to_live = 0
        self.voice: Optional[Voice] = None
        self.last_instr: Optional[int] = None
        self.last_note = 0
        self.table = TablePlayback()
        # Modificadores en directo (MIDI CC); se copian a la voz en render
        self.cc_vol = 1.0
        self.cc_pan: Optional[int] = None
        self.cc_pitch = 1.0
        self.cc_cutoff = 1.0
        # Estado del instrumento MIDI activo en el canal (si lo hay)
        self.kind: Optional[str] = None        # None | "sample" | "midi"
        self.midi_def: Optional[MidiDef] = None
        self.midi_note: Optional[int] = None   # nota sonando (None = off)
        self.midi_ticks = -1                   # cuenta atrás de note length
        self.midi_vel: Optional[int] = None    # velocity (MVEL)
        # Estado de groove del canal (Groove::ChannelGroove del upstream)
        self.groove = 0                        # groove seleccionado (GROV)
        self.g_pos = 0                         # paso dentro del groove
        self.g_ticks = 6                       # cuenta atrás de ticks del paso
        # Efectos live del canal (presets LADSPA): nombre -> cantidad 0-1
        self.fx_amounts: dict[str, float] = {}
        self.fx_objs: dict[str, object] = {}


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class Engine:
    """Secuenciador + mixer. Todo el estado se muta dentro de render().

    Uso:
        engine = Engine(Path("lgpt_cancion"))
        engine.start()
        block = engine.render(512)        # (512, 2) float32
        engine.push_event("cc", 0, 7, 100)  # volumen canal 0
    """

    def __init__(self, project, sample_rate: int = SAMPLE_RATE,
                 audio_delay: float = 0.0):
        if not isinstance(project, LGPTProject):
            project = LGPTProject(Path(project))
        if project.root is None:
            project.load()
        self.project = project
        self.sr = sample_rate
        self.tempo = int(project.project.get("tempo", "125"))
        self.master = int(project.project.get("master", "100")) / 100.0
        self.transpose = int(project.project.get("transpose", "0"))
        # SyncMaster::SetTempo del upstream
        self.samples_per_tick = (
            60.0 * self.sr * 2.0 / self.tempo / 8.0 / TICKS_PER_STEP
        )
        self.bank = SampleBank(project.dir)
        self.instruments = {
            iid: parse_instrument(iid, ins["params"])
            for iid, ins in project.instrument_bank.items()
            if ins["type"] == "Sample"
        }
        self.midi_instruments = {
            iid: parse_midi_instrument(iid, ins["params"])
            for iid, ins in project.instrument_bank.items()
            if ins["type"] == "Midi"
        }
        # Sink de eventos MIDI (instrumentos MIDI, MDCC/MDPG); lo asigna
        # el reproductor. None = no se emite nada.
        self.midi_out: Optional[MidiOut] = None
        # Datos de groove: 0x20 grooves x 16 pasos (0xFF = fin de patrón).
        # Si el proyecto no trae datos, patrón recto [6, 6].
        g = project.grooves
        if len(g) < 0x20 * 16:
            g = bytearray([0xFF] * (0x20 * 16))
            for i in range(0x20):
                g[i * 16] = 6
                g[i * 16 + 1] = 6
        self.groove_data = g
        self.channels = [Channel(i) for i in range(CHANNEL_COUNT)]
        self.tick_count = 0
        self.tick_phase = 0.0           # samples hasta el próximo tick
        self.playing = False
        self.finished = False           # True al recibir STOP
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.unsupported_cmds: set[str] = set()
        self.muted: set[int] = set()    # canales silenciados (índice 0-7)
        # Delay de audio POR CANAL (segundos): el secuenciador y los
        # eventos MIDI van en tiempo real (t=0); el audio sale retrasado.
        # La modulación del controlador (vol/pan/drive/LP) se aplica a la
        # salida del delay (t+1), en tiempo real para quien escucha.
        self._stage: dict[int, np.ndarray] = {}     # render t=0 por canal
        self._rings: list[Optional[np.ndarray]] = [None] * CHANNEL_COUNT
        self._ring_pos = [0] * CHANNEL_COUNT
        self.set_audio_delay(audio_delay)

    def set_audio_delay(self, seconds: float):
        """Configura el retardo de la salida de audio (0 = sin delay)."""
        n = int(seconds * self.sr)
        self._rings = [
            np.zeros((n, 2), dtype=np.float32) if n > 0 else None
            for _ in range(CHANNEL_COUNT)
        ]
        self._ring_pos = [0] * CHANNEL_COUNT

    # -- transporte ---------------------------------------------------------

    def start(self):
        """(Re)inicia la canción desde el principio."""
        for ch in self.channels:
            ch.playing = False
            ch.voice = None
            self._midi_stop_note(ch)
            ch.kind = None
            ch.midi_def = None
            ch.midi_ticks = -1
            ch.midi_vel = None
            ch.table.stop()
            ch.time_to_start = 0
            ch.time_to_live = 0
            ch.last_instr = None
            ch.groove = 0
            ch.g_pos = 0
            ch.g_ticks = self._groove_len(0, 0)
            for pos in range(256):
                if self._is_playable(pos, ch.idx):
                    ch.playing = True
                    self._set_song_pos(ch, pos, 0, -1)
                    break
        self.tick_count = 0
        self.tick_phase = 0.0
        self.finished = False
        self.playing = True
        self.unsupported_cmds.clear()

    def panic(self):
        """Note off de todas las notas MIDI activas (al cambiar de canción
        o salir)."""
        for ch in self.channels:
            self._midi_stop_note(ch)

    def push_event(self, *event):
        """Encola un evento externo (MIDI, teclado). Thread-safe."""
        self.events.put(event)

    # -- render --------------------------------------------------------------

    def render(self, frames: int) -> np.ndarray:
        """Renderiza `frames` samples estéreo float32.

        Arquitectura de tiempos:
          - t=0: secuenciador y eventos MIDI de LGPT, en tiempo real.
          - Las voces se renderizan a un buffer por canal y entran en la
            línea de retardo del canal (`audio_delay` segundos).
          - t+1: el audio sale del delay y AHÍ se aplica la modulación
            del controlador (volumen, pan, drive, LP), así que los pots
            se oyen al instante. El pitch se aplica en la voz (t=0).
        """
        self._drain_events()
        # 1. t=0: render de voces por canal
        for ch in self.channels:
            buf = self._stage.get(ch.idx)
            if buf is None or len(buf) < frames:
                buf = np.zeros((max(frames, 1024), 2), dtype=np.float32)
                self._stage[ch.idx] = buf
            else:
                buf[:frames] = 0.0
        if self.playing:
            off = 0
            while off < frames and self.playing:
                n = min(frames - off, int(self.tick_phase))
                if n > 0:
                    for ch in self.channels:
                        if ch.idx in self.muted:
                            continue
                        v = ch.voice
                        if v is not None:
                            if v.active:
                                v.cc_vol = 1.0       # vol/pan del controlador
                                v.cc_pan = None      # van tras el delay
                                v.cc_pitch = ch.cc_pitch
                                v.cc_cutoff = ch.cc_cutoff
                                v.render(self._stage[ch.idx], off, n)
                            if not v.active:
                                ch.voice = None
                    off += n
                    self.tick_phase -= n
                if self.tick_phase < 1.0:
                    frac = self.tick_phase    # resto fraccionario ya consumido
                    self._process_tick()
                    self.tick_phase = self.samples_per_tick + frac
        # 2. t+1: salida del delay, efectos del controlador y mezcla
        out = np.zeros((frames, 2), dtype=np.float32)
        for ch in self.channels:
            block = self._delay_channel(ch, self._stage[ch.idx][:frames])
            for name, cls in EFFECT_PRESETS.items():
                amount = ch.fx_amounts.get(name, 0.0)
                if amount > 0.001:
                    fx = ch.fx_objs.get(name)
                    if fx is None:
                        fx = cls(self.sr)
                        ch.fx_objs[name] = fx
                    fx.apply(block, amount)
            if ch.cc_vol != 1.0:
                block *= ch.cc_vol
            if ch.cc_pan is not None:
                x = ch.cc_pan / 254.0
                block[:, 0] *= min(1.0, 2.0 * (1.0 - x))
                block[:, 1] *= min(1.0, 2.0 * x)
            out += block
        out *= self.master
        np.clip(out, -1.0, 1.0, out=out)
        return out

    def _delay_channel(self, ch: Channel, block: np.ndarray) -> np.ndarray:
        """Línea de retardo circular del canal; devuelve el bloque
        retrasado (copia nueva) o el propio `block` si no hay delay."""
        ring = self._rings[ch.idx]
        if ring is None:
            return block
        frames = len(block)
        d = len(ring)
        pos = self._ring_pos[ch.idx]
        result = np.empty_like(block)
        if frames <= d - pos:
            result[:] = ring[pos:pos + frames]
            ring[pos:pos + frames] = block
        else:
            k = d - pos
            result[:k] = ring[pos:]
            ring[pos:] = block[:k]
            result[k:] = ring[:frames - k]
            ring[:frames - k] = block[k:]
        self._ring_pos[ch.idx] = (pos + frames) % d
        return result

    # -- eventos externos -----------------------------------------------------

    def _drain_events(self):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            kind = ev[0]
            if kind == "cc":
                self._apply_cc(ev[1], ev[2], ev[3])
            elif kind == "param":
                self._apply_param(ev[1], ev[2], ev[3])
            elif kind == "play":
                if self.finished:
                    self.start()
                else:
                    self.playing = True
            elif kind == "pause":
                self.playing = False
            elif kind == "stop":
                self.playing = False
                self.finished = True
                for ch in self.channels:
                    ch.voice = None
                    self._midi_stop_note(ch)

    def _apply_cc(self, ci: int, cc: int, val: int):
        """Mapeo MIDI CC por canal: 1=cutoff, 7=volumen, 10=pan, 20=pitch."""
        if not 0 <= ci < CHANNEL_COUNT:
            return
        ch = self.channels[ci]
        if cc == 7:
            ch.cc_vol = val / 127.0
        elif cc == 10:
            ch.cc_pan = min(254, round(val * 254 / 127))
        elif cc == 20:
            ch.cc_pitch = 2.0 ** ((val - 64) / 64.0)
        elif cc == 1:
            ch.cc_cutoff = val / 127.0

    def _apply_param(self, ci: int, name: str, val: int):
        """Aplica un parámetro live por nombre (lo usan los pots mapeados
        en la configuración)."""
        if not 0 <= ci < CHANNEL_COUNT:
            return
        ch = self.channels[ci]
        if name == "volume":
            ch.cc_vol = val / 127.0
        elif name == "pan":
            ch.cc_pan = min(254, round(val * 254 / 127))
        elif name == "pitch":
            ch.cc_pitch = 2.0 ** ((val - 64) / 64.0)
        elif name == "cutoff":
            ch.cc_cutoff = val / 127.0
        elif name in EFFECT_PRESETS:
            ch.fx_amounts[name] = val / 127.0

    # -- núcleo del secuenciador (Player::Trigger del upstream) ----------------

    def _process_tick(self):
        if self.tick_count > 0:
            # Groove::Trigger + moveToNextStep: avance por canal según su
            # groove (patrón de longitudes de step en ticks)
            for ch in self.channels:
                self._update_groove(ch)
                if self._groove_trigger(ch):
                    self._advance_step(ch)
        for ch in self.channels:
            if ch.time_to_start > 0:
                ch.time_to_start -= 1
                if ch.time_to_start == 0:
                    self._trigger_row(ch)
        for ch in self.channels:
            self._process_row_commands(ch)
        for ch in self.channels:
            ch.table.step(ch, self)
        for ch in self.channels:
            if ch.time_to_live > 0:
                ch.time_to_live -= 1
                if ch.time_to_live == 0:
                    ch.voice = None
                    self._midi_stop_note(ch)
        for ch in self.channels:
            if ch.midi_ticks > 0:
                ch.midi_ticks -= 1
                if ch.midi_ticks == 0:
                    self._midi_stop_note(ch)
        self.tick_count += 1

    # -- groove (Application/Model/Groove.cpp del upstream) --------------------

    def _groove_len(self, groove: int, pos: int) -> int:
        v = self.groove_data[groove * 16 + pos]
        return v if 0 < v < 0xFF else TICKS_PER_STEP

    def _set_groove(self, ch: Channel, groove: int):
        if groove >= 0x20:
            return
        ch.groove = groove
        ch.g_pos = 0
        ch.g_ticks = self._groove_len(groove, 0)

    def _update_groove(self, ch: Channel):
        # Groove::UpdateGroove(reverse=false)
        if ch.g_ticks == 0:
            ch.g_pos = (ch.g_pos + 1) % 16
            if self.groove_data[ch.groove * 16 + ch.g_pos] == 0xFF:
                ch.g_pos = 0
            ch.g_ticks = self._groove_len(ch.groove, ch.g_pos)
        ch.g_ticks -= 1

    def _groove_trigger(self, ch: Channel) -> bool:
        # Groove::TriggerChannel: toca avanzar cuando ticks llega a 0
        return ch.g_ticks % self._groove_len(ch.groove, ch.g_pos) == 0

    def _advance_step(self, ch: Channel):
        if not ch.playing or ch.phrase == 0xFF:
            return
        pos = ch.phrase_pos + 1
        if pos != 16:
            hop = self._get_hop(ch, pos)
            if hop >= 0:
                self._next_phrase(ch, hop)
            else:
                self._set_phrase_pos(ch, pos)
        else:
            self._next_phrase(ch)

    def _set_phrase_pos(self, ch: Channel, pos: int):
        ch.phrase_pos = pos
        ch.time_to_start = 1
        row = ch.phrase * 16 + pos
        if self.project.cmd1[row] == "DLAY":
            ch.time_to_start = (self.project.param1[row] & 0xF) + 1
        if self.project.cmd2[row] == "DLAY":
            # El upstream lee param1 también para cmd2 (Player.cpp:807);
            # se replica ese comportamiento.
            ch.time_to_start = (self.project.param1[row] & 0xF) + 1

    def _next_phrase(self, ch: Channel, hop: int = -1):
        pos = ch.chain_pos + 1
        can = (
            pos < 16
            and ch.chain != 0xFF
            and self.project.chains[ch.chain * 16 + pos] != 0xFF
        )
        if can:
            self._set_chain_pos(ch, pos, hop)
        else:
            self._next_chain(ch, hop)

    def _next_chain(self, ch: Channel, hop: int = -1):
        song = self.project.song
        chains = self.project.chains
        pos = ch.song_pos + 1
        loop_back = True
        if pos < 256:
            data = song[pos * 8 + ch.idx]
            loop_back = data == 0xFF or chains[data * 16] == 0xFF
        if loop_back:
            # Vuelve al principio del bloque contiguo actual (loop de sección)
            pos -= 1
            while pos >= 0:
                data = song[pos * 8 + ch.idx]
                if data == 0xFF or chains[data * 16] == 0xFF:
                    break
                pos -= 1
            pos += 1
        if 0 <= pos < 256 and self._is_playable(pos, ch.idx):
            self._set_song_pos(ch, pos, 0, hop)
        else:
            self._stop_channel(ch)

    def _is_playable(self, pos: int, ci: int) -> bool:
        chain = self.project.song[pos * 8 + ci]
        return chain != 0xFF and self.project.chains[chain * 16] != 0xFF

    def _set_song_pos(self, ch: Channel, pos: int, chain_pos: int, hop: int):
        ch.song_pos = pos
        ch.chain = self.project.song[pos * 8 + ch.idx]
        self._set_chain_pos(ch, chain_pos, hop)

    def _set_chain_pos(self, ch: Channel, pos: int, hop: int):
        ch.chain_pos = pos
        if ch.chain != 0xFF:
            ch.phrase = self.project.chains[ch.chain * 16 + pos]
        else:
            ch.phrase = 0xFF
        if ch.phrase == 0xFF:
            self._stop_channel(ch)
        else:
            self._set_phrase_pos(ch, hop if hop >= 0 else 0)

    def _stop_channel(self, ch: Channel):
        ch.playing = False
        ch.voice = None
        self._midi_stop_note(ch)

    # -- instrumentos MIDI ----------------------------------------------------

    def _midi_start_note(self, ch: Channel, mdef: MidiDef, note: int):
        """Arranca una nota MIDI (MidiInstrument::Start/Render del upstream):
        primero CC7 con el volumen del instrumento, luego el note on."""
        ch.midi_def = mdef
        if self.midi_out is not None:
            vol = int((mdef.volume + 0.99) / 2)
            self.midi_out.cc(mdef.channel, 7, vol)
            vel = ch.midi_vel if ch.midi_vel is not None else vol
            self.midi_out.note_on(mdef.channel, note, vel)
        ch.midi_note = note
        ch.midi_ticks = mdef.note_length if mdef.note_length > 0 else -1

    def _midi_stop_note(self, ch: Channel):
        if (
            ch.midi_note is not None
            and ch.midi_def is not None
            and self.midi_out is not None
        ):
            self.midi_out.note_off(ch.midi_def.channel, ch.midi_note)
        ch.midi_note = None
        ch.midi_ticks = -1

    def _get_hop(self, ch: Channel, pos: int) -> int:
        row = ch.phrase * 16 + pos
        if self.project.cmd1[row] == "HOP ":
            return self.project.param1[row] & 0xF
        if self.project.cmd2[row] == "HOP ":
            return self.project.param2[row] & 0xF
        return -1

    def _trigger_row(self, ch: Channel):
        if not ch.playing or ch.phrase == 0xFF:
            return
        row = ch.phrase * 16 + ch.phrase_pos
        note = self.project.notes[row]
        instr = self.project.instruments[row]
        if note == 0xFF:
            return
        clean = instr != 0xFF
        if clean:
            ch.last_instr = instr
        if ch.last_instr is None:
            ch.last_instr = 0
        idef = self.instruments.get(ch.last_instr)
        mdef = (
            self.midi_instruments.get(ch.last_instr)
            if idef is None else None
        )
        if idef is None and mdef is None:
            return                            # instrumento inexistente
        t = 0
        if ch.chain != 0xFF:
            tb = self.project.transposes[ch.chain * 16 + ch.chain_pos]
            t = tb - 256 if tb > 127 else tb
        final = (note + t + self.transpose) % 256
        if final >= 128:
            return
        # Monofonía: corta la voz sample y/o la nota MIDI anteriores
        ch.voice = None
        self._midi_stop_note(ch)
        if idef is not None:
            sample = self.bank.get(idef.sample_name)
            if sample is None:
                return
            ch.voice = Voice(sample, idef, final, self.sr,
                             self.samples_per_tick)
            ch.kind = "sample"
        else:
            self._midi_start_note(ch, mdef, final)
            ch.kind = "midi"
        ch.last_note = final
        if clean:
            table = idef.table if idef is not None else mdef.table
            if table >= 0 and table in self.project.tables:
                ch.table.start(self.project.tables[table])
            else:
                ch.table.stop()

    def _process_row_commands(self, ch: Channel):
        if not ch.playing or ch.phrase == 0xFF:
            return
        row = ch.phrase * 16 + ch.phrase_pos
        self._exec_command(ch, self.project.cmd1[row], self.project.param1[row])
        self._exec_command(ch, self.project.cmd2[row], self.project.param2[row])

    def _exec_command(self, ch: Channel, cmd: str, param: int):
        if cmd == "----":
            return
        if cmd == "KILL":
            ch.time_to_live = (param & 0xFF) + 1
        elif cmd == "TABL":
            tid = param & 0x7F
            if tid in self.project.tables:
                ch.table.start(self.project.tables[tid])
        elif cmd == "STOP":
            self.playing = False
            self.finished = True
        elif cmd in ("HOP ", "DLAY"):
            pass                    # se procesan en el avance de step/trigger
        elif cmd == "GROV":
            groove = param & 0xFF
            if param & 0xFF00:                # nibble alto: todos los canales
                for c in self.channels:
                    self._set_groove(c, groove)
            else:
                self._set_groove(ch, groove)
        elif cmd in ("VOLM", "LEGA", "MDCC", "MDPG", "MVEL"):
            self._instrument_command(ch, cmd, param)
        else:
            self.unsupported_cmds.add(cmd)

    def _instrument_command(self, ch: Channel, cmd: str, param: int):
        if ch.kind == "sample" and ch.voice is not None:
            if cmd == "VOLM":
                ch.voice.set_volm(param)
            elif cmd == "LEGA":
                ch.voice.set_lega(param, ch.last_note)
        elif ch.kind == "midi" and ch.midi_def is not None:
            # MidiInstrument::ProcessCommand del upstream
            mch = ch.midi_def.channel
            if cmd == "VOLM" and self.midi_out is not None:
                self.midi_out.cc(mch, 7, (param // 2) & 0x7F)
            elif cmd == "MDCC" and self.midi_out is not None:
                self.midi_out.cc(mch, (param >> 8) & 0x7F, param & 0x7F)
            elif cmd == "MDPG" and self.midi_out is not None:
                self.midi_out.program_change(mch, param & 0x7F)
            elif cmd == "MVEL":
                ch.midi_vel = (param // 2) & 0x7F

    # -- información para la UI -------------------------------------------------

    def active_channels(self) -> int:
        return sum(
            1 for ch in self.channels
            if ch.voice is not None or ch.midi_note is not None
        )

    def song_positions(self) -> list[int]:
        return [ch.song_pos for ch in self.channels]


if __name__ == "__main__":
    # Benchmark headless: renderiza N segundos y mide velocidad.
    import sys
    import time

    project_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "/home/angel/LGPT/songs/lgpt_Sartenazo.VERSION1")
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

    engine = Engine(project_dir)
    engine.start()
    block = 512
    total = int(seconds * SAMPLE_RATE / block)
    t0 = time.perf_counter()
    peak = 0.0
    for _ in range(total):
        out = engine.render(block)
        if not engine.playing:
            break
        peak = max(peak, float(np.abs(out).max()))
    dt = time.perf_counter() - t0
    rendered = total * block / SAMPLE_RATE
    print(f"{project_dir.name}: {rendered:.1f}s de audio en {dt:.2f}s "
          f"({rendered / dt:.1f}x tiempo real, pico {peak:.3f})")
    if engine.unsupported_cmds:
        print("comandos ignorados:", sorted(engine.unsupported_cmds))
