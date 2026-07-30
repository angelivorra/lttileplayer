# Catálogo de efectos LADSPA disponibles en la Pi

Inventario de `/usr/lib/ladspa` en la Raspberry Pi (`ladspa-sdk` + `swh-plugins`
+ `caps.so`), para elegir nuevos efectos de knob por canal/canción sin tener
que volver a inspeccionar el equipo cada vez. Consultado con `listplugins` /
`analyseplugin` (paquete `ladspa-sdk`, ya instalado en la Pi).

Cada canal solo tiene **un `amount` 0-1** por efecto (el valor del knob), así
que lo que importa aquí es qué plugins se controlan bien con un solo knob
(o con 2-3 parámetros que se puedan derivar de `amount` con una fórmula fija,
como ya hace `lgpt_engine.py`/`ladspa_fx.py`).

## 1. Ya wireados en `EFFECT_PRESETS` (`lgpt_engine.py`)

| preset (target) | plugin LADSPA | .so | notas |
|---|---|---|---|
| `suboctave` | Audio Divider | `divider_1186.so` (1186) | denominador 1→2, "una marcha" |
| `satan` | Barry's Satan Maximiser | `satan_maximiser_1408.so` (1408) | knee 0→-60dB + compensación |
| `ringmod` | Ringmod with LFO | `ringmod_1188.so` (1189) | depth+rate, zona metálica 30-330Hz |
| `chopper` | Ringmod with LFO (onda triangular) | `ringmod_1188.so` (1189) | mismo plugin que `ringmod` pero con `set_control` fijando triangle=1; rate 2-16Hz. **Es el que no convenció en knob2** |
| `phaser` | LFO Phaser | `phasers_1217.so` (1217) | rate 0.2-1.7Hz + feedback |
| `decimator` | Decimator | `decimator_1202.so` (1202) | bits 24→16, rate 100%→20% |
| `tape_delay` | Tape Delay Simulation | `tape_delay_1211.so` (1211) | tap -6dB, dry -2dB |
| `acid_lp` | C* AutoFilter (o SVF si no hay CAPS) | `caps.so` (2593) / `svf_1214.so` (1214) | barrido 3800→160Hz + resonancia |

También hay clases ya escritas en `ladspa_fx.py` pero **sin preset asignado**
en `EFFECT_PRESETS` (listas para usar, solo falta darles nombre y curva de
`amount`): `LadspaOverdrive`/`LadspaStereoOverdrive` (`foverdrive_1196.so`,
1196) y `LadspaFoldover`/`LadspaStereoFoldover` (`foldover_1213.so`, 1213).

## 2. Candidatos para reemplazar `chopper` en knob2 (mismo canal, otra textura)

Chopper es un tremolo/ring-mod con LFO — a `amount` alto se oye como
distorsión metálica porque el LFO entra en zona de audio (hasta 16Hz está
bien, pero `Ringmod with LFO` tiene depth acoplado al mismo control que sube
la profundidad de la modulación en anillo, no solo el tremolo). Alternativas
más "musicales" con un solo knob:

| Plugin | .so (ID) | Parámetros relevantes | Carácter |
|---|---|---|---|
| **Retro Flanger** | `retro_flange_1208.so` (1208) | `Average stall (ms)` 0-10, `Flange frequency (Hz)` 0.5-8 (fijo o barrido con `amount`) | Flanger simple, un knob, sonido suave/vintage, nada agresivo |
| **DJ flanger** | `dj_flanger_1438.so` (1438) | `LFO period (s)` 0.1-32, `LFO depth (ms)` 1-5, `Feedback %` -100..100 | Flanger clásico de DJ, más marcado que el Retro |
| **Multivoice Chorus** | `multivoice_chorus_1201.so` (1201) | `Nº voces` 1-8, `Detune %` 0-5, `LFO freq` 2-30Hz | Engorda el sonido sin "romperlo"; bueno para bajo/pad |
| **GVerb** (reverb) | `gverb_1216.so` (1216) | `Roomsize` 1-300, `Reverb time` 0.1-30s, `Damping` 0-1 | Efecto "espacio", muy distinto a todo lo que hay ahora |
| **C\* AutoFilter** (ya está de fallback en `acid_lp`) | `caps.so` (2593) | `f (Hz)` 20-3800 log, `rate`, `depth`, `shape` (0=sin,1=tri) | Wah/auto-filtro automodulado; ya lo tenemos wireado como clase, solo falta exponerlo como preset propio en vez de solo fallback |
| **Valve saturation** | `valve_1209.so` (1209) | `Distortion level` 0-1, `Distortion character` 0-1 | Saturación cálida (no es "distorsión dura" como satan) |
| **VyNil (vinyl)** | `vynil_1905.so` (1905) | `Crackle` 0-1, `Wear` 0-1, `Surface warping` 0-1, `RPM` 33-78 | Textura lo-fi/vinilo, encaja con la estética Pip-Boy |
| **Giant flange** | `giant_flange_1437.so` (1437) | 2 LFOs + `Feedback` + `Dry/Wet` | Flanger extremo/espacial, dos velocidades combinadas |

Recomendación rápida para probar ya: **Retro Flanger** (sonido limpio, un
solo control, cero riesgo de "distorsión no deseada") o **C\* AutoFilter**
como preset independiente (ya tenemos el wrapper hecho, es prácticamente
gratis añadirlo).

## 3. Resto del catálogo instalado (`/usr/lib/ladspa`, 102 archivos .so)

Agrupado por función. "Insertable 1 knob" = candidato razonable para un
target de knob tal y como funciona hoy (un único `amount` 0-1); el resto
necesita más de un control con sentido o no es un inserto de audio normal
(generadores, utilidades de mezcla, EQs de muchas bandas).

### Distorsión / saturación / bitcrush
`foverdrive_1196` (Fast overdrive, ya en `ladspa_fx.py` sin preset),
`foldover_1213` (íd.), `chebstortion_1430` (Distortion 0-3), `valve_1209`
(ver arriba), `crossover_dist_1404`, `pointer_cast_1910`, `alias_1407`
(Aliasing level 0-1, crush digital simple), `sinus_wavewrapper_1198`,
`shaper_1187` (Waveshape -10..10), `diode_1185`, `declip_1195`,
`caps.so::Saturate` (2603, `mode` 0-11 + `gain`).

### Modulación (flanger / chorus / phaser / ring mod)
`flanger_1191`, `retro_flange_1208`, `dj_flanger_1438`, `giant_flange_1437`,
`multivoice_chorus_1201`, `caps.so::ChorusI` (1767), `caps.so::PhaserII`
(2586, alternativa a `phasers_1217`), `phasers_1217` (ya en uso),
`ringmod_1188` (ya en uso x2), `am_pitchshift_1433` (Pitch shift 0.25-4x),
`bode_shifter_1431` (Frequency shift 0-5000Hz, dos salidas up/down).

### Filtros
`svf_1214` (ya en uso), `caps.so::AutoFilter` (ya en uso),
`lowpass_iir_1891`/`highpass_iir_1890`/`bandpass_iir_1892`/
`bandpass_a_iir_1893`/`notch_iir_1894` (Glame IIR, 1 sola frecuencia de
corte, sencillos), `hermes_filter_1200` (muy completo: 2 LFOs + 2 osc + 3
filtros + 3 delays, demasiados parámetros para un solo knob salvo que se
fije casi todo y solo module 1-2 cosas).

### Delay / reverb / espacial
`tape_delay_1211` (ya en uso), `delayorama_1402` (multi-tap, hasta 128
taps), `gverb_1216` (reverb, ver arriba), `caps.so::Plate`/`PlateX2`
(1779/1795, reverb de placa), `caps.so::Scape` (2588, delay estéreo con
resonancias cromáticas), `revdelay_1605` (Reverse Delay), `lcr_delay_1436`,
`fad_delay_1192`, `mod_delay_1419`, `caps.so::Wider`/`Narrower` (imagen
estéreo, no añaden textura, solo anchura).

### Dinámica (compresores / gates / limitadores)
`sc1_1425`..`sc4_1882`/`sc4m_1916` (compresores Steve Harris, varios modos),
`caps.so::Compress`/`CompressX2` (1772/2598), `gate_1410`,
`caps.so::Noisegate` (2602), `fast_lookahead_limiter_1913`,
`hard_limiter_1413`, `dyson_compress_1403`. Útiles para "domar" un canal,
no tanto como efecto creativo de knob.

### EQ / tono
`caps.so::Eq10`/`Eq10X2`/`Eq4p`/`EqFA4p`/`ToneStack`, `mbeq_1197`
(15 bandas), `dj_eq_1901`, `single_para_1203`/`triple_para_1204`,
`butterworth_1902` (x-over/LP/HP). Muchos controles: mejor para EQ fija
de mezcla que para modular con un knob en directo.

### Amp/cabinet sim (guitarra)
`caps.so::AmpVTS`/`CabinetIII`/`CabinetIV` — pensados para guitarra
eléctrica con ecualización de altavoz; podrían dar un carácter interesante
a un bajo o sample percusivo, pero tienen muchos parámetros (mejor fijar
un "modelo" y modular solo `gain`).

### Generadores / osciladores (NO son inserto de audio — tienen entrada de
control pero no de audio, o generan su propia señal)
`caps.so::Sin`/`White`/`Fractal`/`Click`/`CEO`, `sine.so`, `noise.so`,
`analogue_osc_1416`, `fm_osc_1415`, `wave_terrain_1412`, `sin_cos_1881`,
`const_1909`, `gong_1424`/`gong_beater_1439` (modelo físico de gong, es un
instrumento en sí mismo). No aplican como efecto de canal tal y como está
montado el motor (que hace `plugin.run(buf)` sobre el audio del canal).

### Utilidad / conversión (no son efectos creativos)
`matrix_ms_st_1421`, `matrix_st_ms_1420`, `matrix_spatialiser_1422`,
`surround_encoder_1401`, `split_1406`, `comb_splitter_1411`,
`step_muxer_1212`, `xfade_1915`, `zm1_1428`, `dc_remove_1207`,
`freq_tracker_1418`, `latency_1914`, `pitch_scale_1193`/`1194` (pitch
shifter de calidad, 2 versiones — candidato si algún día se quiere un
"pitch" más fino que el `cc_pitch` actual), `rate_shifter_1417`,
`karaoke_1409` (necesita estéreo con voz centrada, no aplica a samples
mono/con la voz mezclada), `harmonic_gen_1220` (excitador armónico, 10
controles de magnitud — fijar una curva y modular el conjunto con
`amount` sería posible pero laborioso), `vocoder_1337` (necesita 2
entradas: portadora + moduladora, no encaja con el modelo actual de 1
`amount` por canal), `gsm_1215` (simulador de codec GSM, para textura muy
concreta de "llamada telefónica" si algún día interesa).

## 4. Cómo se añade un preset nuevo (recordatorio rápido)

1. Mirar los puertos con `analyseplugin` (o esta tabla) y decidir qué
   parámetro(s) mapear a `amount` 0-1.
2. Añadir el wrapper en `ladspa_fx.py` (ctypes, como los que ya hay) si no
   existe.
2. Añadir una clase en `lgpt_engine.py` con `apply(buf, amount)`, con
   compensación de nivel para que `amount` no dispare el volumen (patrón
   de todos los presets actuales).
3. Registrarla en `EFFECT_PRESETS` con el nombre que se usará como
   `target` en `robotraca.json` (`"canal:nombre"`).
4. Mini-deploy (`./deploy-quick.sh`) y probar con el knob físico.
