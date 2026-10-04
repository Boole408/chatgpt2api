#!/bin/bash
set -euo pipefail

children=()
cleanup() {
    trap - EXIT INT TERM
    if [ ${#children[@]} -gt 0 ]; then
        kill "${children[@]}" 2>/dev/null || true
        wait "${children[@]}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp -ac &
children+=("$!")
ready=false
for attempt in $(seq 1 50); do
    if [ -S /tmp/.X11-unix/X99 ]; then ready=true; break; fi
    kill -0 "${children[0]}"
    sleep 0.1
done
test "$ready" = true

openbox &
children+=("$!")
# VNC is confined to loopback inside the shared container network.
# Access uses the loopback Docker port through SSH or authenticated HTTPS.
x11vnc -display :99 -localhost -rfbport 5900 -forever -shared -nopw -noxdamage &
children+=("$!")
websockify --web=/usr/share/novnc/ --heartbeat=30 0.0.0.0:6080 127.0.0.1:5900 &
children+=("$!")

wait -n "${children[@]}"
# A desktop process exited; let restart policy recreate the whole session.
exit 1
