#!/usr/bin/env bash
# Start the hub (real devices only) and the gateway with this home's settings. Ctrl+C stops both.
# The Matter Server (deploy/home/matter_server.sh) runs in its own tab.
#
#   deploy/home/start.sh
#
#   Hub dashboard:  http://127.0.0.1:8100/dashboard
#   Gateway (MCP):  http://127.0.0.1:8000/mcp
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CONF="${PORCH_CONFIG_DIR:-$HOME/.config/porch-hub}"
[ -f "$CONF/home.env" ] || { echo "Run deploy/home/setup_mac.sh first." >&2; exit 1; }

set -a
# shellcheck disable=SC1091
. "$CONF/home.env"
set +a
export PORCH_GATEWAY_CONFIG="${PORCH_GATEWAY_CONFIG:-$CONF/porch_config.json}"
exec "$REPO/porch_gateway/start.sh"
