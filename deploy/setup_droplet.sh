#!/usr/bin/env bash
# Sets up (or updates) the Porch demo server: Device Hub, Porch gateway, benchmark baseline,
# demo shortcuts, and optionally Hermes. Run as root on Ubuntu 24.04. Safe to run again.
#
#   Normally run for you by deploy/deploy.sh (from your laptop), which copies the code to /opt/porch-app.
#   First time on a fresh server:  SETUP_ARGS=--with-hermes deploy/deploy.sh
#                                  then on the server: set-anthropic-key  and  /opt/porch-app/deploy/hermes_profiles.sh
#
# Everything listens on 127.0.0.1 only. View dashboards through an SSH tunnel (see deploy/README.md).
set -euo pipefail
APP=/opt/porch-app
GW=$APP/porch_gateway
export PATH="$HOME/.local/bin:$PATH"
export DEBIAN_FRONTEND=noninteractive

command -v tmux >/dev/null || { apt-get update -qq && apt-get install -y -qq tmux >/dev/null; }
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1

# Python environments: the hub at the repo root, the gateway in porch_gateway/
cd $APP
[ -d .venv ] || uv venv -q .venv
uv pip install -q --python .venv/bin/python -e ".[lg]"
cd $GW
[ -d .venv ] || uv venv -q .venv
uv pip install -q --python .venv/bin/python -r requirements.txt

# Services
mkdir -p /var/lib/porch-hub
cp $APP/deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable porch-hub porch-gateway porch-direct >/dev/null 2>&1
systemctl restart porch-hub
sleep 3
systemctl restart porch-gateway porch-direct

# Demo shortcuts: porch-demo, energy, concierge, approve, reset-demo, occupied, vacant, leak-on, leak-off, set-anthropic-key
install -m 755 $APP/deploy/bin/* /usr/local/bin/

if [ "${1:-}" = "--with-hermes" ] && ! command -v hermes >/dev/null; then
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --non-interactive --skip-browser --skip-computer-use >/root/hermes-install.log 2>&1
  hermes config set model.provider anthropic >/dev/null
  hermes config set model.default claude-opus-5-5 >/dev/null
  echo "Hermes installed. Next: set-anthropic-key, then $APP/deploy/hermes_profiles.sh"
fi

sleep 3
systemctl is-active porch-hub porch-gateway porch-direct
