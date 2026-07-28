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
DEFAULT_SONGS_DIR = "/home/angel/Documentos/canciones/"
DEFAULT_DELAY = 1.0

BUTTON_ACTIONS = [
    ("up", "ARRIBA (anterior en la lista)"),
    ("down", "ABAJO (siguiente en la lista)"),
    ("play", "PLAY (reproducir / pausa)"),
    ("stop", "STOP (parar / volver a la lista)"),
]

POT_COUNT = 8
# Targets por defecto de cada pot ("canal:parametro"; canal 2 = columna 3,
# el bajo en abduccion). Editables a mano en el TOML.
POT_DEFAULT_TARGETS = {
    1: "2:lp_cutoff",
    2: "2:volume",
    3: "2:pan",
    4: "2:pitch",
    5: "2:lp_res",
}


def load_existing() -> dict:
    if CONFIG_PATH.is_file():
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    return {}


def ask_songs_dir(current: str) -> str:
    """Carpeta de canciones; enter conserva el valor actual."""
    try:
        raw = input(f"\nCarpeta de canciones (enter = {current}): ").strip()
    except EOFError:
        print()
        return current
    if not raw:
        return current
    path = Path(raw).expanduser()
    if not path.is_dir():
        print(f"  (aviso: {path} no existe todavía)")
    return str(path)


def ask_delay(current: float) -> float:
    """Delay del audio en segundos; enter conserva el valor actual."""
    while True:
        try:
            raw = input(f"\nDelay del audio en segundos "
                        f"(enter = {current}): ").strip()
        except EOFError:
            print()
            return current
        if not raw:
            return current
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
        print("Valor no válido.")


def choose(title: str, options: list[str], current_index: int = 0) -> int:
    """Menú numerado por líneas. Enter conserva la opción actual."""
    print(f"\n{title}")
    for i, opt in enumerate(options):
        mark = "  <- actual" if i == current_index else ""
        print(f"  {i}) {opt}{mark}")
    while True:
        try:
            raw = input(f"Elige 0-{len(options) - 1} "
                        f"(enter = {options[current_index]}): ").strip()
        except EOFError:
            print()
            return current_index
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
        if sys.stdin.readline() == "":
            break                                # EOF


def _drain_midi(port):
    """Descarta todos los eventos MIDI pendientes."""
    for _msg in port.iter_pending():
        pass


def wait_quiet(port, seconds: float = 0.6):
    """Espera a que dejen de llegar eventos MIDI (el pot se ha soltado)."""
    quiet_since = None
    while True:
        got = False
        for _msg in port.iter_pending():
            got = True
        if got:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.time()
        elif time.time() - quiet_since >= seconds:
            return
        time.sleep(0.05)


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
            line = sys.stdin.readline()
            if line == "":
                return current                # EOF: conservar lo guardado
            return ""                         # enter: sin asignar
        for msg in port.iter_pending():
            if msg.type == "note_on" and msg.velocity > 0:
                spec = f"note:{msg.channel}:{msg.note}"
            elif msg.type == "control_change" and msg.value > 0:
                spec = f"cc:{msg.channel}:{msg.control}"
            else:
                continue
            print(f"  -> {spec}")
            return spec


def write_config(cfg: dict, path: Path = CONFIG_PATH):
    audio = cfg["audio"]
    midi = cfg["midi"]
    buttons = cfg["buttons"]
    pots = cfg["pots"]
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
        f'record = "{audio.get("record", "")}"',
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
    lines += [
        "",
        "[pots]",
        '# Potenciómetros: cc = "cc:canal:control" (capturado aquí),',
        '# target = "canal:parametro" (canal LGPT 0-7; parametro:',
        '# lp_cutoff, lp_res, volume, pan, pitch; "" = sin asignar).',
    ]
    for n in range(1, POT_COUNT + 1):
        entry = pots.get(f"pot{n}", {})
        cc = entry.get("cc", "")
        target = entry.get("target", "")
        lines.append(f'pot{n} = {{ cc = "{cc}", target = "{target}" }}')
    path.write_text("\n".join(lines) + "\n")
    print(f"\nConfiguración guardada en {path}")


def main():
    cfg = load_existing()
    audio = cfg.get("audio", {})
    midi = cfg.get("midi", {})
    buttons = cfg.get("buttons", {})
    pots = cfg.get("pots", {})

    print("=== Configuración de lttileplayer ===")

    songs_dir = ask_songs_dir(cfg.get("songs_dir") or DEFAULT_SONGS_DIR)
    audio["output"] = pick_audio_output(audio.get("output", ""))
    audio["delay"] = ask_delay(float(audio.get("delay", DEFAULT_DELAY)))

    try:
        import mido
    except ImportError:
        sys.exit("mido no está disponible en este entorno")

    midi["output"] = pick_midi_output(
        mido.get_output_names(), midi.get("output", ""))
    midi["input"] = pick_midi_input(
        mido.get_input_names(), midi.get("input", ""))

    if midi["input"] and midi["input"] != "off":
        with mido.open_input(midi["input"]) as port:
            print("\n--- Captura de botones ---")
            for action, label in BUTTON_ACTIONS:
                buttons[action] = capture_button(
                    port, label, buttons.get(action, ""))
            print("\n--- Captura de potenciómetros ---")
            print("Gira cada pot y suéltalo: espero a que pare el flujo "
                  "MIDI antes de pedir el siguiente.")
            for n in range(1, POT_COUNT + 1):
                key = f"pot{n}"
                entry = pots.get(key, {})
                if not isinstance(entry, dict):
                    entry = {}
                target = entry.get("target") or POT_DEFAULT_TARGETS.get(n, "")
                label = f"POTENCIÓMETRO {n} ({target or 'libre'})"
                spec = capture_button(port, label, entry.get("cc", ""))
                wait_quiet(port)
                pots[key] = {"cc": spec, "target": target}
    else:
        print("\nSin entrada MIDI: botones y pots sin asignar.")
        buttons = {action: "" for action, _ in BUTTON_ACTIONS}
        pots = {}

    write_config({
        "songs_dir": songs_dir,
        "audio": {
            "output": audio["output"],
            "samplerate": audio.get("samplerate", 44100),
            "blocksize": audio.get("blocksize", 512),
            "delay": audio["delay"],
            "record": audio.get("record", ""),
        },
        "midi": midi,
        "buttons": buttons,
        "pots": pots,
    })


if __name__ == "__main__":
    main()
