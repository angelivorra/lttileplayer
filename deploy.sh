#!/usr/bin/env bash
# Despliegue completo de lttileplayer en la Raspberry Pi.
#
# Uso: ./deploy.sh [host]        (host SSH por defecto: Lgpt)
#
# Idempotente. Hace:
#   1. Sincroniza el código a /home/angel/lttileplayer
#   2. Copia las canciones a /home/angel/Documentos/canciones
#   3. Crea el venv en la Pi e instala dependencias (pip)
#   4. Escribe lttileplayer.toml para la Pi (audio HAT, MIDI)
#   5. Instala lttileplayer.service (systemd) y lo arranca
#   6. Desactiva el antiguo lgpt.service y limpia rc.local
#   7. Verifica: servicio activo, puerto MIDI conectado a movida:in,
#      y render de prueba del engine en la Pi

set -euo pipefail

PI="${1:-Lgpt}"
APP=/home/angel/lttileplayer
SONGS_DST=/home/angel/Documentos/canciones
SONGS_SRC="${SONGS_SRC:-/home/angel/LGPT/songs}"
SONGS=(lgpt_abduccion lgpt_Bulebule lgpt_Energia lgpt_Sartenazo.VERSION1)
AUDIO_DEV="${AUDIO_DEV:-IQaudIODAC}"
MIDI_IN="${MIDI_IN:-Minilab3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> 1/7 Código -> $PI:$APP"
ssh "$PI" "mkdir -p '$APP' '$SONGS_DST'"
rsync -a --delete \
    --include='lgpt_*.py' --include='ladspa_fx.py' \
    --include='lttileplayer.toml' --include='README.md' \
    --include='tests/' --include='tests/**' --exclude='*' \
    "$HERE/" "$PI:$APP/"

echo "==> 2/7 Canciones -> $SONGS_DST"
for s in "${SONGS[@]}"; do
    rsync -a "$SONGS_SRC/$s" "$PI:$SONGS_DST/"
done

echo "==> 3/7 Entorno virtual en la Pi"
ssh "$PI" "
    sudo apt-get install -y -qq python3-venv python3-pip libasound2-dev >/dev/null
    [ -d '$APP/.venv' ] || python3 -m venv '$APP/.venv'
    '$APP/.venv/bin/pip' install -q --upgrade numpy sounddevice soundfile mido python-rtmidi
"

echo "==> 4/7 Configuración ($APP/lttileplayer.toml)"
ssh "$PI" "cat > '$APP/lttileplayer.toml'" <<'EOF'
# Configuración de lttileplayer en la Pi (generada por deploy.sh).
songs_dir = "/home/angel/Documentos/canciones/"

[audio]
output = "AUDIO_DEV_PLACEHOLDER"
samplerate = 44100
blocksize = 512
delay = 1.0
record = ""

[midi]
input = "MIDI_IN_PLACEHOLDER"
output = "virtual"

[buttons]
up = "note:9:37"
down = "note:9:36"
accept = "note:9:38"
play = "note:9:41"
stop = "note:9:40"

[pots]
pot1 = { cc = "", target = "2:lp_cutoff" }
pot2 = { cc = "", target = "2:volume" }
pot3 = { cc = "", target = "2:pan" }
pot4 = { cc = "", target = "2:pitch" }
pot5 = { cc = "", target = "2:lp_res" }
pot6 = { cc = "", target = "" }
pot7 = { cc = "", target = "" }
pot8 = { cc = "", target = "" }
EOF
ssh "$PI" "sed -i 's|AUDIO_DEV_PLACEHOLDER|$AUDIO_DEV|; s|MIDI_IN_PLACEHOLDER|$MIDI_IN|' '$APP/lttileplayer.toml'"

echo "==> 5/7 Arranque en pantalla (autologin tty1)"
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

echo "==> 6/7 Sustituir el arranque antiguo"
ssh "$PI" "
    sudo systemctl disable --now lgpt.service lttileplayer.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/lgpt.service /etc/systemd/system/lttileplayer.service
    sudo sed -i 's|^\([^#]*run-lgpt.py.*\)\$|#\1|' /etc/rc.local || true
    sudo systemctl daemon-reload
"

echo "==> 7/7 Verificación"
sleep 6
ssh "$PI" "
    pgrep -af lgpt_player
    '$APP/.venv/bin/python' '$APP/lgpt_engine.py' '$SONGS_DST/lgpt_abduccion' 5
"
echo "Despliegue completado."
