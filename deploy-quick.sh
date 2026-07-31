#!/usr/bin/env bash
# Mini-deploy rápido para iterar probando knobs/canciones/efectos.
#
# Uso: ./deploy-quick.sh [host]        (host SSH por defecto: Lgpt)
#
# A diferencia de deploy.sh (venv, systemd, autologin...), esto asume que
# el player YA está instalado y corriendo en la Pi en modo kiosk (tty1).
# Solo:
#   1. Sincroniza el código y lttileplayer.toml.
#   2. Sincroniza los robotraca.json de las canciones de prueba (si existen
#      en local, sin volver a copiar los WAV de la canción).
#   3. Mata el lgpt_player en marcha: agetty/.bash_profile lo relanza solo
#      (kiosk), recogiendo el código/config nuevos.

set -euo pipefail

PI="${1:-Lgpt}"
APP=/home/angel/lttileplayer
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SONGS_DST="$APP/songs"
SONGS_SRC="${SONGS_SRC:-$HERE/songs}"

echo "==> Código -> $PI:$APP"
rsync -a \
    --include='lgpt_*.py' --include='ladspa_fx.py' \
    --include='lttileplayer.toml' --exclude='*' \
    "$HERE/" "$PI:$APP/"

echo "==> robotraca.json de canciones de prueba -> $SONGS_DST"
for f in "$SONGS_SRC"/lgpt_*/robotraca.json; do
    [ -f "$f" ] || continue
    song="$(basename "$(dirname "$f")")"
    ssh "$PI" "mkdir -p '$SONGS_DST/$song'"
    rsync -a "$f" "$PI:$SONGS_DST/$song/robotraca.json"
    echo "  $song"
done

echo "==> Reiniciando lgpt_player en $PI"
ssh "$PI" "pkill -f lgpt_player.py" || true
sleep 1
ssh "$PI" "pgrep -af lgpt_player" || echo "  (aún relanzando; agetty lo hace en cuanto la pantalla se refresque)"
echo "Mini-deploy completado."
