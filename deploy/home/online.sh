#!/usr/bin/env bash
# Let your own agents reach Porch from ChatGPT, claude.ai and the Claude phone app, through Tailscale.
#
#   deploy/home/online.sh on        open the door to the internet and print its address
#   deploy/home/online.sh off       close it
#   deploy/home/online.sh status    whether it is open, and which agents have an online key
#   deploy/home/online.sh serve     run the door (deploy/home/run_all.sh starts it with Porch)
#
# See docs/REAL_HOME.md, "Your agents in ChatGPT and on claude.ai".
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
export PORCH_CONFIG_DIR="${PORCH_CONFIG_DIR:-$HOME/.config/porch-hub}"
exec "$REPO/porch_gateway/.venv/bin/python" "$REPO/deploy/home/online.py" "$@"
