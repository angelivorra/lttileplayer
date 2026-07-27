#!/usr/bin/env python3
"""Reproductor standalone de proyectos LittleGPTracker para consola.

Pensado para Raspberry Pi 4 con HAT de audio (headless), con control
en directo por MIDI CC (canal MIDI 1-8 -> canal tracker 0-7):

    CC 1  -> cutoff del filtro   CC 7  -> volumen
    CC 10 -> pan                 CC 20 -> pitch (+-1 octava, centro en 64)

Uso:
    lgpt_player.py [--songs DIR] [--device DEV] [--midi NOMBRE]
                   [--samplerate HZ] [--blocksize N]

Teclas:
    lista:  up/down o j/k moverse, enter reproducir, q salir
    play:   espacio play/pausa, n siguiente, p anterior, q volver a la lista
"""

from __future__ import annotations

import argparse
import select
import sys
import termios
import threading
import time
import tty
from pathlib import Path

import sounddevice as sd

from lgpt_engine import Engine, SAMPLE_RATE

DEFAULT_SONGS_DIR = "/home/angel/LGPT/songs"


def find_projects(songs_dir: Path) -> list[Path]:
    """Proyectos LGPT: directorios que contienen lgptsav.dat."""
    if not songs_dir.is_dir():
        return []
    return sorted(
        (d for d in songs_dir.iterdir()
         if d.is_dir() and (d / "lgptsav.dat").is_file()),
        key=lambda d: d.name.lower(),
    )


def open_midi(port_name: str | None, engine_ref: dict):
    """Abre un puerto MIDI de entrada y encola los CC al engine activo.

    engine_ref es un dict mutable con la clave "engine": el callback MIDI
    siempre usa el engine actual, aunque se cambie de canción.
    """
    try:
        import mido
    except ImportError:
        print("[midi] mido no disponible; control MIDI desactivado")
        return None

    names = mido.get_input_names()
    if not names:
        print("[midi] no hay puertos MIDI de entrada; desactivado")
        return None

    chosen = None
    if port_name:
        for n in names:
            if port_name.lower() in n.lower():
                chosen = n
                break
        if chosen is None:
            print(f"[midi] puerto '{port_name}' no encontrado; "
                  f"disponibles: {names}")
            return None
    else:
        chosen = names[0]

    def on_message(msg):
        engine = engine_ref.get("engine")
        if engine is None:
            return
        if msg.type == "control_change":
            engine.push_event("cc", msg.channel % 8, msg.control, msg.value)

    port = mido.open_input(chosen, callback=on_message)
    print(f"[midi] escuchando en '{chosen}'")
    return port


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
        self.projects = find_projects(Path(args.songs))
        if not self.projects:
            sys.exit(f"No se encuentran proyectos LGPT en {args.songs}")
        self.index = 0
        self.engine_ref: dict = {}
        self.midi_port = None
        self.stream = sd.OutputStream(
            samplerate=args.samplerate,
            channels=2,
            dtype="float32",
            blocksize=args.blocksize,
            device=args.device,
            callback=self._audio_callback,
        )
        self._status_lock = threading.Lock()
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
        engine = Engine(project_dir, sample_rate=self.args.samplerate)
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
        with self._status_lock:
            if status != self._last_status:
                self._last_status = status
                sys.stdout.write("\r\x1b[K" + status)
                sys.stdout.flush()

    def run_list(self) -> bool:
        """Menú de selección. Devuelve False para salir del programa."""
        self._draw_list()
        while True:
            key = self.kb.read(0.2)
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
        while True:
            self._draw_status(engine)
            key = self.kb.read(0.1)
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
        self.midi_port = open_midi(self.args.midi, self.engine_ref)
        self.stream.start()
        try:
            with Keyboard() as self.kb:
                while self.run_list():
                    self.run_song()
        finally:
            self.stream.stop()
            self.stream.close()
            if self.midi_port is not None:
                self.midi_port.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--songs", default=DEFAULT_SONGS_DIR,
                        help="directorio con proyectos lgpt_*")
    parser.add_argument("--device", default=None,
                        help="dispositivo de salida (nombre o índice PortAudio)")
    parser.add_argument("--midi", default=None,
                        help="nombre (parcial) del puerto MIDI de entrada")
    parser.add_argument("--samplerate", type=int, default=SAMPLE_RATE)
    parser.add_argument("--blocksize", type=int, default=512)
    args = parser.parse_args()
    Player(args).run()


if __name__ == "__main__":
    main()
