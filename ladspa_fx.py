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


class LadspaPlugin:
    """Instancia genérica de un plugin LADSPA (un canal de audio)."""

    def __init__(self, path: str, unique_id: int, sample_rate: int):
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        self._lib = ctypes.CDLL(path)
        desc_fn = self._lib.ladspa_descriptor
        desc_fn.restype = ctypes.POINTER(_Descriptor)
        desc_fn.argtypes = [ctypes.c_ulong]
        desc_ptr = None
        for i in range(512):
            d = desc_fn(i)
            if not d:
                break
            if d.contents.UniqueID == unique_id:
                desc_ptr = d
                break
        if desc_ptr is None:
            raise RuntimeError(f"plugin {unique_id} no encontrado en {path}")
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
        self._controls: dict[int, ctypes.c_float] = {}
        # Conecta TODOS los puertos de control de entrada a 0.0: un plugin
        # puede leerlos aunque no los usemos (puntero colgado = segfault)
        LADSPA_PORT_INPUT = 0x1
        LADSPA_PORT_CONTROL = 0x4
        for port in range(desc.PortCount):
            flags = desc.PortDescriptors[port]
            if flags & LADSPA_PORT_INPUT and flags & LADSPA_PORT_CONTROL:
                self.set_control(port, 0.0)

    def set_control(self, port: int, value: float):
        f = self._controls.get(port)
        if f is None:
            f = ctypes.c_float(float(value))
            self._controls[port] = f
            self._connect(self._handle, port, ctypes.byref(f))
        else:
            f.value = float(value)

    def run(self, buf: np.ndarray, in_port: int, out_port: int):
        data = buf.ctypes.data_as(ctypes.c_void_p)
        self._connect(self._handle, in_port, data)
        self._connect(self._handle, out_port, data)
        self._run(self._handle, len(buf))


class LadspaSVF(LadspaPlugin):
    """State Variable Filter (svf_1214.so) para un canal de audio."""

    def __init__(self, sample_rate: int, path: str = SVF_PATH):
        super().__init__(path, SVF_ID, sample_rate)
        self.set_control(SVF_TYPE, 1.0)        # LP
        self.set_control(SVF_Q, 0.25)

    def set(self, freq_hz: float | None = None, res: float | None = None):
        if freq_hz is not None:
            self.set_control(SVF_FREQ, min(max(freq_hz, 0.0), 6000.0))
        if res is not None:
            self.set_control(SVF_RES, min(max(res, 0.0), 1.0))

    def run(self, buf: np.ndarray):
        super().run(buf, SVF_INPUT, SVF_OUTPUT)


CAPS_PATH = "/usr/lib/ladspa/caps.so"
CAPS_AUTOFILTER_ID = 2593

# Puertos del C* AutoFilter
AF_MODE = 0
AF_FILTER = 1
AF_FREQ = 2             # 20-3800 Hz (log)
AF_Q = 3                # 0-1
AF_DEPTH = 4
AF_LFOENV = 5           # 0 = cutoff manual
AF_RATE = 6
AF_SHAPE = 7
AF_INPUT = 8
AF_OUTPUT = 9


class LadspaAutoFilter(LadspaPlugin):
    """C* AutoFilter (caps.so): filtro resonante auto-modulado.

    Configurado como filtro acid manual: la envolvente/LFO no modula
    (lfo/env = 0); el cutoff lo controla el pot."""

    def __init__(self, sample_rate: int, path: str = CAPS_PATH):
        super().__init__(path, CAPS_AUTOFILTER_ID, sample_rate)
        self.set_control(AF_MODE, 0.0)         # 0 = low-pass (1 es HP!)
        self.set_control(AF_FILTER, 0.0)
        self.set_control(AF_DEPTH, 1.0)
        self.set_control(AF_LFOENV, 0.0)
        self.set_control(AF_RATE, 0.25)
        self.set_control(AF_SHAPE, 1.0)

    def set(self, freq_hz: float | None = None, res: float | None = None):
        if freq_hz is not None:
            self.set_control(AF_FREQ, min(max(freq_hz, 20.0), 3800.0))
        if res is not None:
            self.set_control(AF_Q, min(max(res, 0.0), 1.0))

    def run(self, buf: np.ndarray):
        super().run(buf, AF_INPUT, AF_OUTPUT)


class LadspaStereoSVF:
    """Dos instancias mono para el buffer estéreo del canal."""

    _PLUGIN = LadspaSVF

    def __init__(self, sample_rate: int):
        self.left = self._PLUGIN(sample_rate)
        self.right = self._PLUGIN(sample_rate)

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


class LadspaStereoAutoFilter(LadspaStereoSVF):
    """C* AutoFilter estéreo (filtro acid del canal de bajo)."""

    _PLUGIN = LadspaAutoFilter


FOVERDRIVE_PATH = "/usr/lib/ladspa/foverdrive_1196.so"
FOVERDRIVE_ID = 1196

# Puertos del Fast overdrive
OD_DRIVE = 0            # 1-3 (1 = limpio)
OD_INPUT = 1
OD_OUTPUT = 2


class LadspaOverdrive(LadspaPlugin):
    """Fast overdrive (swh): drive 1-3; 1 = sin distorsión."""

    def __init__(self, sample_rate: int, path: str = FOVERDRIVE_PATH):
        super().__init__(path, FOVERDRIVE_ID, sample_rate)

    def set(self, drive: float):
        self.set_control(OD_DRIVE, min(max(drive, 1.0), 3.0))

    def run(self, buf: np.ndarray):
        super().run(buf, OD_INPUT, OD_OUTPUT)


class LadspaStereoOverdrive:
    """Dos instancias mono para el buffer estéreo del canal."""

    def __init__(self, sample_rate: int):
        self.left = LadspaOverdrive(sample_rate)
        self.right = LadspaOverdrive(sample_rate)

    def set(self, drive: float):
        self.left.set(drive)
        self.right.set(drive)

    def run(self, buf: np.ndarray):
        left = np.ascontiguousarray(buf[:, 0])
        self.left.run(left)
        buf[:, 0] = left
        right = np.ascontiguousarray(buf[:, 1])
        self.right.run(right)
        buf[:, 1] = right


FOLDOVER_PATH = "/usr/lib/ladspa/foldover_1213.so"
FOLDOVER_ID = 1213

# Puertos del Foldover distortion
FO_DRIVE = 0            # 0-1
FO_SKEW = 1             # 0-1 (lo dejamos a 0: drive 0 = limpio)
FO_INPUT = 2
FO_OUTPUT = 3


class LadspaFoldover(LadspaPlugin):
    """Foldover distortion (swh): wavefolding, muy audible en bajos.

    skew = 0: drive 0 pasa limpio; al subir drive crece el contenido
    armónico progresivamente."""

    def __init__(self, sample_rate: int, path: str = FOLDOVER_PATH):
        super().__init__(path, FOLDOVER_ID, sample_rate)
        self.set_control(FO_SKEW, 0.0)

    def set(self, drive: float):
        self.set_control(FO_DRIVE, min(max(drive, 0.0), 1.0))

    def run(self, buf: np.ndarray):
        super().run(buf, FO_INPUT, FO_OUTPUT)


class LadspaStereoFoldover:
    """Dos instancias mono + compensación de nivel (1/(1+drive))."""

    def __init__(self, sample_rate: int):
        self.left = LadspaFoldover(sample_rate)
        self.right = LadspaFoldover(sample_rate)
        self._drive = 0.0

    def set(self, drive: float):
        self._drive = min(max(drive, 0.0), 1.0)
        self.left.set(self._drive)
        self.right.set(self._drive)

    def run(self, buf: np.ndarray):
        left = np.ascontiguousarray(buf[:, 0])
        self.left.run(left)
        buf[:, 0] = left
        right = np.ascontiguousarray(buf[:, 1])
        self.right.run(right)
        buf[:, 1] = right
        buf *= 1.0 / (1.0 + self._drive)


DIVIDER_PATH = "/usr/lib/ladspa/divider_1186.so"
DIVIDER_ID = 1186

# Puertos del Audio Divider (suboctava)
DIV_DENOM = 0           # 1-8 entero
DIV_INPUT = 1
DIV_OUTPUT = 2


class LadspaDivider(LadspaPlugin):
    """Audio Divider (swh): generador de suboctava para bajos.

    denominator 1 = casi limpio; 2-4 añade la octava inferior."""

    def __init__(self, sample_rate: int, path: str = DIVIDER_PATH):
        super().__init__(path, DIVIDER_ID, sample_rate)

    def set(self, denominator: float):
        self.set_control(DIV_DENOM,
                         float(int(round(min(max(denominator, 1.0), 8.0)))))

    def run(self, buf: np.ndarray):
        super().run(buf, DIV_INPUT, DIV_OUTPUT)


class LadspaStereoDivider:
    """Dos instancias mono para el buffer estéreo del canal."""

    def __init__(self, sample_rate: int):
        self.left = LadspaDivider(sample_rate)
        self.right = LadspaDivider(sample_rate)

    def set(self, denominator: float):
        self.left.set(denominator)
        self.right.set(denominator)

    def run(self, buf: np.ndarray):
        left = np.ascontiguousarray(buf[:, 0])
        self.left.run(left)
        buf[:, 0] = left
        right = np.ascontiguousarray(buf[:, 1])
        self.right.run(right)
        buf[:, 1] = right


SATAN_PATH = "/usr/lib/ladspa/satan_maximiser_1408.so"
SATAN_ID = 1408

# Puertos de Barry's Satan Maximiser
SAT_DECAY = 0           # 2-30 samples
SAT_KNEE = 1            # -90 a 0 dB (0 = limpio, -90 = destrucción)
SAT_INPUT = 2
SAT_OUTPUT = 3


class LadspaSatan(LadspaPlugin):
    """Barry's Satan Maximiser (swh): maximizador/distorsión brutal."""

    def __init__(self, sample_rate: int, path: str = SATAN_PATH):
        super().__init__(path, SATAN_ID, sample_rate)
        self.set_control(SAT_DECAY, 30.0)

    def set(self, knee_db: float):
        self.set_control(SAT_KNEE, min(max(knee_db, -90.0), 0.0))

    def run(self, buf: np.ndarray):
        super().run(buf, SAT_INPUT, SAT_OUTPUT)


class LadspaStereoSatan:
    """Dos instancias mono para el buffer estéreo del canal."""

    def __init__(self, sample_rate: int):
        self.left = LadspaSatan(sample_rate)
        self.right = LadspaSatan(sample_rate)

    def set(self, knee_db: float):
        self.left.set(knee_db)
        self.right.set(knee_db)

    def run(self, buf: np.ndarray):
        left = np.ascontiguousarray(buf[:, 0])
        self.left.run(left)
        buf[:, 0] = left
        right = np.ascontiguousarray(buf[:, 1])
        self.right.run(right)
        buf[:, 1] = right


RINGMOD_PATH = "/usr/lib/ladspa/ringmod_1188.so"
RINGMOD_ID = 1189

# Puertos del Ringmod with LFO
RM_DEPTH = 0            # 0=none, 1=AM, 2=RM
RM_FREQ = 1             # 1-1000 Hz
RM_SINE = 2
RM_INPUT = 6
RM_OUTPUT = 7


class LadspaRingmod(LadspaPlugin):
    """Ringmod with LFO (swh): textura robótica/metálica."""

    def __init__(self, sample_rate: int, path: str = RINGMOD_PATH):
        super().__init__(path, RINGMOD_ID, sample_rate)
        self.set_control(RM_SINE, 1.0)

    def set(self, depth: float, freq_hz: float):
        self.set_control(RM_DEPTH, min(max(depth, 0.0), 2.0))
        self.set_control(RM_FREQ, min(max(freq_hz, 1.0), 1000.0))

    def run(self, buf: np.ndarray):
        super().run(buf, RM_INPUT, RM_OUTPUT)


class LadspaStereoRingmod:
    """Dos instancias mono para el buffer estéreo del canal."""

    def __init__(self, sample_rate: int):
        self.left = LadspaRingmod(sample_rate)
        self.right = LadspaRingmod(sample_rate)

    def set(self, depth: float, freq_hz: float):
        self.left.set(depth, freq_hz)
        self.right.set(depth, freq_hz)

    def run(self, buf: np.ndarray):
        left = np.ascontiguousarray(buf[:, 0])
        self.left.run(left)
        buf[:, 0] = left
        right = np.ascontiguousarray(buf[:, 1])
        self.right.run(right)
        buf[:, 1] = right


PHASER_PATH = "/usr/lib/ladspa/phasers_1217.so"
PHASER_ID = 1217

# Puertos del LFO Phaser
PH_RATE = 0             # 0-100 Hz
PH_DEPTH = 1            # 0-1 (0 = seco)
PH_FEEDBACK = 2         # -1 a 1
PH_SPREAD = 3           # 0-2 octavas
PH_INPUT = 4
PH_OUTPUT = 5


class LadspaPhaser(LadspaPlugin):
    """LFO Phaser (swh): movimiento sin tocar el nivel."""

    def __init__(self, sample_rate: int, path: str = PHASER_PATH):
        super().__init__(path, PHASER_ID, sample_rate)
        self.set_control(PH_SPREAD, 1.0)

    def set(self, rate: float, depth: float, feedback: float):
        self.set_control(PH_RATE, min(max(rate, 0.0), 100.0))
        self.set_control(PH_DEPTH, min(max(depth, 0.0), 1.0))
        self.set_control(PH_FEEDBACK, min(max(feedback, -1.0), 1.0))

    def run(self, buf: np.ndarray):
        super().run(buf, PH_INPUT, PH_OUTPUT)


class LadspaStereoPhaser:
    """Dos instancias mono para el buffer estéreo del canal."""

    def __init__(self, sample_rate: int):
        self.left = LadspaPhaser(sample_rate)
        self.right = LadspaPhaser(sample_rate)

    def set(self, rate: float, depth: float, feedback: float):
        self.left.set(rate, depth, feedback)
        self.right.set(rate, depth, feedback)

    def run(self, buf: np.ndarray):
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
