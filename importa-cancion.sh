#!/usr/bin/env bash
# Trae una canción desde la carpeta de trabajo de LGPT (la sincronizada con
# MEGA) a songs/ del repo.
#
# Uso: ./importa-cancion.sh <lgpt_nombre> [carpeta_origen]
#      ./importa-cancion.sh --todas [carpeta_origen]
#
# Existe por un motivo concreto: `robotraca.json` es config de ESTE player
# (mutes, knobs, master, volumen de pads) y LGPT no sabe nada de él, así que
# vive solo en el repo. Una copia a lo bruto desde la carpeta de LGPT lo
# borraría o lo dejaría desactualizado y se perdería el trabajo de ajuste.
# Este script copia el lgptsav.dat y los samples, y NO TOCA el robotraca.json.
#
# Tampoco trae los bounces (mixdown.wav y el .wav suelto de la raíz): el
# player no los usa y pesan.

set -euo pipefail

ORIGEN_DEF=/home/angel/LGPT/songs
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESTINO="$HERE/songs"

uso() {
    echo "Uso: $0 <lgpt_nombre> [carpeta_origen]"
    echo "     $0 --todas [carpeta_origen]      (actualiza las que ya están)"
    exit 1
}

[ $# -ge 1 ] || uso

importa() {
    local nombre="$1" origen="$2"
    local src="$origen/$nombre" dst="$DESTINO/$nombre"

    if [ ! -f "$src/lgptsav.dat" ]; then
        echo "  $nombre: no está en $origen  (saltada)"
        return 1
    fi
    mkdir -p "$dst"

    # el JSON es nuestro: se queda como está
    local tenia_json="no"
    [ -f "$dst/robotraca.json" ] && tenia_json="sí"

    rsync -a --delete \
        --exclude='robotraca.json' \
        --exclude='mixdown.wav' \
        --exclude='*.bak-*' \
        --exclude='/*.wav' \
        "$src/" "$dst/"

    echo "  $nombre: canción y samples actualizados"
    if [ "$tenia_json" = "sí" ]; then
        echo "     robotraca.json conservado (config del player)"
    else
        echo "     AVISO: no hay robotraca.json; sin él la canción suena"
        echo "            sin mutes ni knobs asignados"
    fi
}

if [ "$1" = "--todas" ]; then
    origen="${2:-$ORIGEN_DEF}"
    echo "==> Actualizando desde $origen las canciones ya presentes en songs/"
    for d in "$DESTINO"/lgpt_*; do
        [ -d "$d" ] || continue
        importa "$(basename "$d")" "$origen" || true
    done
else
    importa "$1" "${2:-$ORIGEN_DEF}"
fi

echo
echo "Repasa con 'git status' antes de commitear."
