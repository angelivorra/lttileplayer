# lttileplayer (ROBOTRACA)

Reproductor standalone en Python de canciones de [LittleGPTracker](https://github.com/djdiskmachine/LittleGPTracker)
(LGPT), pensado para correr headless/kiosk en una **Raspberry Pi 4 con HAT de
audio**, controlado por un controlador MIDI con **8 pads** (sampler) y
**8 knobs** (modulación en directo por canal). Estética Pip-Boy (consola
verde fósforo, curses).

El `README.md` es la documentación de referencia (deps, config, uso,
deploy, qué está soportado/limitado). Este archivo es para retomar trabajo
rápido con Claude Code: arquitectura interna, convenciones y estado actual.

## Arquitectura

```
lgpt_parser.py   XML/LZ77 -> LGPTProject (song/chains/phrases/tables/grooves/instrumentos)
lgpt_engine.py   Engine.render(frames) -> bloque float32 estéreo. Puro numpy,
                 sin dependencia de tarjeta de audio (testable headless).
ladspa_fx.py     Wrappers ctypes sobre plugins LADSPA del sistema (opcional:
                 cada efecto tiene fallback en numpy puro si el .so no está).
lgpt_player.py   sounddevice (audio real) + mido (MIDI real) + curses (UI) +
                 lectura de lttileplayer.toml/robotraca.json.
lgpt_setup.py    Asistente de configuración por terminal (captura MIDI).
```

Flujo de datos: `Player` carga un `Engine` por canción; el callback de audio
llama a `engine.render(frames)` en el hilo de audio. Todo evento externo
(MIDI, botones, teclado) entra por `engine.push_event(...)` a una cola y se
aplica dentro de `render()` — **no hay locks en el camino de audio**.

Dentro de `render()`, dos tiempos por canal:
- **t=0**: secuenciador (song→chain→phrase, ticks, grooves, tablas) y pitch
  de la voz, en tiempo real.
- **t+1**: salida de la línea de retardo del canal (`audio_delay`), donde se
  aplican los efectos en directo (volumen/pan/FX de knobs) — así los pots se
  oyen al instante aunque el audio esté retrasado para sincronizar con MIDI
  remoto.

## Pads (sampler)

8 pads disparan WAVs de un banco (`audio.wavs_dir` en el toml, `001.wav` →
pad 1, etc.), independientes de la canción activa, sin delay ni FX de canal.
Se activan por nota MIDI (`buttons.sampleN` en el toml) → `engine.push_event
("trigger", idx)` → `Engine._trigger_pad`. Volumen configurable global
(`audio.pad_volume`) o por pad (`robotraca.json` → `pad_volume` como dict
`{"2": 40}`).

## Knobs (pots) — mapeo en dos capas

1. **Físico** (`[pots]` en `lttileplayer.toml`, global): `potN.cc` = spec
   MIDI CC del knob físico (`cc:canal:control`). No cambia entre canciones.
2. **Target** (`robotraca.json` por canción, carpeta del proyecto LGPT):
   `pots.potN` = `"canal:parametro"` (canal tracker 0-7). Se recalcula en
   `Player._apply_song_config` al cargar cada canción — el mismo knob físico
   puede modular cosas distintas según la canción.

Parámetros válidos: `volume`, `pan`, `pitch`, `lp_cutoff`/`cutoff`, `lp_res`,
o el nombre de cualquier entrada de `EFFECT_PRESETS` (efecto LADSPA en
`lgpt_engine.py`): `suboctave`, `satan`, `ringmod`, `chopper`, `phaser`,
`decimator`, `tape_delay`, `acid_lp`.

En la pantalla CONFIG del propio player (tecla `c` desde la lista) se puede
recapturar el CC físico de cada pot sin tocar el toml a mano.

## Efectos (`lgpt_engine.py` + `ladspa_fx.py`)

Cada preset en `EFFECT_PRESETS` es una clase con `apply(buf, amount)`
(`amount` 0-1 viene del knob). Todas intentan cargar un plugin LADSPA real
del sistema (`ladspa_fx.py`) y si falla (plugin no instalado) caen a una
aproximación en numpy puro — así el código es portable a una Pi sin los
`.so` de LADSPA instalados, a costa de menos fidelidad. Al añadir o ajustar
un efecto, mantener ese patrón (plugin real + fallback) y evitar que el
`amount` dispare el volumen (todos compensan nivel al subir).

## Config por canción: `robotraca.json`

Vive en la carpeta del proyecto LGPT (`lgpt_<nombre>/robotraca.json`), junto
a `lgptsav.dat`. Claves: `mute` (lista de canales 0-7 siempre silenciados),
`pad_volume` (número o dict por pad), `pots` (targets, ver arriba). Ausente
= sin mute y sin efectos.

## Convenciones del repo

- Comentarios y docstrings en **español**, solo cuando explican el *por qué*
  (referencia al comportamiento del upstream C++, un workaround, una regla
  no obvia) — no comentan lo que el código ya dice con nombres claros.
- Sin dependencias nuevas sin justificar; sin archivos temporales en el
  repo. `PROMPT_*.md` está en `.gitignore` (notas de trabajo, no se suben).
- El motor (`lgpt_engine.py`) no debe depender de `sounddevice` — debe poder
  testearse headless y como benchmark (`python lgpt_engine.py <proyecto> <segundos>`).
- Tests en `tests/` (unittest/pytest), corren sobre proyectos reales en
  `/home/angel/LGPT/songs/` (rutas hardcodeadas en los tests, es entorno de
  desarrollo del propio autor, no CI genérica).

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python lgpt_engine.py /ruta/a/lgpt_cancion 60   # benchmark headless
```

## Despliegue

`./deploy.sh [host_ssh]` (por defecto `Lgpt`): sincroniza código+canciones,
crea venv, escribe config para la Pi, deja autologin en tty1 con el player
en kiosk. Ver README.md para detalle completo.

## Estado / en curso

Trabajo reciente (ver `git log`): ajuste de los efectos de los knobs
(suboctava menos agresiva, chopper con onda triangular en vez de corte
duro) y volumen de pads configurable por canción. El knob 2 (`pot2`) está
mapeado por defecto a `5:chopper` (canal 5 = pista 6, el bajo) en
`lttileplayer.toml`; los knobs 3-8 siguen sin target asignado. `pot1` está
en `2:suboctave`.

Hay un cambio sin commitear en `lttileplayer.toml` (intercambio de
`sample1`/`sample2` entre notas 38/43) — revisar antes de seguir para no
perderlo ni confundirlo con trabajo nuevo.
