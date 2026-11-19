#!/usr/bin/env bash
# Connect an account to the hub. Each is done once; what it saves stays in ~/.config/porch-hub,
# readable only by you. See docs/REAL_HOME.md for what each account needs first.
#
#   deploy/home/connect.sh google                 sign in to Google Home (a browser opens), then list the home
#   deploy/home/connect.sh google --check-setup   only check that the files and tools are in place
#   deploy/home/connect.sh ring                   enter the Ring Partner app's link at hidden prompts
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CONF="${PORCH_CONFIG_DIR:-$HOME/.config/porch-hub}"
[ -f "$CONF/home.env" ] || { echo "Run deploy/home/setup_mac.sh first." >&2; exit 1; }

set -a
# shellcheck disable=SC1091
. "$CONF/home.env"
set +a
export PORCH_CONFIG_DIR="$CONF"

case "${1:-}" in
  google)
    shift
    export GOOGLE_HOME_DIRECTORY="${GOOGLE_HOME_DIRECTORY:-$CONF/google-home}"
    mkdir -p "$GOOGLE_HOME_DIRECTORY"
    chmod 700 "$GOOGLE_HOME_DIRECTORY"
    exec "$REPO/.venv/bin/python" -m porch_hub.drivers.google_home.login "$@"
    ;;
  ring)
    export RING_AUTHORIZATION_FILE="${RING_AUTHORIZATION_FILE:-$CONF/ring-authorization.json}"
    exec "$REPO/.venv/bin/python" -m porch_hub.drivers.ring configure
    ;;
  *)
    echo "Usage: deploy/home/connect.sh google [--check-setup] | ring" >&2
    exit 2
    ;;
esac
