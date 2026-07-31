#!/usr/bin/env python3
"""Reproductor standalone de proyectos LittleGPTracker para consola.

Pensado para Raspberry Pi 4 con HAT de audio (headless), con control
en directo por MIDI CC (canal MIDI 1-8 -> canal tracker 0-7):

    CC 1  -> cutoff del filtro   CC 7  -> volumen
    CC 10 -> pan                 CC 20 -> pitch (+-1 octava, centro en 64)

La configuración (carpeta de canciones, dispositivo de salida de audio,
puertos MIDI de entrada y salida) se lee de `lttileplayer.toml` en el
directorio del programa. Los argumentos de línea de comandos tienen
prioridad sobre el archivo.

Uso:
    lgpt_player.py [--config TOML] [--songs DIR] [--device DEV]
                   [--midi IN] [--midi-out OUT] [--samplerate HZ]
                   [--blocksize N]

Teclas:
    lista:  up/down o j/k moverse, enter reproducir, q salir
    play:   espacio play/pausa, n siguiente, p anterior, q volver a la lista
"""

from __future__ import annotations

import argparse
import math
import queue
import sys
import threading
import time
import tomllib
from pathlib import Path

import numpy as np
import sounddevice as sd

from lgpt_engine import Engine, MidiOut, SAMPLE_RATE

DEFAULT_SONGS_DIR = "/home/angel/Documentos/canciones/"
CONFIG_PATH = Path(__file__).resolve().parent / "lttileplayer.toml"

# --- Visualizador en directo (espectro reactivo al audio) --------------------
# Se analiza el audio ya mezclado (el bloque del callback), no el motor: una
# ventana rodante de VIZ_NFFT muestras -> rFFT -> bandas log. Todo el análisis
# ocurre en el hilo de UI; el hilo de audio solo copia el bloque (ver
# _audio_callback). Constantes calibradas sobre canciones reales.
VIZ_NFFT = 2048           # ~46 ms @44.1k: resuelve graves sin retardo visible
VIZ_FMIN = 35.0
VIZ_FMAX = 16000.0
# Auto-ganancia: con ganancia fija, un tema con graves fuertes deja las
# barras bajas clavadas arriba (el drone de bulebule) y el visor deja de
# informar. Se normaliza contra una referencia que sube rápido y baja
# despacio, así el visor sigue siendo expresivo con cualquier nivel.
VIZ_AGC_REF = 0.02        # nivel de referencia inicial/mínimo (evita ruido)
VIZ_AGC_UP = 0.30         # adaptación al subir (rápida: no satura)
VIZ_AGC_DOWN = 0.02       # adaptación al bajar (lenta: mantiene dinámica)
VIZ_AGC_KNEE = 2.3        # una banda en la referencia llega a ~90% de altura
VIZ_TILT = 0.5            # realce de agudos: peso (f/fmin)**VIZ_TILT
VIZ_MAX_BANDS = 48        # más bandas que bins útiles solo repetiría barras
VIZ_ZONES = 8             # la pantalla se reparte en 8 zonas, una por knob
VIZ_ATTACK = 0.6          # subida rápida de la barra (0-1, mayor = más rápida)
VIZ_RELEASE = 0.16        # caída lenta de la barra
VIZ_PEAK_FALL = 0.012     # caída del testigo de pico por frame
# Sub-bloques verticales para resolución fina dentro de cada celda de texto.
VIZ_BLOCKS = " ▁▂▃▄▅▆▇█"


def load_config(path: Path) -> dict:
    if path.is_file():
        with open(path, "rb") as f:
            return tomllib.load(f)
    return {}


def find_projects(songs_dir: Path) -> list[Path]:
    """Proyectos LGPT: directorios que contienen lgptsav.dat."""
    if not songs_dir.is_dir():
        return []
    return sorted(
        (d for d in songs_dir.iterdir()
         if d.is_dir() and (d / "lgptsav.dat").is_file()),
        key=lambda d: d.name.lower(),
    )


def _pick_port(names: list[str], wanted: str | None, what: str) -> str | None:
    """Resuelve un puerto MIDI por nombre parcial. Si el nombre guardado
    incluye el id de cliente ALSA ("... 128:0"), también se prueba sin él
    (el número cambia entre arranques)."""
    if not names:
        print(f"[midi] no hay puertos MIDI de {what}; desactivado")
        return None
    if wanted:
        base = wanted.rsplit(" ", 1)[0]      # sin el "client:port" final
        for n in names:
            if wanted.lower() in n.lower() or base.lower() in n.lower():
                return n
        print(f"[midi] puerto '{wanted}' no encontrado; disponibles: {names}")
        return None
    return names[0]


class TcpStreamer:
    """Emite la salida de audio por TCP (PCM s16le estéreo) para escuchar
    la Pi desde otro equipo. Escucha en un puerto; al conectar un cliente
    empieza a enviar. En el PC: `nc <pi> <puerto> | aplay -f S16_LE -c 2`.

    El callback encola bloques (cola acotada: si la red no da abasto se
    descartan, no se bloquea el audio).
    """

    def __init__(self, port: int, samplerate: int, on_event=None):
        import socket
        self.samplerate = samplerate
        self._on_event = on_event            # callback(msg) para la UI
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self.dropped = 0
        self._queued = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", port))
        self._sock.listen(1)
        self._client = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _notify(self, msg: str):
        if self._on_event is not None:
            self._on_event(msg)

    def _run(self):
        import socket
        while True:
            if self._client is None:
                try:
                    self._sock.settimeout(0.5)
                    self._client, addr = self._sock.accept()
                    self._notify(f"stream conectado: {addr[0]}")
                    self._queued = 0
                except socket.timeout:
                    continue
                except OSError:
                    return
            try:
                block = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._queued = max(0, self._queued - 1)
            if block is None:
                return
            try:
                self._client.sendall(block)
            except OSError:
                self._notify("stream desconectado")
                self._client = None

    def write(self, block: np.ndarray):
        if self._client is None:
            return
        # cola acotada (~0.37 s): si se llena, descartamos lo MÁS VIEJO
        # para que el stream se mantenga en directo tras tirones de red
        while self._queued >= 32:
            try:
                self._queue.get_nowait()
                self.dropped += 1
                self._queued -= 1
            except queue.Empty:
                self._queued = 0
                break
        pcm = (np.clip(block, -1.0, 1.0) * 32767).astype(np.int16)
        self._queue.put(pcm.tobytes())
        self._queued += 1

    def close(self):
        self._queue.put(None)
        try:
            self._sock.close()
            if self._client is not None:
                self._client.close()
        except OSError:
            pass


class WavRecorder:
    """Graba la salida de audio a un WAV sin bloquear el callback:
    el callback encola bloques y un hilo escritor los vuelca a disco."""

    def __init__(self, path: str, samplerate: int):
        import soundfile as sf
        self._sf = sf.SoundFile(path, "w", samplerate=samplerate,
                                channels=2, subtype="PCM_16")
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            block = self._queue.get()
            if block is None:
                break
            self._sf.write(block)
        self._sf.close()

    def write(self, block):
        self._queue.put(block.copy())

    def close(self):
        self._queue.put(None)
        self._thread.join()


class MidoMidiOut(MidiOut):
    """Sink MidiOut del engine sobre un puerto mido."""

    def __init__(self, port, mido):
        self._port = port
        self._mido = mido

    def note_on(self, channel, note, velocity):
        self._port.send(self._mido.Message(
            "note_on", channel=channel, note=note, velocity=velocity))

    def note_off(self, channel, note):
        self._port.send(self._mido.Message(
            "note_off", channel=channel, note=note, velocity=0))

    def cc(self, channel, control, value):
        self._port.send(self._mido.Message(
            "control_change", channel=channel, control=control, value=value))

    def program_change(self, channel, program):
        self._port.send(self._mido.Message(
            "program_change", channel=channel, program=program))


def parse_button_spec(spec: str) -> tuple | None:
    """'note:canal:nota' o 'cc:canal:control' -> tupla normalizada, o None."""
    try:
        kind, ch, num = spec.split(":")
        ch, num = int(ch), int(num)
    except (ValueError, AttributeError):
        return None
    if kind == "note":
        return ("note_on", ch & 0x0F, num & 0x7F)
    if kind == "cc":
        return ("control_change", ch & 0x0F, num & 0x7F)
    return None


def match_button(mapping: dict, msg) -> str | None:
    """Devuelve la acción del botón que coincide con el mensaje, o None.

    mapping: acción -> spec de parse_button_spec()."""
    if msg.type == "note_on" and msg.velocity == 0:
        return None
    for action, spec in mapping.items():
        if spec is None:
            continue
        mtype, ch, num = spec
        if msg.type != mtype or getattr(msg, "channel", None) != ch:
            continue
        if mtype == "note_on" and msg.note == num:
            return action
        if mtype == "control_change" and msg.control == num and msg.value > 0:
            return action
    return None


def parse_pot_target(target: str) -> tuple | None:
    """'canal:parametro' -> (canal, parametro); None si no es válido."""
    try:
        ch, name = target.split(":")
        ch = int(ch)
    except (ValueError, AttributeError):
        return None
    if not 0 <= ch < 8 or not name:
        return None
    return (ch, name)


def match_pot(pots: list, msg) -> tuple | None:
    """Devuelve (canal, parámetro, nº de knob 0-7) del pot que coincide.

    pots: lista de (spec, target, idx) donde target es (canal|None, param);
    canal None = se deriva del canal MIDI del mensaje (% 8). `idx` es el
    knob físico (pot1 -> 0), necesario para pintarlo en el visor."""
    if msg.type != "control_change":
        return None
    for spec, target, idx in pots:
        if spec is None:
            continue
        mtype, ch, num = spec
        if mtype == "control_change" and msg.channel == ch \
                and msg.control == num:
            tch, tparam = target
            return (msg.channel % 8 if tch is None else tch), tparam, idx
    return None


def open_midi_input(port_name: str | None, engine_ref: dict,
                    ui_queue: queue.SimpleQueue, buttons: dict,
                    pots: dict):
    """Abre el puerto MIDI de entrada: botones a la UI y pots/CC al engine.

    engine_ref es un dict mutable con la clave "engine": el callback MIDI
    siempre usa el engine actual, aunque se cambie de canción.
    Si hay pots configurados solo se procesan esos; si no, se usa el mapeo
    CC por defecto del engine (1/7/10/20).
    """
    if port_name == "off":
        return None
    try:
        import mido
    except ImportError:
        print("[midi] mido no disponible; control MIDI desactivado")
        return None

    chosen = _pick_port(mido.get_input_names(), port_name, "entrada")
    if chosen is None:
        return None

    def on_message(msg):
        rq = engine_ref.get("raw_queue")
        if rq is not None:
            num = getattr(msg, "note", getattr(msg, "control", 0))
            rq.put((msg.type, getattr(msg, "channel", 0), num))
        if engine_ref.get("capture_mode"):
            return                          # CONFIG capturando: no disparar
        # Los botones mapeados tienen prioridad sobre pots y CC
        action = match_button(buttons, msg)
        if action is not None:
            if action.startswith("sample"):
                # pads sampler: disparan WAVs del banco (sample1 -> 001.wav)
                engine = engine_ref.get("engine")
                if engine is not None:
                    try:
                        idx = int(action[6:]) - 1
                    except ValueError:
                        idx = 0
                    engine.push_event("trigger", idx)
            else:
                ui_queue.put(action)
            return
        engine = engine_ref.get("engine")
        if engine is None:
            return
        # pots: la lista se rellena por canción; se evalúa EN CADA mensaje
        if pots:
            hit = match_pot(pots, msg)
            if hit is not None:
                tch, tparam, idx = hit
                engine.push_event("param", tch, tparam, msg.value)
                values = engine_ref.get("pot_values")
                if values is not None:
                    values[idx] = msg.value     # solo para el visor
        elif msg.type == "control_change":
            engine.push_event("cc", msg.channel % 8, msg.control, msg.value)

    port = mido.open_input(chosen, callback=on_message)
    print(f"[midi] entrada: '{chosen}'")
    return port


def open_midi_output(port_name: str | None) -> MidoMidiOut | None:
    """Abre el puerto MIDI de salida para los eventos MIDI de LGPT."""
    if not port_name or port_name == "off":
        return None
    try:
        import mido
    except ImportError:
        print("[midi] mido no disponible; salida MIDI desactivada")
        return None

    if port_name == "virtual":
        # Crea un puerto ALSA nuevo al que se pueden conectar otros programas
        port = mido.open_output("lttileplayer", virtual=True)
        print("[midi] salida: puerto virtual 'lttileplayer'")
        return MidoMidiOut(port, mido)

    chosen = _pick_port(mido.get_output_names(), port_name, "salida")
    if chosen is None:
        return None
    port = mido.open_output(chosen)
    print(f"[midi] salida: '{chosen}'")
    return MidoMidiOut(port, mido)


NOTE_NAMES = ["C-", "C#", "D-", "D#", "E-", "F-",
              "F#", "G-", "G#", "A-", "A#", "B-"]

# Microfuente 3x5 para el título y la lista de canciones (estética retro)
FONT3X5 = {
    "A": [" # ", "# #", "###", "# #", "# #"],
    "B": ["## ", "# #", "## ", "# #", "## "],
    "C": [" ##", "#  ", "#  ", "#  ", " ##"],
    "D": ["## ", "# #", "# #", "# #", "## "],
    "E": ["###", "#  ", "## ", "#  ", "###"],
    "F": ["###", "#  ", "## ", "#  ", "#  "],
    "G": [" ##", "#  ", "# #", "# #", " ##"],
    "H": ["# #", "# #", "###", "# #", "# #"],
    "I": ["###", " # ", " # ", " # ", "###"],
    "J": ["  #", "  #", "  #", "# #", " # "],
    "K": ["# #", "# #", "## ", "# #", "# #"],
    "L": ["#  ", "#  ", "#  ", "#  ", "###"],
    "M": ["# #", "###", "###", "# #", "# #"],
    "N": ["# #", "## ", "###", " ##", "# #"],
    "O": [" # ", "# #", "# #", "# #", " # "],
    "P": ["## ", "# #", "## ", "#  ", "#  "],
    "Q": [" # ", "# #", "# #", " ##", "  #"],
    "R": ["## ", "# #", "## ", "# #", "# #"],
    "S": [" ##", "#  ", " # ", "  #", "## "],
    "T": ["###", " # ", " # ", " # ", " # "],
    "U": ["# #", "# #", "# #", "# #", "###"],
    "V": ["# #", "# #", "# #", "# #", " # "],
    "W": ["# #", "# #", "###", "###", "# #"],
    "X": ["# #", "# #", " # ", "# #", "# #"],
    "Y": ["# #", "# #", " # ", " # ", " # "],
    "Z": ["###", "  #", " # ", "#  ", "###"],
    "0": [" # ", "# #", "# #", "# #", " # "],
    "1": [" # ", "## ", " # ", " # ", "###"],
    "2": ["## ", "  #", " # ", "#  ", "###"],
    "3": ["## ", "  #", " # ", "  #", "## "],
    "4": ["# #", "# #", "###", "  #", "  #"],
    "5": ["###", "#  ", "## ", "  #", "## "],
    "6": [" ##", "#  ", "## ", "# #", " # "],
    "7": ["###", "  #", " # ", " # ", " # "],
    "8": [" # ", "# #", " # ", "# #", " # "],
    "9": [" # ", "# #", " ##", "  #", "## "],
    "-": ["   ", "   ", "###", "   ", "   "],
    ".": ["   ", "   ", "   ", "   ", " # "],
    " ": ["   ", "   ", "   ", "   ", "   "],
}


def big_text(scr, y: int, x: int, text: str, scale: int, attr):
    """Dibuja `text` con la microfuente 3x5 escalada en bloques."""
    for i, ch in enumerate(text):
        glyph = FONT3X5.get(ch, FONT3X5[" "])
        for r, line in enumerate(glyph):
            for c, px in enumerate(line):
                if px != " ":
                    scr.addstr(y + r * scale, x + i * 4 * scale + c * scale,
                               "█" * scale, attr)


def big_text_half(scr, y: int, x: int, text: str, attr):
    """Dibuja `text` con la microfuente 3x5 usando medio-bloques (▀ ▄ █):
    cada celda empaqueta 2 píxeles verticales, así que el texto queda más
    suave y compacto (3 filas de celda en vez de 5)."""
    for i, ch in enumerate(text):
        glyph = FONT3X5.get(ch, FONT3X5[" "])
        for pair in range(3):
            top = glyph[pair * 2]
            bottom = glyph[pair * 2 + 1] if pair * 2 + 1 < 5 else "   "
            for c in range(3):
                t, b = top[c] != " ", bottom[c] != " "
                if t and b:
                    cell = "█"
                elif t:
                    cell = "▀"
                elif b:
                    cell = "▄"
                else:
                    continue
                scr.addstr(y + pair, x + i * 4 + c, cell, attr)


def display_name(dirname: str) -> str:
    """Nombre de canción para la lista: sin 'lgpt_', corto y en mayúsculas."""
    name = dirname
    if name.startswith("lgpt_"):
        name = name[5:]
    name = name.split(".")[0]
    return name.upper()[:10]


def note_name(n) -> str:
    if n is None or n >= 0x80:
        return "---"
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 2}"


def meter(value: float, width: int = 8) -> str:
    """Medidor retro de bloques: value 0-1."""
    value = min(max(value, 0.0), 1.0)
    filled = round(value * width)
    return "█" * filled + "░" * (width - filled)


def pan_meter(pan: int, width: int = 5) -> str:
    """Indicador de pan de `width` celdas con marca."""
    pan = min(max(pan, 0), 254)
    pos = round(pan / 254 * (width - 1))
    cells = ["·"] * width
    cells[pos] = "█"
    return "".join(cells)


class Player:
    def __init__(self, args):
        self.args = args
        self.buttons = args.buttons
        self.ui_queue: queue.SimpleQueue = queue.SimpleQueue()
        self.projects = find_projects(Path(args.songs))
        if not self.projects:
            sys.exit(f"No se encuentran proyectos LGPT en {args.songs}")
        self.index = 0
        self.engine_ref: dict = {}
        self.midi_in = None
        self.midi_out: MidoMidiOut | None = None
        self.recorder: WavRecorder | None = None
        self._notice: tuple | None = None   # (mensaje, timestamp) para la UI
        self.streamer: TcpStreamer | None = None
        self._expected_dac_time: float | None = None  # reloj real esperado
        # visualizador en directo (ver constantes VIZ_*)
        self._view_mode = "viz"              # "viz" | "detail"
        self._want_viz = False               # el callback copia audio solo si True
        self._viz_ring = np.zeros(VIZ_NFFT, dtype=np.float32)  # audio mono rodante
        self._viz_bands = None               # alturas suavizadas (0-1) por barra
        self._viz_peaks = None               # testigos de pico por barra
        self._viz_win = np.hanning(VIZ_NFFT).astype(np.float32)
        self._viz_layout_cache: dict = {}    # nbands -> (edges, weights, colors)
        self._viz_agc = VIZ_AGC_REF          # referencia de auto-ganancia
        self.pot_labels: list = [None] * 8   # (pista, efecto) por knob activo
        self.engine_ref["pot_values"] = [0] * 8   # último valor MIDI por knob
        self.stream = sd.OutputStream(
            samplerate=args.samplerate,
            channels=2,
            dtype="float32",
            blocksize=args.blocksize,
            device=args.device or None,
            callback=self._audio_callback,
        )

    # -- audio ----------------------------------------------------------------

    def _audio_callback(self, outdata, frames, time_info, status):
        engine = self.engine_ref.get("engine")
        dac_time = time_info.outputBufferDacTime
        if engine is not None:
            expected = self._expected_dac_time
            if expected is not None:
                drift = dac_time - expected
                if drift > 0.002:      # >2ms: xrun real, no ruido de reloj
                    engine.catch_up(drift)
                    self._set_notice(f"glitch recuperado ({drift * 1000:.0f}ms)")
        self._expected_dac_time = dac_time + frames / self.args.samplerate
        if engine is None:
            outdata[:] = 0
        else:
            outdata[:] = engine.render(frames)
        recorder = self.recorder
        if recorder is not None:
            recorder.write(outdata)
        streamer = self.streamer
        if streamer is not None:
            streamer.write(outdata)
        if self._want_viz:
            # ventana rodante mono para el visualizador (swap de referencia:
            # el hilo de UI ve siempre un snapshot consistente, sin locks).
            mono = (outdata[:, 0] + outdata[:, 1]).astype(np.float32)
            n = len(mono)
            if n >= VIZ_NFFT:
                self._viz_ring = mono[-VIZ_NFFT:].copy()
            else:
                self._viz_ring = np.concatenate((self._viz_ring[n:], mono))

    def _load_song(self, index: int):
        project_dir = self.projects[index]
        old = self.engine_ref.get("engine")
        if old is not None:
            old.panic()                   # note off de notas MIDI colgadas
        engine = Engine(project_dir, sample_rate=self.args.samplerate,
                        audio_delay=self.args.delay,
                        wavs_dir=self.args.wavs_dir)
        engine.midi_out = self.midi_out
        engine.start()
        self._apply_song_config(project_dir, engine)
        self.engine_ref["engine"] = engine   # swap atómico de referencia
        return engine

    def _apply_song_config(self, project_dir: Path, engine: Engine):
        """Config por canción (robotraca.json en la carpeta del proyecto):
        mute de canales y targets de los knobs (canal:efecto).
        Sin JSON: sin mute y sin efectos."""
        import json
        cfg_file = project_dir / "robotraca.json"
        song_cfg = {}
        if cfg_file.is_file():
            try:
                song_cfg = json.loads(cfg_file.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[config] {cfg_file.name}: {exc}")
        engine.muted = set(song_cfg.get("mute", []))
        # volumen de pads: número (todos) o dict por pad {"2": 40}
        pv = song_cfg.get("pad_volume", self.args.pad_volume)
        if isinstance(pv, dict):
            engine.pad_volume_map = {
                int(k) - 1: float(v) / 100 for k, v in pv.items()}
        else:
            engine.pad_volume_map = {}
            engine.pad_volume_default = float(pv) / 100
        # targets por canción sobre el mapeo físico global de knobs
        song_pots = song_cfg.get("pots", {})
        self.args.pots.clear()
        self.pot_labels = [None] * 8       # (nº pista, efecto) por knob activo
        for key, entry in self.args.hw_pots.items():
            if not isinstance(entry, dict):
                continue
            spec = parse_button_spec(entry.get("cc", ""))
            target = parse_pot_target(song_pots.get(key, ""))
            if spec is None or target is None:
                continue
            try:
                idx = int(key[3:]) - 1     # "pot3" -> 2
            except ValueError:
                continue
            if not 0 <= idx < 8:
                continue
            self.args.pots.append((spec, target, idx))
            self.pot_labels[idx] = (target[0] + 1, target[1])

    # -- UI curses --------------------------------------------------------------

    def _poll_buttons(self, context: str) -> str | None:
        """Traduce una acción de botón pendiente a la tecla equivalente."""
        try:
            action = self.ui_queue.get_nowait()
        except queue.Empty:
            return None
        if context == "list":
            return {"up": "up", "down": "down",
                    "play": "\n", "stop": "c"}.get(action)
        return {"up": "p", "down": "n",
                "play": " ", "stop": "q"}.get(action)

    def _drain_buttons(self):
        while True:
            try:
                self.ui_queue.get_nowait()
            except queue.Empty:
                return

    def _set_notice(self, msg: str):
        """Aviso breve en la línea inferior de la UI (thread-safe)."""
        self._notice = (msg, time.time())

    def _draw_notice(self, scr, curses, y: int):
        if self._notice is not None:
            msg, ts = self._notice
            if time.time() - ts < 3.0:
                try:
                    scr.addstr(y, 1, msg[:60],
                               curses.color_pair(5) | curses.A_BOLD)
                except curses.error:
                    pass
            else:
                self._notice = None

    def _draw_list(self, scr, curses):
        """ROBOTRACA a texto de consola + 3 canciones centradas (scroll
        infinito): prev/next con medio-bloques, seleccionada al doble."""
        scr.erase()
        h, w = scr.getmaxyx()
        names = [display_name(p.name) for p in self.projects]
        n = len(names)
        prev = names[(self.index - 1) % n]
        current = names[self.index]
        nxt = names[(self.index + 1) % n]
        sel_scale = 2 if len(current) * 8 <= w else 1
        # bloque: título (1) + hueco + prev (3) + seleccionada + next (3)
        total_rows = 2 + 3 + 5 * sel_scale + 1 + 3
        y0 = max(0, (h - total_rows) // 2)
        title = "R O B O T R A C A"
        scr.addstr(y0, max(0, (w - len(title)) // 2), title,
                   self._pair_bright)
        y = y0 + 2
        big_text_half(scr, y, max(0, (w - len(prev) * 4) // 2), prev,
                      self._pair_dim)
        y += 3 + 1
        big_text(scr, y, max(0, (w - len(current) * 4 * sel_scale) // 2),
                 current, sel_scale, self._pair_sel)
        y += 5 * sel_scale + 1
        big_text_half(scr, y, max(0, (w - len(nxt) * 4) // 2), nxt,
                      self._pair_dim)
        self._draw_notice(scr, curses, h - 1)
        scr.refresh()

    # -- visualizador en directo (espectro reactivo) ----------------------------

    def _enter_song_view(self, scr):
        """Ajusta refresco y análisis según el modo de vista de canción:
        el visualizador va a ~30 fps (necesita fluidez); la vista detallada
        a 10 fps basta y ahorra CPU en la Pi."""
        self._want_viz = self._view_mode == "viz"
        self._viz_bands = None            # reinicia envolvente al entrar
        self._viz_peaks = None
        scr.timeout(33 if self._want_viz else 100)

    def _viz_layout(self, nbands: int):
        """Bordes de bin, pesos y color por barra para `nbands` bandas
        log-espaciadas. Cacheado: solo cambia si cambia el ancho del terminal."""
        cached = self._viz_layout_cache.get(nbands)
        if cached is not None:
            return cached
        sr = self.args.samplerate
        nbins = VIZ_NFFT // 2 + 1
        freqs = np.logspace(np.log10(VIZ_FMIN),
                            np.log10(min(VIZ_FMAX, sr / 2 * 0.95)), nbands + 1)
        edges, weights, colors = [], [], []
        ng = len(self._viz_gradient)
        for i in range(nbands):
            lo = int(freqs[i] / (sr / 2) * (nbins - 1))
            hi = max(lo + 1, int(freqs[i + 1] / (sr / 2) * (nbins - 1)))
            edges.append((lo, min(hi, nbins)))
            center = math.sqrt(freqs[i] * freqs[i + 1])
            weights.append((center / VIZ_FMIN) ** VIZ_TILT)
            colors.append(self._viz_gradient[min(ng - 1, i * ng // nbands)])
        layout = (edges, np.array(weights, dtype=np.float32), colors)
        self._viz_layout_cache[nbands] = layout
        return layout

    def _viz_levels(self, nbands: int) -> np.ndarray:
        """Nivel 0-1 por banda del último audio (rFFT de la ventana rodante),
        con realce de agudos, auto-ganancia y compresión suave para que
        ninguna banda domine ni se quede clavada arriba."""
        edges, weights, _ = self._viz_layout(nbands)
        spec = np.abs(np.fft.rfft(self._viz_ring * self._viz_win)) / VIZ_NFFT
        raw = np.array([spec[lo:hi].mean() for lo, hi in edges],
                       dtype=np.float32) * weights
        # referencia adaptativa: sube rápido, baja despacio
        peak = float(raw.max())
        ref = self._viz_agc
        rate = VIZ_AGC_UP if peak > ref else VIZ_AGC_DOWN
        ref += rate * (peak - ref)
        self._viz_agc = max(ref, VIZ_AGC_REF)
        return 1.0 - np.exp(-raw / self._viz_agc * VIZ_AGC_KNEE)

    def _draw_viz(self, scr, curses, engine: Engine):
        """Barras de espectro a pantalla completa, reactivas al audio."""
        scr.erase()
        h, w = scr.getmaxyx()
        # cabecera mínima: estado + canción + BPM (el resto es visual)
        if engine.finished:
            sym, cpair = "[]", 6
        elif engine.playing:
            sym, cpair = ">", 2
        else:
            sym, cpair = "II", 5
        name = display_name(engine.project.dir.name)
        head = f"{sym} {name}  {engine.tempo}BPM"
        scr.addstr(0, 1, head[:w - 2], curses.color_pair(cpair) | curses.A_BOLD)

        rows = h - 1                         # filas para barras (fila 0 = cabecera)
        if rows < 2 or w < VIZ_ZONES * 2:
            self._draw_notice(scr, curses, h - 1)
            scr.refresh()
            return
        # La pantalla se reparte en 8 zonas, una por knob: cada zona lleva su
        # tramo del espectro y, si el knob está asignado en esta canción, el
        # valor del knob encima.
        zone_w = w // VIZ_ZONES
        step = 3 if zone_w >= 6 else 2       # barra de 2 + 1 de hueco (o 1+1)
        bar_w = step - 1
        per_zone = max(1, min((zone_w - 1) // step, VIZ_MAX_BANDS // VIZ_ZONES))
        nbands = per_zone * VIZ_ZONES
        _, _, colors = self._viz_layout(nbands)

        target = self._viz_levels(nbands)
        bands, peaks = self._viz_bands, self._viz_peaks
        if bands is None or len(bands) != nbands:
            bands = np.zeros(nbands, dtype=np.float32)
            peaks = np.zeros(nbands, dtype=np.float32)
        # envolvente por barra: ataque rápido, caída lenta (look de directo)
        rising = target > bands
        bands += np.where(rising, VIZ_ATTACK, VIZ_RELEASE) * (target - bands)
        peaks = np.maximum(peaks - VIZ_PEAK_FALL, bands)
        self._viz_bands, self._viz_peaks = bands, peaks

        values = self.engine_ref.get("pot_values") or [0] * 8
        levels = 8                           # sub-bloques por celda (VIZ_BLOCKS)
        margin = max(0, (w - VIZ_ZONES * zone_w) // 2)
        for z in range(VIZ_ZONES):
            zx = margin + z * zone_w
            for j in range(per_zone):
                i = z * per_zone + j
                x = zx + j * step
                attr = colors[i]
                total = int(bands[i] * rows * levels)
                full, part = divmod(total, levels)
                for r in range(min(full, rows)):
                    a = attr | (curses.A_BOLD if r >= rows * 0.6 else 0)
                    scr.addstr(h - 1 - r, x, "█" * bar_w, a)
                if full < rows and part > 0:
                    scr.addstr(h - 1 - full, x, VIZ_BLOCKS[part] * bar_w, attr)
                pr = int(peaks[i] * rows)
                if 0 < pr <= rows:
                    scr.addstr(h - 1 - min(pr, rows - 1), x, "▁" * bar_w,
                               self._viz_cap)
            self._draw_knob(scr, curses, z, zx, zone_w, rows, h,
                            values[z] if z < len(values) else 0)

        self._draw_notice(scr, curses, h - 1)
        scr.refresh()

    def _draw_knob(self, scr, curses, z: int, zx: int, zone_w: int,
                   rows: int, h: int, value: int):
        """Knob `z` sobre su zona: etiqueta (pista+efecto) y una banda gruesa
        a la altura del valor, estilo fader. Si el knob no está asignado en
        esta canción no se dibuja nada (la zona queda solo con el espectro)."""
        label = self.pot_labels[z] if z < len(self.pot_labels) else None
        if label is None:
            return
        track, effect = label
        wide = max(1, zone_w - 1)
        val = min(max(value, 0), 127) / 127.0
        # etiqueta: "3 SUB" (knob, efecto abreviado); se enciende con el valor
        attr = self._viz_knob if val > 0.01 else self._viz_knob_off
        tag = f"{z + 1}{effect[:3].upper()}"
        scr.addstr(1, zx, tag[:wide], attr)
        head_rows = 2                        # fila 1 etiqueta, fila 2 valor
        if wide >= 6:
            scr.addstr(2, zx, f"{track}:{int(val * 100):3d}"[:wide],
                       self._viz_knob_off)
        else:
            head_rows = 1
        # fader: banda gruesa a la altura del valor (abajo = 0). Se mueve
        # solo por debajo de la etiqueta para no taparla al llegar al tope.
        top = 1 + head_rows
        span = max(1, (h - 1) - top)
        y = h - 1 - int(val * span)
        scr.addstr(y, zx, "▀" * wide, attr)

    def _draw_song(self, scr, curses, engine: Engine):
        scr.erase()
        h, w = scr.getmaxyx()
        wide = w >= 68
        mw = 8 if wide else 4
        if engine.finished:
            state, color = "STOP ", 6
        elif engine.playing:
            state, color = "PLAY ", 2
        else:
            state, color = "PAUSA", 5
        row = max(engine.song_positions())
        scr.addstr(0, 1, state, curses.color_pair(color) | curses.A_BOLD)
        scr.addstr(0, 7, engine.project.dir.name[:w - 8],
                   curses.color_pair(1) | curses.A_BOLD)
        if wide:
            scr.addstr(0, 30, f"{engine.tempo} BPM  fila {row:02X}  "
                              f"{meter(row / 255, 12)}"[:w - 31])
        for ch in engine.channels:
            y = 2 + ch.idx
            v = ch.voice
            sounding = (v is not None and v.active) or ch.midi_note is not None
            if sounding:
                note = v.note if v is not None else ch.last_note
                instr = ch.last_instr if ch.last_instr is not None else 0
                vol = (v.vol_cur / 255.0 * ch.cc_vol) if v is not None else 0.0
                scr.addstr(y, 1, f"{ch.idx + 1} {note_name(note)} {instr:02X}",
                           curses.color_pair(2))
                scr.addstr(y, 10, f"vol {meter(vol, mw)}"[:w - 11])
                x_fx = 13 + mw
                for name, amount in list(ch.fx_amounts.items())[:2]:
                    attr = curses.color_pair(2) if amount > 0.001 \
                        else curses.color_pair(3)
                    tag = name[:3]
                    scr.addstr(y, x_fx, f"{tag} {meter(amount, mw)}"[:w - 1],
                               attr)
                    x_fx += 7 + mw
                if wide:
                    if v is not None and v.cc_pan is not None:
                        pan_val = v.cc_pan
                    elif v is not None:
                        pan_val = v.pan
                    else:
                        pan_val = 127
                    scr.addstr(y, 43, f"pan {pan_meter(pan_val)}")
                    semis = 12.0 * math.log2(ch.cc_pitch)
                    scr.addstr(y, 52, f"pit {semis:+4.1f}")
            else:
                scr.addstr(y, 1, f"{ch.idx + 1} --- --",
                           curses.color_pair(3))
                if ch.idx in engine.muted:
                    scr.addstr(y, 10, "MUTE", curses.color_pair(5))
                elif ch.playing:
                    scr.addstr(y, 10, "·", curses.color_pair(3))
        scr.addstr(h - 1, 1,
                   "espacio: pausa  n/p: canción  v: visor  q: lista"[:w - 2],
                   curses.color_pair(3))
        self._draw_notice(scr, curses, h - 2)
        scr.refresh()

    def _curses_main(self, scr):
        import curses
        try:
            curses.curs_set(0)
        except curses.error:
            pass                      # terminal sin cursor configurable
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK   # consola linux sin orig_pair
        # Paleta ROBOTRACA (terminal Pip-Boy: verde fósforo monocromo,
        # ámbar para estados; se complementa con setvtrgb en tty1)
        curses.init_pair(1, curses.COLOR_GREEN, bg)      # título/brillante
        curses.init_pair(2, curses.COLOR_GREEN, bg)      # selección
        curses.init_pair(3, curses.COLOR_GREEN, bg)      # secundario (dim)
        curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_GREEN)
        curses.init_pair(5, curses.COLOR_YELLOW, bg)     # pausa (ámbar)
        curses.init_pair(6, curses.COLOR_RED, bg)        # stop (ámbar osc)
        self._pair_bright = curses.color_pair(1) | curses.A_BOLD
        self._pair_sel = curses.color_pair(2) | curses.A_BOLD
        self._pair_dim = curses.color_pair(3) | curses.A_DIM
        # Paleta a color solo para el visualizador (gradiente grave->agudo).
        # Fuera del verde fósforo del resto de la UI a propósito: el directo
        # pide color. Sobre tty1 con setvtrgb salen algo lavados pero legibles.
        viz_fg = [curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN,
                  curses.COLOR_CYAN, curses.COLOR_BLUE, curses.COLOR_MAGENTA]
        for i, fg in enumerate(viz_fg):
            curses.init_pair(10 + i, fg, bg)
        curses.init_pair(16, curses.COLOR_WHITE, bg)   # testigo de pico
        self._viz_gradient = [curses.color_pair(10 + i) for i in range(6)]
        self._viz_cap = curses.color_pair(16) | curses.A_BOLD
        # knobs: blanco intenso al estar activos, atenuado en reposo, para
        # que se lean por encima de las barras de color
        self._viz_knob = curses.color_pair(16) | curses.A_BOLD
        self._viz_knob_off = curses.color_pair(16) | curses.A_DIM
        scr.timeout(100)
        engine = None
        needs_clear = True                # limpieza completa al cambiar de vista
        while True:                       # vista lista
            if needs_clear:
                scr.clear()
                needs_clear = False
            self._drain_buttons()
            try:
                self._draw_list(scr, curses)
            except curses.error:
                pass                      # pantalla pequeña: recorte
            key = self._read_key(scr, curses, "list")
            if key is None:
                continue
            if key in ("q", "esc"):
                return
            if key == "c":
                self._config_view(scr, curses)
                self.projects = find_projects(Path(self.args.songs)) or \
                    self.projects
                self.index %= len(self.projects)
                needs_clear = True
            elif key in ("up", "k"):
                self.index = (self.index - 1) % len(self.projects)
            elif key in ("down", "j"):
                self.index = (self.index + 1) % len(self.projects)
            elif key in ("\r", "\n"):
                engine = self._load_song(self.index)
                self._drain_buttons()
                scr.clear()               # limpieza al entrar en la canción
                self._enter_song_view(scr)
                while True:               # vista canción
                    try:
                        if self._view_mode == "viz":
                            self._draw_viz(scr, curses, engine)
                        else:
                            self._draw_song(scr, curses, engine)
                    except curses.error:
                        pass              # pantalla pequeña: recorte
                    key = self._read_key(scr, curses, "song")
                    if key is None:
                        continue
                    if key == " ":
                        engine.push_event(
                            "pause" if engine.playing else "play")
                    elif key == "v":
                        self._view_mode = ("detail" if self._view_mode == "viz"
                                           else "viz")
                        self._enter_song_view(scr)
                        scr.clear()
                    elif key == "n":
                        self.index = (self.index + 1) % len(self.projects)
                        engine = self._load_song(self.index)
                    elif key == "p":
                        self.index = (self.index - 1) % len(self.projects)
                        engine = self._load_song(self.index)
                    elif key in ("q", "esc"):
                        engine.push_event("stop")
                        self.engine_ref["engine"] = None
                        self._want_viz = False
                        scr.timeout(100)
                        needs_clear = True
                        break

    def _config_view(self, scr, curses):
        """Pantalla de configuración: edita lttileplayer.toml en el propio
        dispositivo (audio, MIDI, botones, pots, delay)."""
        from lgpt_setup import POT_DEFAULT_TARGETS, write_config
        cfg_path = Path(self.args.config)
        cfg = load_config(cfg_path)
        cfg.setdefault("audio", {})
        cfg.setdefault("midi", {})
        cfg.setdefault("buttons", {})
        cfg.setdefault("pots", {})

        def current_values():
            b = cfg["buttons"]
            nb = sum(1 for v in b.values() if v)
            npots = sum(1 for v in cfg["pots"].values()
                        if isinstance(v, dict) and v.get("cc"))
            return [
                ("audio", "Salida de audio",
                 cfg["audio"].get("output") or "(por defecto)"),
                ("midi_out", "Salida MIDI",
                 cfg["midi"].get("output") or "(desactivada)"),
                ("midi_in", "Entrada MIDI",
                 cfg["midi"].get("input") or "(auto)"),
                ("buttons", "Capturar botones", f"{nb}/4 asignados"),
                ("pots", "Capturar potenciómetros", f"{npots}/8 asignados"),
                ("save", "» GUARDAR y volver", ""),
                ("back", "» Volver sin guardar", ""),
            ]

        sel = 0
        while True:
            fields = current_values()
            scr.erase()
            h, w = scr.getmaxyx()
            scr.addstr(1, 2, "CONFIGURACIÓN",
                       curses.color_pair(1) | curses.A_BOLD)
            for i, (_key, label, value) in enumerate(fields):
                attr = curses.color_pair(4) if i == sel else 0
                scr.addstr(3 + i, 2, f"{label:<26}"[:26], attr)
                if value:
                    scr.addstr(3 + i, 29, value[:w - 31], attr)
            scr.addstr(h - 2, 2, "↑↓: moverse   enter: editar   "
                                 "q: volver", curses.color_pair(3))
            scr.addstr(h - 1, 2, "audio/puertos MIDI: se aplican al "
                                 "reiniciar el player", curses.color_pair(3))
            scr.refresh()
            key = self._read_key(scr, curses, "list")
            if key is None:
                continue
            if key in ("q", "esc", "c"):
                return
            if key in ("up", "k"):
                sel = (sel - 1) % len(fields)
            elif key in ("down", "j"):
                sel = (sel + 1) % len(fields)
            elif key in ("\r", "\n"):
                action = fields[sel][0]
                if action == "back":
                    return
                if action == "save":
                    write_config(cfg, cfg_path)
                    self._apply_live_config(cfg)
                    return
                if action == "audio":
                    devs = ["(por defecto del sistema)"] + [
                        d["name"] for d in sd.query_devices()
                        if d["max_output_channels"] > 0]
                    cur = cfg["audio"].get("output") or devs[0]
                    val = self._choose(scr, curses, "Salida de audio",
                                       devs, cur)
                    if val is not None:
                        cfg["audio"]["output"] = "" if val == devs[0] else val
                elif action == "midi_out":
                    val = self._choose_midi(scr, curses, "salida",
                                            cfg["midi"].get("output", ""))
                    if val is not None:
                        cfg["midi"]["output"] = val
                elif action == "midi_in":
                    val = self._choose_midi(scr, curses, "entrada",
                                            cfg["midi"].get("input", ""))
                    if val is not None:
                        cfg["midi"]["input"] = val
                elif action == "buttons":
                    self._capture_view(scr, curses, cfg["buttons"],
                                       [("up", "ARRIBA"), ("down", "ABAJO"),
                                        ("play", "PLAY"),
                                        ("stop", "STOP")], None)
                elif action == "pots":
                    entries = []
                    for n in range(1, 9):
                        e = cfg["pots"].get(f"pot{n}", {})
                        target = (e.get("target")
                                  or POT_DEFAULT_TARGETS.get(n, ""))
                        entries.append((f"pot{n}", f"POT {n} ({target or 'libre'})",
                                        target))
                    self._capture_view(scr, curses, cfg["pots"], entries,
                                       POT_DEFAULT_TARGETS)

    def _wait_midi_spec(self, scr, curses) -> str | None:
        """Espera un note on o CC y devuelve el spec; None si se cancela."""
        rq = self.engine_ref.get("raw_queue")
        if rq is not None:
            while True:
                try:
                    rq.get_nowait()
                except queue.Empty:
                    break
        while True:
            key = scr.getch()
            if key in (10, 13, 27, ord("q")):
                return None
            if rq is not None:
                try:
                    mtype, ch, num = rq.get_nowait()
                    if mtype == "note_on":
                        return f"note:{ch}:{num}"
                    if mtype == "control_change":
                        return f"cc:{ch}:{num}"
                except queue.Empty:
                    pass
            time.sleep(0.05)

    def _wait_quiet_midi(self, seconds: float = 0.6):
        """Espera a que deje de llegar MIDI (pot soltado)."""
        rq = self.engine_ref.get("raw_queue")
        quiet_since = None
        while True:
            got = False
            if rq is not None:
                while True:
                    try:
                        rq.get_nowait()
                        got = True
                    except queue.Empty:
                        break
            if got:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.time()
            elif time.time() - quiet_since >= seconds:
                return
            time.sleep(0.05)

    def _apply_live_config(self, cfg: dict):
        """Botones y mapeo físico de knobs se aplican en caliente."""
        self.buttons.clear()
        self.buttons.update({
            a: parse_button_spec(s)
            for a, s in cfg.get("buttons", {}).items()})
        self.args.hw_pots = cfg.get("pots", {})

    # -- widgets de la pantalla CONFIG ------------------------------------------

    def _choose(self, scr, curses, title, options, current) -> str | None:
        sel = options.index(current) if current in options else 0
        while True:
            scr.erase()
            scr.addstr(1, 2, title, curses.color_pair(1) | curses.A_BOLD)
            for i, opt in enumerate(options[:12]):
                attr = curses.color_pair(4) if i == sel else 0
                mark = " *" if opt == current else ""
                scr.addstr(3 + i, 2, f"{opt}{mark}"[:70], attr)
            scr.addstr(14, 2, "↑↓ + enter   q: cancelar", curses.color_pair(3))
            scr.refresh()
            key = self._read_key(scr, curses, "list")
            if key in ("q", "esc"):
                return None
            if key in ("up", "k"):
                sel = (sel - 1) % len(options[:12])
            elif key in ("down", "j"):
                sel = (sel + 1) % len(options[:12])
            elif key in ("\r", "\n"):
                return options[sel]

    def _choose_midi(self, scr, curses, kind, current) -> str | None:
        import mido
        if kind == "salida":
            names = mido.get_output_names()
            options = names + ["virtual", "off"]
        else:
            names = mido.get_input_names()
            options = names + ["auto", "off"]
        cur = current if current in options else (
            "virtual" if current == "virtual" else options[0])
        val = self._choose(scr, curses, f"Puerto MIDI de {kind}:", options, cur)
        if val is None:
            return None
        if kind == "salida":
            return "" if val == "off" else val
        return "" if val == "auto" else val

    def _capture_view(self, scr, curses, store: dict, entries, defaults):
        """Captura de botones/pots: lista acciones, enter espera un evento
        MIDI y guarda el spec. store se edita en vivo."""
        sel = 0
        while True:
            scr.erase()
            h, w = scr.getmaxyx()
            scr.addstr(1, 2, "CAPTURA MIDI", curses.color_pair(1) | curses.A_BOLD)
            for i, entry in enumerate(entries):
                key_name, label = entry[0], entry[1]
                cur = store.get(key_name, "")
                if isinstance(cur, dict):
                    cur = cur.get("cc", "")
                attr = curses.color_pair(4) if i == sel else 0
                scr.addstr(3 + i, 2, f"{label:<28}"[:28], attr)
                scr.addstr(3 + i, 31, (cur or "---")[:w - 33], attr)
            scr.addstr(h - 1, 2, "enter: capturar   d: borrar   q: volver",
                       curses.color_pair(3))
            scr.refresh()
            key = self._read_key(scr, curses, "list")
            if key in ("q", "esc"):
                return
            if key in ("up", "k"):
                sel = (sel - 1) % len(entries)
            elif key in ("down", "j"):
                sel = (sel + 1) % len(entries)
            elif key == "d":
                self._store_spec(store, entries[sel], "", defaults)
            elif key in ("\r", "\n"):
                self._capture_one(scr, curses, store, entries[sel], defaults)

    def _store_spec(self, store, entry, spec, defaults):
        key_name = entry[0]
        if key_name.startswith("pot"):
            n = int(key_name[3:])
            target = entry[2] if len(entry) > 2 else \
                (defaults or {}).get(n, "")
            store[key_name] = {"cc": spec, "target": target}
        else:
            store[key_name] = spec

    def _capture_one(self, scr, curses, store, entry, defaults):
        """Espera un evento MIDI y lo guarda; luego aguarda a que el flujo
        pare (para no capturar el mismo pot dos veces)."""
        self.engine_ref["capture_mode"] = True
        rq = self.engine_ref.get("raw_queue")
        try:
            scr.addstr(2, 31, "esperando MIDI... (enter: cancela)",
                       curses.color_pair(5))
            scr.refresh()
            if rq is not None:
                while True:
                    try:
                        rq.get_nowait()
                    except queue.Empty:
                        break
            spec = None
            while spec is None:
                key = scr.getch()
                if key in (10, 13, 27, ord("q")):
                    return
                if rq is not None:
                    try:
                        mtype, ch, num = rq.get_nowait()
                        if mtype == "note_on":
                            spec = f"note:{ch}:{num}"
                        elif mtype == "control_change":
                            spec = f"cc:{ch}:{num}"
                    except queue.Empty:
                        pass
                time.sleep(0.05)
            self._store_spec(store, entry, spec, defaults)
            scr.addstr(2, 31, f"{spec} — suelta el pot...          ")
            scr.refresh()
            # espera a que el flujo MIDI se detenga (0.6 s en silencio)
            quiet_since = None
            while True:
                got = False
                if rq is not None:
                    while True:
                        try:
                            rq.get_nowait()
                            got = True
                        except queue.Empty:
                            break
                if got:
                    quiet_since = None
                elif quiet_since is None:
                    quiet_since = time.time()
                elif time.time() - quiet_since >= 0.6:
                    return
                time.sleep(0.05)
        finally:
            self.engine_ref["capture_mode"] = False
            scr.addstr(2, 31, " " * 40)

    def _read_key(self, scr, curses, context: str) -> str | None:
        key = scr.getch()
        if key != -1:
            if key in (curses.KEY_UP,):
                return "up"
            if key in (curses.KEY_DOWN,):
                return "down"
            if key == 27:
                return "esc"
            try:
                return chr(key)
            except ValueError:
                return None
        return self._poll_buttons(context)

    def _run_headless(self):
        """Sin TTY: solo audio + botones MIDI (modo servicio)."""
        while True:
            time.sleep(1.0)

    def run(self):
        self.midi_out = open_midi_output(self.args.midi_out)
        self.engine_ref["raw_queue"] = queue.SimpleQueue()
        self.midi_in = open_midi_input(
            self.args.midi, self.engine_ref, self.ui_queue, self.buttons,
            self.args.pots)
        if self.args.record:
            self.recorder = WavRecorder(self.args.record, self.args.samplerate)
            print(f"[audio] grabando salida en {self.args.record}")
        if self.args.stream:
            self.streamer = TcpStreamer(self.args.stream, self.args.samplerate,
                                        on_event=self._set_notice)
            self._set_notice(f"stream puerto {self.args.stream}")
        self.stream.start()
        try:
            if sys.stdin.isatty():
                import curses
                curses.wrapper(self._curses_main)
            else:
                self._run_headless()
        finally:
            engine = self.engine_ref.get("engine")
            if engine is not None:
                engine.panic()
            self.stream.stop()
            self.stream.close()
            if self.recorder is not None:
                self.recorder.close()
            if self.streamer is not None:
                self.streamer.close()
            if self.midi_in is not None:
                self.midi_in.close()
            if self.midi_out is not None:
                self.midi_out._port.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=str(CONFIG_PATH),
                        help="archivo TOML de configuración")
    parser.add_argument("--songs", default=None,
                        help="directorio con proyectos lgpt_*")
    parser.add_argument("--device", default=None,
                        help="dispositivo de salida (nombre o índice PortAudio)")
    parser.add_argument("--midi", default=None,
                        help="puerto MIDI de entrada (nombre parcial)")
    parser.add_argument("--midi-out", default=None, dest="midi_out",
                        help="puerto MIDI de salida (nombre parcial o "
                             "'virtual')")
    parser.add_argument("--samplerate", type=int, default=None)
    parser.add_argument("--blocksize", type=int, default=None)
    parser.add_argument("--delay", type=float, default=None,
                        help="retardo de la salida de audio en segundos")
    parser.add_argument("--record", default=None, metavar="WAV",
                        help="graba la salida de audio a un archivo WAV")
    parser.add_argument("--stream", type=int, default=None, metavar="PUERTO",
                        help="emite la salida por TCP (PCM s16le)")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    audio_cfg = cfg.get("audio", {})
    midi_cfg = cfg.get("midi", {})

    # Prioridad: línea de comandos > lttileplayer.toml > defecto
    args.songs = args.songs or cfg.get("songs_dir", DEFAULT_SONGS_DIR)
    # songs_dir relativo se resuelve contra la carpeta del programa, así el
    # mismo toml sirve en el PC de desarrollo y en la Pi (ambos usan "songs").
    songs_path = Path(args.songs)
    if not songs_path.is_absolute():
        songs_path = CONFIG_PATH.parent / songs_path
    args.songs = str(songs_path)
    args.device = args.device or audio_cfg.get("output") or None
    args.samplerate = args.samplerate or audio_cfg.get("samplerate", SAMPLE_RATE)
    args.blocksize = args.blocksize or audio_cfg.get("blocksize", 512)
    args.delay = args.delay if args.delay is not None else audio_cfg.get(
        "delay", 1.0)
    args.record = args.record or audio_cfg.get("record") or None
    args.stream = (
        args.stream if args.stream is not None
        else audio_cfg.get("stream", 0) or None
    )
    args.midi = args.midi if args.midi is not None else midi_cfg.get("input", "")
    args.midi_out = (
        args.midi_out if args.midi_out is not None
        else midi_cfg.get("output", "")
    )
    args.buttons = {
        action: parse_button_spec(spec)
        for action, spec in cfg.get("buttons", {}).items()
    }
    args.hw_pots = cfg.get("pots", {})     # mapeo físico global (CC por knob)
    args.pots = []                          # targets (se arman por canción)
    args.mute = cfg.get("channels", {}).get("mute", [])
    wd = audio_cfg.get("wavs_dir") or None
    if wd:                                   # relativo -> junto al programa
        wp = Path(wd)
        if not wp.is_absolute():
            wp = CONFIG_PATH.parent / wp
        wd = str(wp)
    args.wavs_dir = wd
    args.pad_volume = audio_cfg.get("pad_volume", 60)

    Player(args).run()


if __name__ == "__main__":
    main()
