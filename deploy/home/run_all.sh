#!/usr/bin/env bash
# Start everything this home needs in one Terminal window: Porch's Matter Server, then the hub
# and the gateway. Ctrl+C stops all of it.
#
#   deploy/home/run_all.sh
#
# Run it in the Terminal app, not inside another app's terminal. The Matter Server has to reach
# the devices on your Wi-Fi, and macOS allows that per app: the first time, macOS asks whether
# Terminal may find devices on your local network (System Settings > Privacy & Security > Local
# Network). Programs started inside another app also stop when that app quits.
#
# The Matter Server's own output goes to ~/.config/porch-hub/logs/matter-server.log.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="${PORCH_CONFIG_DIR:-$HOME/.config/porch-hub}"
[ -f "$CONF/home.env" ] || { echo "Run deploy/home/setup_mac.sh first." >&2; exit 1; }

for port in 5580 8100 8000 8090; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Something is already listening on port $port: another Porch is running." >&2
    echo "Stop it first (Ctrl+C in its window), then run this again." >&2
    exit 1
  fi
done

MATTER=""
stop_matter() {
  if [ -n "$MATTER" ] && kill -0 "$MATTER" 2>/dev/null; then
    kill "$MATTER" 2>/dev/null || true
    wait "$MATTER" 2>/dev/null || true
  fi
}
trap stop_matter EXIT

# The Matter Server only when this home uses one.
if grep -q '^MATTER_SERVER_URL=' "$CONF/home.env"; then
  mkdir -p "$CONF/logs"
  chmod 700 "$CONF/logs"
  LOG="$CONF/logs/matter-server.log"
  echo "Starting the Matter Server (its output: $LOG)"
  "$HERE/matter_server.sh" > "$LOG" 2>&1 &
  MATTER=$!
  for _ in $(seq 1 120); do
    lsof -nP -iTCP:5580 -sTCP:LISTEN >/dev/null 2>&1 && break
    kill -0 "$MATTER" 2>/dev/null || { echo "The Matter Server stopped. See $LOG" >&2; exit 1; }
    sleep 0.5
  done
  lsof -nP -iTCP:5580 -sTCP:LISTEN >/dev/null 2>&1 || { echo "The Matter Server did not start. See $LOG" >&2; exit 1; }
  # Its first lines tell whether macOS lets it onto the local network.
  sleep 3
  if grep -q "EHOSTUNREACH" "$LOG"; then
    echo
    echo "macOS is keeping this terminal off your local network, so the Matter devices cannot be reached."
    echo "Allow it in System Settings > Privacy & Security > Local Network, then start this again."
    echo "(The hub starts anyway: devices reached through LG, Google Home and Ring still work.)"
    echo
  fi
fi

# The online door: local only, and it refuses everything until online access is on
# (deploy/home/online.sh on). Its lines show in this window.
DOOR=""
if [ -x "$HERE/online.sh" ]; then
  "$HERE/online.sh" serve &
  DOOR=$!
fi
stop_all() {
  if [ -n "$DOOR" ] && kill -0 "$DOOR" 2>/dev/null; then kill "$DOOR" 2>/dev/null || true; fi
  stop_matter
}
trap stop_all EXIT

# The hub and the gateway, in front. The Mac stays awake while they run.
if command -v caffeinate >/dev/null; then
  caffeinate -i "$HERE/start.sh"
else
  "$HERE/start.sh"
fi
