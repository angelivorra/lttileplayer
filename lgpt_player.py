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
import queue
import select
import sys
import termios
import threading
import time
import tomllib
import tty
from pathlib import Path

import sounddevice as sd

from lgpt_engine import Engine, MidiOut, SAMPLE_RATE

DEFAULT_SONGS_DIR = "/home/angel/LGPT/songs"
CONFIG_PATH = Path(__file__).resolve().parent / "lttileplayer.toml"


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
    """Resuelve un puerto MIDI por nombre parcial."""
    if not names:
        print(f"[midi] no hay puertos MIDI de {what}; desactivado")
        return None
    if wanted:
        for n in names:
            if wanted.lower() in n.lower():
                return n
        print(f"[midi] puerto '{wanted}' no encontrado; disponibles: {names}")
        return None
    return names[0]


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


def open_midi_input(port_name: str | None, engine_ref: dict,
                    ui_queue: queue.SimpleQueue, buttons: dict):
    """Abre el puerto MIDI de entrada: botones a la UI y CCs al engine.

    engine_ref es un dict mutable con la clave "engine": el callback MIDI
    siempre usa el engine actual, aunque se cambie de canción.
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
        # Los botones mapeados tienen prioridad sobre el CC en directo
        action = match_button(buttons, msg)
        if action is not None:
            ui_queue.put(action)
            return
        engine = engine_ref.get("engine")
        if engine is None:
            return
        if msg.type == "control_change":
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


class Keyboard:
    """Lectura no bloqueante de teclas en modo raw (stdin es un TTY)."""

    def __init__(self):
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)

    def __enter__(self):
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc):
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def read(self, timeout: float = 0.0) -> str | None:
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if not r:
            return None
        ch = sys.stdin.read(1)
        if ch == "\x1b":                      # posible secuencia de escape
            r, _, _ = select.select([sys.stdin], [], [], 0.01)
            if r:
                seq = ch + sys.stdin.read(1)
                r, _, _ = select.select([sys.stdin], [], [], 0.01)
                if r:
                    seq += sys.stdin.read(1)
                return {"\x1b[A": "up", "\x1b[B": "down"}.get(seq)
            return "esc"
        return ch


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
        self.stream = sd.OutputStream(
            samplerate=args.samplerate,
            channels=2,
            dtype="float32",
            blocksize=args.blocksize,
            device=args.device or None,
            callback=self._audio_callback,
        )
        self._last_status = ""

    # -- audio ----------------------------------------------------------------

    def _audio_callback(self, outdata, frames, time_info, status):
        engine = self.engine_ref.get("engine")
        if engine is None:
            outdata[:] = 0
            return
        outdata[:] = engine.render(frames)

    def _load_song(self, index: int):
        project_dir = self.projects[index]
        print(f"\rCargando {project_dir.name}...{' ' * 20}", end="", flush=True)
        old = self.engine_ref.get("engine")
        if old is not None:
            old.panic()                   # note off de notas MIDI colgadas
        engine = Engine(project_dir, sample_rate=self.args.samplerate,
                        audio_delay=self.args.delay)
        engine.midi_out = self.midi_out
        engine.start()
        self.engine_ref["engine"] = engine   # swap atómico de referencia
        return engine

    # -- UI -------------------------------------------------------------------

    def _draw_list(self):
        sys.stdout.write("\x1b[2J\x1b[H")      # limpia pantalla
        print("Proyectos LGPT (↑↓/j/k + enter, q salir):\n")
        for i, p in enumerate(self.projects):
            marker = ">" if i == self.index else " "
            print(f" {marker} {p.name}")
        sys.stdout.flush()

    def _draw_status(self, engine: Engine):
        if engine.finished:
            state = "STOP"
        elif engine.playing:
            state = "PLAY"
        else:
            state = "PAUSA"
        row = max(engine.song_positions())
        status = (f"{state} {engine.project.dir.name}  "
                  f"{engine.tempo} BPM  fila {row:02X}  "
                  f"canales {engine.active_channels()}  "
                  f"[espacio=play/pausa n=sig p=ant q=lista]")
        if status != self._last_status:
            self._last_status = status
            sys.stdout.write("\r\x1b[K" + status)
            sys.stdout.flush()

    def _poll_buttons(self, context: str) -> str | None:
        """Traduce una acción de botón pendiente a la tecla equivalente.

        Lista: up/down = moverse, accept/play = seleccionar.
        Canción: play/accept = play/pausa, stop = volver a la lista,
        up/down = anterior/siguiente.
        """
        try:
            action = self.ui_queue.get_nowait()
        except queue.Empty:
            return None
        if context == "list":
            return {"up": "up", "down": "down",
                    "accept": "\n", "play": "\n"}.get(action)
        return {"up": "p", "down": "n",
                "accept": " ", "play": " ", "stop": "q"}.get(action)

    def _drain_buttons(self):
        """Descarta pulsaciones acumuladas al cambiar de vista."""
        while True:
            try:
                self.ui_queue.get_nowait()
            except queue.Empty:
                return

    def run_list(self) -> bool:
        """Menú de selección. Devuelve False para salir del programa."""
        self._drain_buttons()
        self._draw_list()
        while True:
            key = self.kb.read(0.2) or self._poll_buttons("list")
            if key is None:
                continue
            if key in ("q", "esc"):
                return False
            if key in ("up", "k"):
                self.index = (self.index - 1) % len(self.projects)
                self._draw_list()
            elif key in ("down", "j"):
                self.index = (self.index + 1) % len(self.projects)
                self._draw_list()
            elif key in ("\r", "\n"):
                return True

    def run_song(self):
        sys.stdout.write("\x1b[2J\x1b[H")
        engine = self._load_song(self.index)
        self._last_status = ""
        self._drain_buttons()
        while True:
            self._draw_status(engine)
            key = self.kb.read(0.1) or self._poll_buttons("song")
            if key is None:
                continue
            if key == " ":
                engine.push_event("pause" if engine.playing else "play")
            elif key == "n":
                self.index = (self.index + 1) % len(self.projects)
                engine = self._load_song(self.index)
                self._last_status = ""
            elif key == "p":
                self.index = (self.index - 1) % len(self.projects)
                engine = self._load_song(self.index)
                self._last_status = ""
            elif key in ("q", "esc"):
                engine.push_event("stop")
                self.engine_ref["engine"] = None
                return

    def run(self):
        self.midi_out = open_midi_output(self.args.midi_out)
        self.midi_in = open_midi_input(
            self.args.midi, self.engine_ref, self.ui_queue, self.buttons)
        self.stream.start()
        try:
            with Keyboard() as self.kb:
                while self.run_list():
                    self.run_song()
        finally:
            engine = self.engine_ref.get("engine")
            if engine is not None:
                engine.panic()
            self.stream.stop()
            self.stream.close()
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
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    audio_cfg = cfg.get("audio", {})
    midi_cfg = cfg.get("midi", {})

    # Prioridad: línea de comandos > lttileplayer.toml > defecto
    args.songs = args.songs or cfg.get("songs_dir", DEFAULT_SONGS_DIR)
    args.device = args.device or audio_cfg.get("output") or None
    args.samplerate = args.samplerate or audio_cfg.get("samplerate", SAMPLE_RATE)
    args.blocksize = args.blocksize or audio_cfg.get("blocksize", 512)
    args.delay = args.delay if args.delay is not None else audio_cfg.get(
        "delay", 0.0)
    args.midi = args.midi if args.midi is not None else midi_cfg.get("input", "")
    args.midi_out = (
        args.midi_out if args.midi_out is not None
        else midi_cfg.get("output", "")
    )
    args.buttons = {
        action: parse_button_spec(spec)
        for action, spec in cfg.get("buttons", {}).items()
    }

    Player(args).run()


if __name__ == "__main__":
    main()
