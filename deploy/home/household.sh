#!/usr/bin/env bash
# Who lives here, where each person is right now, and the PIN each one approves with.
#
#   deploy/home/household.sh list                      the people, where each is, whether each has a PIN
#   deploy/home/household.sh add wife --name "Name"    someone who lives here
#   deploy/home/household.sh set-pin wife              they choose their PIN (hidden, asked twice)
#   deploy/home/household.sh where wife living         mark where someone is: away, home, or a room
#
# A command here is the owner's own hand, so it needs no PIN. On a page, marking where someone is
# needs that person's PIN or the owner's. See docs/REAL_HOME.md, "The household".
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CONF="${PORCH_CONFIG_DIR:-$HOME/.config/porch-hub}"
[ -f "$CONF/home.env" ] || { echo "Run deploy/home/setup_mac.sh first." >&2; exit 1; }
set -a
# shellcheck disable=SC1091
. "$CONF/home.env"
set +a
export PORCH_CONFIG_DIR="$CONF"
cd "$REPO"
exec "$REPO/.venv/bin/python" -m porch_hub.household "$@"
