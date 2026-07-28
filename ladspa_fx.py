#!/usr/bin/env python3
"""Host LADSPA mínimo (ctypes) para el filtro del canal de bajo.

Carga plugins LADSPA directamente del .so (sin servidor JACK ni host
externo) y los ejecuta en el hilo de audio. Pensado para el
"State Variable Filter" de swh-plugins (svf_1214.so) instalado en la
Raspberry Pi; si no está disponible, el engine usa su biquad propio.

Referencia: ladspa.h (LADSPA SDK).
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import numpy as np

SVF_PATH = "/usr/lib/ladspa/svf_1214.so"
SVF_ID = 1214

# Puertos del State Variable Filter
SVF_INPUT = 0
SVF_OUTPUT = 1
SVF_TYPE = 2          # 0=none, 1=LP, 2=HP, 3=BP, 4=BR, 5=AP
SVF_FREQ = 3          # 0-6000 Hz
SVF_Q = 4             # 0-1
SVF_RES = 5           # 0-1


class _Descriptor(ctypes.Structure):
    _fields_ = [
        ("UniqueID", ctypes.c_ulong),
        ("Label", ctypes.c_char_p),
        ("Properties", ctypes.c_int),
        ("Name", ctypes.c_char_p),
        ("Maker", ctypes.c_char_p),
        ("Copyright", ctypes.c_char_p),
        ("PortCount", ctypes.c_ulong),
        ("PortDescriptors", ctypes.POINTER(ctypes.c_int)),
        ("PortNames", ctypes.POINTER(ctypes.c_char_p)),
        ("PortRangeHints", ctypes.c_void_p),
        ("ImplementationData", ctypes.c_void_p),
        ("instantiate", ctypes.c_void_p),
        ("connect_port", ctypes.c_void_p),
        ("activate", ctypes.c_void_p),
        ("run", ctypes.c_void_p),
        ("run_adding", ctypes.c_void_p),
        ("set_run_adding_gain", ctypes.c_void_p),
        ("deactivate", ctypes.c_void_p),
        ("cleanup", ctypes.c_void_p),
    ]


class LadspaSVF:
    """State Variable Filter LADSPA para un canal de audio.

    Uso:
        fx = LadspaSVF(44100)          # falla si no hay plugin
        fx.set(freq_hz=440.0, res=0.5)
        fx.run(buffer_float32_mono)    # in-place
    """

    def __init__(self, sample_rate: int, path: str = SVF_PATH):
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        self._lib = ctypes.CDLL(path)
        desc_fn = self._lib.ladspa_descriptor
        desc_fn.restype = ctypes.POINTER(_Descriptor)
        desc_fn.argtypes = [ctypes.c_ulong]
        desc_ptr = None
        for i in range(256):
            d = desc_fn(i)
            if not d:
                break
            if d.contents.UniqueID == SVF_ID:
                desc_ptr = d
                break
        if desc_ptr is None:
            raise RuntimeError(f"plugin {SVF_ID} no encontrado en {path}")
        self._desc_ptr = desc_ptr
        desc = desc_ptr.contents

        instantiate = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)(
            desc.instantiate)
        self._handle = instantiate(desc_ptr, sample_rate)
        if not self._handle:
            raise RuntimeError("instantiate falló")

        self._connect = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p)(
            desc.connect_port)
        self._run = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulong)(
            desc.run)
        if desc.activate:
            activate = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(desc.activate)
            activate(self._handle)

        # Puertos de control como floats persistentes
        self._type = ctypes.c_float(1.0)       # LP
        self._freq = ctypes.c_float(440.0)
        self._q = ctypes.c_float(0.25)
        self._res = ctypes.c_float(0.0)
        self._connect(self._handle, SVF_TYPE, ctypes.byref(self._type))
        self._connect(self._handle, SVF_FREQ, ctypes.byref(self._freq))
        self._connect(self._handle, SVF_Q, ctypes.byref(self._q))
        self._connect(self._handle, SVF_RES, ctypes.byref(self._res))

    def set(self, freq_hz: float | None = None, res: float | None = None):
        if freq_hz is not None:
            self._freq.value = float(min(max(freq_hz, 0.0), 6000.0))
        if res is not None:
            self._res.value = float(min(max(res, 0.0), 1.0))

    def run(self, buf: np.ndarray):
        """Procesa un buffer mono float32 contiguo in-place."""
        data = buf.ctypes.data_as(ctypes.c_void_p)
        self._connect(self._handle, SVF_INPUT, data)
        self._connect(self._handle, SVF_OUTPUT, data)
        self._run(self._handle, len(buf))


class LadspaStereoSVF:
    """Dos instancias mono para el buffer estéreo del canal."""

    def __init__(self, sample_rate: int):
        self.left = LadspaSVF(sample_rate)
        self.right = LadspaSVF(sample_rate)

    def set(self, freq_hz: float | None = None, res: float | None = None):
        self.left.set(freq_hz, res)
        self.right.set(freq_hz, res)

    def run(self, buf: np.ndarray):
        """buf (n, 2) float32, in-place."""
        left = np.ascontiguousarray(buf[:, 0])
        self.left.run(left)
        buf[:, 0] = left
        right = np.ascontiguousarray(buf[:, 1])
        self.right.run(right)
        buf[:, 1] = right


if __name__ == "__main__":
    # Autotest: ruido blanco -> con cutoff bajo debe atenuarse mucho
    sr = 44100
    fx = LadspaSVF(sr)
    rng = np.random.default_rng(0)
    noise = rng.standard_normal(sr).astype(np.float32) * 0.5
    ref = float(np.sqrt((noise ** 2).mean()))
    fx.set(freq_hz=100.0, res=0.0)
    out = noise.copy()
    fx.run(out)
    lp = float(np.sqrt((out ** 2).mean()))
    print(f"rms abierto {ref:.4f} -> lp100 {lp:.4f} "
          f"(atenuación {20 * np.log10(lp / ref):.1f} dB)")
