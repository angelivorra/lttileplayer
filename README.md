# lttileplayer

Reproductor standalone de canciones de [LittleGPTracker](https://github.com/djdiskmachine/LittleGPTracker)
(LGPT / Little Piggy Tracker) para consola, pensado para una **Raspberry Pi 4
con HAT de audio** (Raspberry Pi OS Lite, headless), con modulación en
directo vía **MIDI CC** sobre cada uno de los 8 canales.

El motor replica el comportamiento del reproductor original (secuenciador
song → chain → phrase, 6 ticks por step sample-accurate, rampas k-rate,
pan law original) limitándose a lo que usan las canciones del proyecto.

## Archivos

- `lgpt_parser.py` — parser de `lgptsav.dat` (XML plano o comprimido LZ77).
- `lgpt_engine.py` — motor de audio puro (numpy): voces, secuenciador,
  mixer. Sin dependencia de tarjeta de audio (testable headless).
- `lgpt_player.py` — reproductor de consola: lista de canciones, salida de
  audio con `sounddevice`, entrada MIDI con `mido`/`python-rtmidi`.
- `tests/test_engine.py` — tests headless (unittest/pytest).

## Dependencias

Python 3 con: `numpy`, `soundfile`, `sounddevice`, `mido`, `python-rtmidi`.

```sh
python3 -m venv .venv
.venv/bin/pip install numpy soundfile sounddevice mido python-rtmidi
```

## Uso

```sh
.venv/bin/python lgpt_player.py --songs /ruta/a/lgpt/songs
```

Opciones: `--device` (salida PortAudio/ALSA, p. ej. el HAT),
`--midi` (nombre parcial del puerto de entrada), `--samplerate`,
`--blocksize` (512 por defecto).

Teclas:

- Lista: `↑`/`↓` o `j`/`k` para moverse, `enter` reproduce, `q` sale.
- Reproducción: `espacio` play/pausa, `n` siguiente, `p` anterior,
  `q` vuelve a la lista.

MIDI CC (canal MIDI 1-8 → canal tracker 0-7):

| CC  | Parámetro                        |
|-----|----------------------------------|
| 1   | cutoff del filtro                |
| 7   | volumen                          |
| 10  | pan                              |
| 20  | pitch (±1 octava, centro en 64)  |

## Tests y benchmark

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python lgpt_engine.py /ruta/a/lgpt_cancion 60   # benchmark headless
```

## Qué está soportado

Medido sobre las canciones del proyecto (abduccion, Bulebule, Energia,
Sartenazo v1):

- Samples WAV (8/16 bits, mono/estéreo, cualquier frecuencia) por nombre.
- Secuenciador song → chain → phrase con transposes y loop de sección.
- Timing: tempo BPM, 6 ticks/step, avance sample-accurate sin deriva.
- Pitch por nota (root note + fine tune), volumen, pan (pan law original),
  master volume, loop oneshot/forward, recorte por `end`.
- Comandos: `VOLM` (rampas), `KILL`, `DLAY`, `LEGA`, `TABL`, `STOP`, `HOP`.
- Tablas (1 fila/tick, 3 columnas, HOP con contador).
- Crush/downsample y filtro LP del upstream (ver limitaciones).
- Comandos MIDI-out (`MDCC`, `MDPG`) se ignoran por diseño.

## Limitaciones conocidas

- **Filtro en modo `scream`**: el original desborda punto fijo int32 a
  propósito; aquí se satura a [-2, 2]. Aproximación pendiente de ajuste
  fino a oído (solo afecta al instrumento "accordion").
- Sin grooves personalizados (ninguna canción los usa), sin instrumentos
  MIDI, sin feedback, slices, oscillator ni ping-pong.
- El comando `STOP` detiene la canción; no hay avance automático a la
  siguiente (se pulsa `n` o `espacio`).
