#!/usr/bin/env bash
# The owner's side of Porch for this home: open a control window for real devices, answer
# requests agents are waiting on, and say whether someone is home.
#
# Family protection (works without the hub running; see docs/REAL_HOME.md):
#   deploy/home/owner.sh allow --device <id> [--device <id>] [--minutes 5] [--command On --command Off]
#                                          let the named devices take commands for a few minutes
#   deploy/home/owner.sh protect           close the control window now
#   deploy/home/owner.sh protection        say whether a window is open
#   deploy/home/owner.sh set-pin           choose the owner PIN the pages' button asks for
#   deploy/home/owner.sh always --device <id> [--device <id>]
#                                          let the named devices be switched at any time, with no window
#   deploy/home/owner.sh always --none     take that back
#
# The gateway (deploy/home/start.sh must be running):
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
export PORCH_CONFIG_DIR="$CONF"
export PORCH_OWNER_COMMAND="$REPO/deploy/home/owner.sh"
export PORCH_GATEWAY_CONFIG="${PORCH_GATEWAY_CONFIG:-$CONF/porch_config.json}"

case "${1:-}" in
  allow|protect|set-pin|always)
    exec "$REPO/.venv/bin/python" -m porch_hub.protection "$@"
    ;;
  protection)
    exec "$REPO/.venv/bin/python" -m porch_hub.protection status
    ;;
esac
exec "$REPO/porch_gateway/.venv/bin/python" "$REPO/porch_gateway/owner.py" "$@"
