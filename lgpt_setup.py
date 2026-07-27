#!/usr/bin/env python3
"""Asistente de configuración de lttileplayer.

Interactivo, por líneas: funciona en cualquier terminal, también por SSH.
Pregunta la salida de audio, la salida MIDI, la entrada MIDI y los botones
del controlador (arriba, abajo, aceptar, play, stop), y escribe el
resultado en `lttileplayer.toml`.

Uso:
    .venv/bin/python lgpt_setup.py
"""

from __future__ import annotations

import select
import sys
import time
import tomllib
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "lttileplayer.toml"

BUTTON_ACTIONS = [
    ("up", "ARRIBA (anterior en la lista)"),
    ("down", "ABAJO (siguiente en la lista)"),
    ("accept", "ACEPTAR (seleccionar)"),
    ("play", "PLAY (reproducir / pausa)"),
    ("stop", "STOP (parar / volver a la lista)"),
]


def load_existing() -> dict:
    if CONFIG_PATH.is_file():
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    return {}


def choose(title: str, options: list[str], current_index: int = 0) -> int:
    """Menú numerado por líneas. Enter conserva la opción actual."""
    print(f"\n{title}")
    for i, opt in enumerate(options):
        mark = "  <- actual" if i == current_index else ""
        print(f"  {i}) {opt}{mark}")
    while True:
        raw = input(f"Elige 0-{len(options) - 1} "
                    f"(enter = {options[current_index]}): ").strip()
        if not raw:
            return current_index
        try:
            idx = int(raw)
            if 0 <= idx < len(options):
                return idx
        except ValueError:
            pass
        print("Opción no válida.")


def pick_audio_output(current: str) -> str:
    import sounddevice as sd

    devices = sd.query_devices()
    names = [d["name"] for d in devices if d["max_output_channels"] > 0]
    options = ["(por defecto del sistema)"] + names
    current_index = 0
    if current:
        for i, name in enumerate(names):
            if current in name:
                current_index = i + 1
                break
    idx = choose("Salida de audio:", options, current_index)
    return "" if idx == 0 else names[idx - 1]


def _find_port_index(names: list[str], stored: str) -> int | None:
    for i, name in enumerate(names):
        if stored == name or stored in name:
            return i
    return None


def pick_midi_output(names: list[str], current: str) -> str:
    options = names + ["virtual (puerto ALSA nuevo 'lttileplayer')",
                       "off (desactivado)"]
    if current == "virtual":
        current_index = len(names)
    elif current == "":
        current_index = len(names) + 1
    else:
        current_index = _find_port_index(names, current) or 0
    idx = choose("Puerto MIDI de salida:", options, current_index)
    if idx < len(names):
        return names[idx]
    return "virtual" if idx == len(names) else ""


def pick_midi_input(names: list[str], current: str) -> str:
    options = names + ["auto (primero disponible)", "off (desactivado)"]
    if current == "":
        current_index = len(names)
    elif current == "off":
        current_index = len(names) + 1
    else:
        current_index = _find_port_index(names, current) or 0
    idx = choose("Puerto MIDI de entrada:", options, current_index)
    if idx < len(names):
        return names[idx]
    return "" if idx == len(names) else "off"


def _drain_stdin():
    """Descarta todo el teclado pendiente (enters de menús anteriores)."""
    while select.select([sys.stdin], [], [], 0)[0]:
        sys.stdin.readline()


def _drain_midi(port):
    """Descarta todos los eventos MIDI pendientes."""
    for _msg in port.iter_pending():
        pass


def capture_button(port, label: str, current: str) -> str:
    """Espera un evento MIDI (note on o CC) y lo devuelve como spec.

    Enter deja el botón sin asignar ("")."""
    print(f"\nPulsa el botón {label} "
          f"(enter = sin asignar, actual: {current or 'ninguno'})")
    # Pausa + limpieza para no capturar enters de los menús ni eventos
    # MIDI antiguos (botón aún pulsado de la captura anterior)
    _drain_stdin()
    _drain_midi(port)
    time.sleep(0.4)
    _drain_stdin()
    _drain_midi(port)
    while True:
        r, _, _ = select.select([sys.stdin], [], [], 0.1)
        if r:
            sys.stdin.readline()
            return ""
        for msg in port.iter_pending():
            if msg.type == "note_on" and msg.velocity > 0:
                spec = f"note:{msg.channel}:{msg.note}"
            elif msg.type == "control_change" and msg.value > 0:
                spec = f"cc:{msg.channel}:{msg.control}"
            else:
                continue
            print(f"  -> {spec}")
            return spec


def write_config(cfg: dict):
    audio = cfg["audio"]
    midi = cfg["midi"]
    buttons = cfg["buttons"]
    lines = [
        "# Configuración de lttileplayer (generada por lgpt_setup.py).",
        "# Los argumentos de línea de comandos tienen prioridad.",
        "",
        f'songs_dir = "{cfg["songs_dir"]}"',
        "",
        "[audio]",
        f'output = "{audio["output"]}"',
        f'samplerate = {audio["samplerate"]}',
        f'blocksize = {audio["blocksize"]}',
        f'delay = {audio["delay"]}',
        "",
        "[midi]",
        f'input = "{midi["input"]}"',
        f'output = "{midi["output"]}"',
        "",
        "[buttons]",
        '# Botones del controlador: "note:canal:nota" o "cc:canal:control",',
        '# "" = sin asignar.',
    ]
    for action, _label in BUTTON_ACTIONS:
        lines.append(f'{action} = "{buttons.get(action, "")}"')
    CONFIG_PATH.write_text("\n".join(lines) + "\n")
    print(f"\nConfiguración guardada en {CONFIG_PATH}")


def main():
    cfg = load_existing()
    audio = cfg.get("audio", {})
    midi = cfg.get("midi", {})
    buttons = cfg.get("buttons", {})

    print("=== Configuración de lttileplayer ===")

    audio["output"] = pick_audio_output(audio.get("output", ""))

    try:
        import mido
    except ImportError:
        sys.exit("mido no está disponible en este entorno")

    midi["output"] = pick_midi_output(
        mido.get_output_names(), midi.get("output", ""))
    midi["input"] = pick_midi_input(
        mido.get_input_names(), midi.get("input", ""))

    if midi["input"] and midi["input"] != "off":
        print("\n--- Captura de botones ---")
        with mido.open_input(midi["input"]) as port:
            for action, label in BUTTON_ACTIONS:
                buttons[action] = capture_button(
                    port, label, buttons.get(action, ""))
    else:
        print("\nSin entrada MIDI: botones sin asignar.")
        buttons = {action: "" for action, _ in BUTTON_ACTIONS}

    write_config({
        "songs_dir": cfg.get("songs_dir", "/home/angel/LGPT/songs"),
        "audio": {
            "output": audio["output"],
            "samplerate": audio.get("samplerate", 44100),
            "blocksize": audio.get("blocksize", 512),
            "delay": audio.get("delay", 0.0),
        },
        "midi": midi,
        "buttons": buttons,
    })


if __name__ == "__main__":
    main()
