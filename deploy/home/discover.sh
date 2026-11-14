#!/usr/bin/env bash
# List what LG and the Matter Server report, with bindings to paste into home.env. Reads only.
#
#   deploy/home/discover.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CONF="${PORCH_CONFIG_DIR:-$HOME/.config/porch-hub}"
[ -f "$CONF/home.env" ] || { echo "Run deploy/home/setup_mac.sh first." >&2; exit 1; }

set -a
# shellcheck disable=SC1091
. "$CONF/home.env"
set +a
exec "$REPO/.venv/bin/python" -m porch_hub.discover
