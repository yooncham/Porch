#!/usr/bin/env bash
# Your own agents in Porch: give one a key and rights, and connect a Claude on this Mac as it.
#
#   deploy/home/agent.sh add dot --name Dot     a new agent with its own key (never printed)
#   deploy/home/agent.sh list                   the agents and what each may ask for
#   deploy/home/agent.sh connect dot            an MCP server on stdio that speaks to Porch as Dot
#   deploy/home/agent.sh desktop                a Claude Desktop connection for each agent that lives there
#   deploy/home/agent.sh remove dot             take its key away
#
# After add or remove, restart Porch (deploy/home/start.sh). See docs/REAL_HOME.md, "Your own agents".
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
export PORCH_CONFIG_DIR="${PORCH_CONFIG_DIR:-$HOME/.config/porch-hub}"
exec "$REPO/porch_gateway/.venv/bin/python" "$REPO/deploy/home/agents.py" "$@"
