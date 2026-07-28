# ROBOTRACA (lttileplayer)

Reproductor standalone de canciones de [LittleGPTracker](https://github.com/djdiskmachine/LittleGPTracker)
(LGPT / Little Piggy Tracker) para consola, pensado para una **Raspberry Pi 4
con HAT de audio** (Raspberry Pi OS Lite, arranque kiosk en HDMI), con
modulación en directo vía **MIDI CC** sobre cada uno de los 8 canales.
Estética Pip-Boy: consola verde fósforo, título ROBOTRACA y lista de
canciones a lo grande.

El motor replica el comportamiento del reproductor original (secuenciador
song → chain → phrase, 6 ticks por step sample-accurate, grooves por canal,
rampas k-rate, pan law original) limitándose a lo que usan las canciones
del proyecto.

## Archivos

- `lttileplayer.toml` — configuración fijada del sistema (audio, MIDI,
  botones, pots, delay).
- `deploy.sh` — despliegue completo a la Pi (código, canciones, venv,
  config, arranque kiosk).
- `lgpt_setup.py` — asistente de configuración por terminal (opcional).
- `lgpt_parser.py` — parser de `lgptsav.dat` (XML plano o comprimido LZ77).
- `lgpt_engine.py` — motor de audio puro (numpy): voces, secuenciador,
  mixer. Sin dependencia de tarjeta de audio (testable headless).
- `lgpt_player.py` — reproductor: UI curses retro (estética Pip-Boy),
  salida de audio con `sounddevice`, entrada/salida MIDI.
- `tests/` — tests headless (unittest/pytest).

## Dependencias

Python 3 con: `numpy`, `soundfile`, `sounddevice`, `mido`, `python-rtmidi`.

```sh
python3 -m venv .venv
.venv/bin/pip install numpy soundfile sounddevice mido python-rtmidi
```

## Configuración

La configuración está fijada en `lttileplayer.toml` (incluida en el repo):

- `songs_dir`: carpeta de canciones.
- `[audio]`: salida (HAT), samplerate, blocksize, `delay` (retardo del
  audio en segundos), `record` (WAV de salida, vacío = no grabar).
- `[midi]`: entrada del controlador (nombre parcial, sin el nº de cliente
  ALSA que cambia entre arranques) y salida de eventos LGPT.
- `[buttons]`: 4 botones del controlador (up/down/play/stop) como
  `note:canal:nota` o `cc:canal:control`.
- `[pots]`: 8 potenciómetros `potN = { cc = "cc:canal:control",
  target = "canal:parametro" }` con `parametro` ∈ `lp_cutoff`, `lp_res`,
  `volume`, `pan`, `pitch`.

Los argumentos de línea de comandos (`--songs`, `--device`, `--midi`,
`--midi-out`, `--samplerate`, `--blocksize`, `--delay`, `--record`,
`--config`) tienen prioridad sobre el archivo. Si algún día hace falta
reconfigurar, hay un asistente por terminal: `.venv/bin/python lgpt_setup.py`.

## Uso

```sh
.venv/bin/python lgpt_player.py [--record salida.wav]
```

`--record` (o `record = "ruta.wav"` en `[audio]`) graba a WAV exactamente
lo que sale por el stream (incluye el delay configurado), sin bloquear
el callback de audio.

Controles (botones MIDI o teclado):

- Lista: **arriba/abajo** para moverse (scroll infinito, 3 canciones),
  **play** reproduce, **stop** abre la pantalla de captura de botones/pots.
- Reproducción: **play** = play/pausa, **arriba/abajo** =
  anterior/siguiente canción, **stop** = volver a la lista.

Modulación en directo con los pots configurados. Cada pot tiene un
`target = canal:parametro` donde el canal es 0-7 (0 = columna 1) y el
parámetro puede ser:

| Parámetro   | Efecto                                             |
|-------------|----------------------------------------------------|
| `lp_cutoff` | Filtro low-pass del canal (barrido 40 Hz - 16 kHz) |
| `lp_res`    | Resonancia del low-pass (Q 0.5 - 10, efecto acid)  |
| `volume`    | Volumen del canal                                  |
| `pan`       | Pan del canal                                      |
| `pitch`     | Pitch del canal (±1 octava, centro en 64)          |

Si no hay pots configurados se usa el mapeo por defecto CC1=cutoff,
CC7=volumen, CC10=pan, CC20=pitch (canal MIDI 1-8 → canal tracker 0-7).

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

## Despliegue en la Raspberry Pi

```sh
./deploy.sh [host_ssh]     # por defecto "Lgpt"
```

El script (idempotente) sincroniza el código a `/home/angel/lttileplayer`,
copia las canciones a `/home/angel/Documentos/canciones`, crea el venv,
escribe la config para la Pi (salida = HAT de audio, entrada MIDI,
salida MIDI = puerto virtual `lttileplayer`) y deja el arranque
configurado: **autologin en tty1** con el player a pantalla completa
(kiosk: si el proceso termina, agetty lo relanza). Desactiva el antiguo
`lgpt.service`/`rc.local` y conecta la salida MIDI a `movida:in` si el
bridge TCP-MIDI está presente.

La UI se ve en la pantalla HDMI de la Pi y se controla con los botones
MIDI (el teclado de la consola también funciona). Para capturar
pots/botones en la Pi, ejecuta allí el asistente por SSH:
`/home/angel/lttileplayer/.venv/bin/python /home/angel/lttileplayer/lgpt_setup.py`
(escribe el mismo `lttileplayer.toml`; reinicia el player después con
Ctrl+Alt+Supr o `sudo pkill -f lgpt_player`).

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
- Timing: tempo BPM, avance sample-accurate sin deriva y grooves por
  canal (patrón de longitudes de step en ticks; Bulebule usa [7,5,6,5]).
- Pitch por nota (root note + fine tune), volumen, pan (pan law original),
  master volume, loop oneshot/forward, recorte por `end`.
- Comandos: `VOLM` (rampas), `KILL`, `DLAY`, `LEGA`, `TABL`, `STOP`, `HOP`.
- Tablas (1 fila/tick, 3 columnas, HOP con contador).
- Crush/downsample y filtro LP del upstream (ver limitaciones).
- Instrumentos MIDI: note on/off, `MDCC`, `MDPG`, `MVEL` y `VOLM` → CC7,
  emitidos por el puerto MIDI de salida configurado.
- Filtro low-pass con resonancia por canal (biquad propio, sin plugins
  LADSPA: no añade dependencias y sobra para 8 canales en la RPi4).

## Limitaciones conocidas

- **Filtro en modo `scream`**: el original desborda punto fijo int32 a
  propósito; aquí se satura a [-2, 2]. Aproximación pendiente de ajuste
  fino a oído (solo afecta al instrumento "accordion").
- Sin instrumentos de tipo soundfont, sin feedback, slices, oscillator
  ni ping-pong.
- El comando `STOP` detiene la canción; no hay avance automático a la
  siguiente (se pulsa `n` o `espacio`).
