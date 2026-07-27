# lttileplayer

Reproductor standalone de canciones de [LittleGPTracker](https://github.com/djdiskmachine/LittleGPTracker)
(LGPT / Little Piggy Tracker) para consola, pensado para una **Raspberry Pi 4
con HAT de audio** (Raspberry Pi OS Lite, headless), con modulación en
directo vía **MIDI CC** sobre cada uno de los 8 canales.

El motor replica el comportamiento del reproductor original (secuenciador
song → chain → phrase, 6 ticks por step sample-accurate, rampas k-rate,
pan law original) limitándose a lo que usan las canciones del proyecto.

## Archivos

- `lttileplayer.toml` — configuración: carpeta de canciones, dispositivo
  de salida de audio, puertos MIDI de entrada y salida, botones.
- `lgpt_setup.py` — asistente de configuración (terminal/SSH).
- `lgpt_parser.py` — parser de `lgptsav.dat` (XML plano o comprimido LZ77).
- `lgpt_engine.py` — motor de audio puro (numpy): voces, secuenciador,
  mixer. Sin dependencia de tarjeta de audio (testable headless).
- `lgpt_player.py` — reproductor de consola: lista de canciones, salida de
  audio con `sounddevice`, entrada/salida MIDI con `mido`/`python-rtmidi`.
- `tests/` — tests headless (unittest/pytest).

## Dependencias

Python 3 con: `numpy`, `soundfile`, `sounddevice`, `mido`, `python-rtmidi`.

```sh
python3 -m venv .venv
.venv/bin/pip install numpy soundfile sounddevice mido python-rtmidi
```

## Configuración

Asistente interactivo (terminal, funciona también por SSH):

```sh
.venv/bin/python lgpt_setup.py
```

Pregunta la salida de audio (lista numerada de dispositivos), la salida
MIDI, la entrada MIDI y luego pide pulsar cada botón del controlador:
**arriba, abajo, aceptar, play, stop** (captura note on o CC). En los
menús, pulsar **enter** conserva el valor guardado en el sistema; en la
captura de botones, enter deja el botón sin asignar. El resultado se
guarda en `lttileplayer.toml`:

```toml
songs_dir = "/home/angel/LGPT/songs"   # carpeta con proyectos lgpt_*

[audio]
output = ""          # salida de audio (nombre/índice PortAudio; "" = defecto)
samplerate = 44100
blocksize = 512
delay = 0.0          # retardo del audio en segundos (típico 0.5-1.0)

[midi]
input = ""           # entrada MIDI CC ("" = primera disponible, "off" = no)
output = "virtual"   # salida MIDI de eventos LGPT ("virtual" = puerto ALSA
                     # nuevo 'lttileplayer'; nombre parcial; "" = desactivada)

[buttons]            # "note:canal:nota" o "cc:canal:control"; "" = sin asignar
up = "note:0:36"
down = "note:0:37"
accept = "note:0:38"
play = "cc:0:41"
stop = "cc:0:42"
```

Los argumentos de línea de comandos (`--songs`, `--device`, `--midi`,
`--midi-out`, `--samplerate`, `--blocksize`, `--delay`, `--config`)
tienen prioridad sobre el archivo.

## Uso

```sh
.venv/bin/python lgpt_player.py
```

Teclas y botones MIDI asignados:

- Lista: `↑`/`↓` (o botones **arriba**/**abajo**) para moverse,
  `enter` (o **aceptar**/**play**) reproduce, `q` sale.
- Reproducción: `espacio` (o **play**/**aceptar**) play/pausa,
  `n`/`p` (o **abajo**/**arriba**) siguiente/anterior,
  `q` (o **stop**) vuelve a la lista.

MIDI CC de entrada (canal MIDI 1-8 → canal tracker 0-7):

| CC  | Parámetro                        |
|-----|----------------------------------|
| 1   | cutoff del filtro                |
| 7   | volumen                          |
| 10  | pan                              |
| 20  | pitch (±1 octava, centro en 64)  |

Salida MIDI: todos los eventos MIDI que genera LGPT salen por el puerto
configurado para que los recoja otro programa o sintetizador:

- Note on/off de los instrumentos MIDI (0x80-0x8F), con su canal,
  volumen (CC7 al disparar) y `note length`.
- `MDCC` (CC arbitrario), `MDPG` (program change), `VOLM` (CC7) y
  `MVEL` (velocity) cuando el canal tiene un instrumento MIDI activo.
- Al cambiar de canción o salir se envía note off de las notas activas.

Los eventos MIDI salen **en tiempo real**, al compás del secuenciador.
Si se configura `[audio] delay` (o `--delay`), solo el audio se retrasa:
útil cuando otro programa recibe el MIDI por red y suena con latencia —
el audio local se retrasa lo mismo para mantenerse sincronizado.

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
- Instrumentos MIDI: note on/off, `MDCC`, `MDPG`, `MVEL` y `VOLM` → CC7,
  emitidos por el puerto MIDI de salida configurado.

## Limitaciones conocidas

- **Filtro en modo `scream`**: el original desborda punto fijo int32 a
  propósito; aquí se satura a [-2, 2]. Aproximación pendiente de ajuste
  fino a oído (solo afecta al instrumento "accordion").
- Sin grooves personalizados (ninguna canción los usa), sin feedback,
  slices, oscillator ni ping-pong.
- El comando `STOP` detiene la canción; no hay avance automático a la
  siguiente (se pulsa `n` o `espacio`).
