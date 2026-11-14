#!/usr/bin/env bash
# Check this home end to end through the gateway. deploy/home/start.sh must be running.
#
#   deploy/home/check.sh                       reads only: devices, what each agent may do
#   deploy/home/check.sh home-1/entry-light    also switches that one device and puts it back
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CONF="${PORCH_CONFIG_DIR:-$HOME/.config/porch-hub}"
[ -f "$CONF/home.env" ] || { echo "Run deploy/home/setup_mac.sh first." >&2; exit 1; }

set -a
# shellcheck disable=SC1091
. "$CONF/home.env"
set +a
export PORCH_GATEWAY_CONFIG="${PORCH_GATEWAY_CONFIG:-$CONF/porch_config.json}"
exec "$REPO/porch_gateway/.venv/bin/python" "$REPO/deploy/home/check_home.py" "$@"
