#!/usr/bin/env bash
# Despliegue completo de lttileplayer en la Raspberry Pi.
#
# Uso: ./deploy.sh [host]        (host SSH por defecto: Lgpt)
#
# Idempotente. Hace:
#   1. Sincroniza el código a /home/angel/lttileplayer (incluye
#      lttileplayer.toml: la config real y actual vive en el repo)
#   2. Copia las canciones (repo songs/) a /home/angel/lttileplayer/songs
#   3. Crea el venv en la Pi e instala dependencias (pip)
#   4. Instala lttileplayer.service (systemd) y lo arranca
#   5. Desactiva el antiguo lgpt.service y limpia rc.local
#   6. Verifica: servicio activo, puerto MIDI conectado a movida:in,
#      y render de prueba del engine en la Pi
#
# lttileplayer.toml se edita en el repo (o con lgpt_setup.py / la
# pantalla CONFIG del player en la propia Pi) — deploy.sh ya no lo
# sobrescribe con una plantilla aparte.

set -euo pipefail

PI="${1:-Lgpt}"
APP=/home/angel/lttileplayer
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Las canciones (con su robotraca.json) viven en el repo, en songs/, y se
# despliegan dentro de la app ($APP/songs). songs_dir del toml es relativo
# ("songs"), así el mismo config vale en el PC y en la Pi.
SONGS_DST="$APP/songs"
SONGS_SRC="${SONGS_SRC:-$HERE/songs}"
SONGS=(lgpt_abduccion lgpt_Bulebule lgpt_Energia lgpt_Sartenazo.VERSION1)

echo "==> 1/6 Código -> $PI:$APP"
ssh "$PI" "mkdir -p '$APP' '$SONGS_DST'"
rsync -a --delete \
    --include='lgpt_*.py' --include='ladspa_fx.py' \
    --include='lttileplayer.toml' --include='README.md' \
    --include='tests/' --include='tests/**' --exclude='*' \
    "$HERE/" "$PI:$APP/"

echo "==> 2/6 Canciones -> $SONGS_DST"
for s in "${SONGS[@]}"; do
    rsync -a --exclude='mixdown.wav' "$SONGS_SRC/$s" "$PI:$SONGS_DST/"
done

echo "==> 3/6 Entorno virtual en la Pi"
ssh "$PI" "
    if [ -x '$APP/.venv/bin/python' ] && \
       '$APP/.venv/bin/python' -c 'import numpy, sounddevice, soundfile, mido' 2>/dev/null; then
        echo '  ya instalado, nada que hacer (Pi sin acceso a internet)'
    else
        sudo apt-get install -y -qq python3-venv python3-pip libasound2-dev >/dev/null
        [ -d '$APP/.venv' ] || python3 -m venv '$APP/.venv'
        '$APP/.venv/bin/pip' install -q --upgrade numpy sounddevice soundfile mido python-rtmidi
    fi
"

echo "==> 4/6 Arranque en pantalla (autologin tty1)"
ssh "$PI" "cat > '$APP/midi-connect.sh' && chmod +x '$APP/midi-connect.sh'" <<'EOF'
#!/bin/sh
# Conecta la salida MIDI de lttileplayer al bridge TCP-MIDI (movida:in)
# si existe; si no, no hace nada.
for i in $(seq 1 40); do
    SRC=$(aconnect -l | awk '/^client [0-9]+/{c=$2; gsub(":","",c)} /lttileplayer/{print c; exit}')
    [ -n "$SRC" ] && break
    sleep 0.5
done
if [ -n "$SRC" ]; then
    aconnect -l | grep -q "movida" && aconnect "$SRC:0" "movida:in" 2>/dev/null
fi
exit 0
EOF
ssh "$PI" "cat > '$APP/run-on-tty1.sh' && chmod +x '$APP/run-on-tty1.sh'" <<EOF
#!/bin/sh
$APP/midi-connect.sh &
exec $APP/.venv/bin/python $APP/lgpt_player.py
EOF
# autologin de angel en tty1 + hook en el profile (solo tty1, no SSH)
ssh "$PI" "
    sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
    sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin angel --noclear %I \\\$TERM
EOF
    grep -q run-on-tty1 /home/angel/.bash_profile 2>/dev/null || cat >> /home/angel/.bash_profile <<EOF

if [ \"\\\$(tty)\" = \"/dev/tty1\" ]; then
    exec $APP/run-on-tty1.sh
fi
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now getty@tty1.service
"

echo "==> 5/6 Sustituir el arranque antiguo"
ssh "$PI" "
    sudo systemctl disable --now lgpt.service lttileplayer.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/lgpt.service /etc/systemd/system/lttileplayer.service
    sudo sed -i 's|^\([^#]*run-lgpt.py.*\)\$|#\1|' /etc/rc.local || true
    sudo systemctl daemon-reload
"

echo "==> 6/6 Verificación"
sleep 6
ssh "$PI" "
    pgrep -af lgpt_player
    '$APP/.venv/bin/python' '$APP/lgpt_engine.py' '$SONGS_DST/lgpt_abduccion' 5
"
echo "Despliegue completado."
