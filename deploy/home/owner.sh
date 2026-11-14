#!/usr/bin/env bash
# The owner's side of Porch for this home: answer requests agents are waiting on, and say
# whether someone is home. deploy/home/start.sh must be running.
#
#   deploy/home/owner.sh list              requests waiting for you
#   deploy/home/owner.sh approve [<id>]    approve one (without an id: the only one waiting)
#   deploy/home/owner.sh decline <id>
#   deploy/home/owner.sh home | away       whether someone is home (heating rule)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CONF="${PORCH_CONFIG_DIR:-$HOME/.config/porch-hub}"
[ -f "$CONF/home.env" ] || { echo "Run deploy/home/setup_mac.sh first." >&2; exit 1; }

set -a
# shellcheck disable=SC1091
. "$CONF/home.env"
set +a
export PORCH_GATEWAY_CONFIG="${PORCH_GATEWAY_CONFIG:-$CONF/porch_config.json}"
exec "$REPO/porch_gateway/.venv/bin/python" "$REPO/porch_gateway/owner.py" "$@"
