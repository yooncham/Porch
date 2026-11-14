#!/usr/bin/env bash
# One-time setup for running Porch on a real home from a Mac. Safe to run again.
#
#   deploy/home/setup_mac.sh
#
# Checks Node.js and uv, creates the hub's and the gateway's Python environments, and creates
# ~/.config/porch-hub with your env file (home.env) and the gateway's config for this home.
# It never talks to a device and never overwrites a file you already have.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CONF="${PORCH_CONFIG_DIR:-$HOME/.config/porch-hub}"

say() { printf '%s\n' "$*"; }
stop() { printf 'Stopped: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || say "Note: written for macOS. On Linux, see docs/REAL_HOME.md."

command -v node >/dev/null || stop "Node.js is missing. Install it with: brew install node"
NODE_VERSION="$(node -p 'process.versions.node')"
# The Matter Server needs Node.js 22.13 or newer.
node -e 'const [a, b] = process.versions.node.split(".").map(Number); process.exit(a > 22 || (a === 22 && b >= 13) ? 0 : 1)' \
  || stop "Node.js $NODE_VERSION is too old; the Matter Server needs 22.13 or newer: brew upgrade node"
command -v uv >/dev/null || stop "uv is missing. Install it with: brew install uv"
say "Found Node.js $NODE_VERSION and uv."

cd "$REPO"
[ -x .venv/bin/python ] || uv venv -q --python 3.12 .venv
uv pip install -q --python .venv/bin/python -e ".[lg]"
say "Hub environment ready."
[ -x porch_gateway/.venv/bin/python ] || (cd porch_gateway && uv venv -q --python 3.12 .venv)
uv pip install -q --python porch_gateway/.venv/bin/python -r porch_gateway/requirements.txt
say "Gateway environment ready."

mkdir -p "$CONF/matter"
chmod 700 "$CONF"
if [ -f "$CONF/home.env" ]; then
  say "Kept your $CONF/home.env."
else
  cp deploy/home.env.example "$CONF/home.env"
  chmod 600 "$CONF/home.env"
  say "Created $CONF/home.env."
fi
if [ -f "$CONF/porch_config.json" ]; then
  say "Kept your $CONF/porch_config.json."
else
  # The demo's agents and keys, marked as physical hardware, with no plug mapped yet: until a
  # plug is mapped to what it powers, no agent may control it.
  .venv/bin/python - "porch_gateway/porch_config.json" "$CONF/porch_config.json" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
config["hardware"] = "physical"
config["device_capabilities"] = {}
with open(sys.argv[2], "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
PY
  chmod 600 "$CONF/porch_config.json"
  say "Created $CONF/porch_config.json for the gateway."
fi

say ""
say "Next:"
say "  1. Put your LG token in $CONF/home.env (the line LG_THINQ_PAT=)."
say "  2. In a new Terminal tab: deploy/home/matter_server.sh"
say "  3. Pair the Tapo devices, then: deploy/home/discover.sh"
