#!/usr/bin/env bash
# Run the Device Hub (simulated devices) and the Porch gateway locally. Ctrl+C stops both.
#   Hub:     http://127.0.0.1:8100  (dashboards: /dashboard, /stage, /digital-twin, /home-3d)
#   Porch:   http://127.0.0.1:8000/mcp  (stage feed ws://127.0.0.1:8000/ws/stage)
# Then, in another terminal:  porch_gateway/.venv/bin/python porch_gateway/check_demo.py
set -e
GW="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$GW")"
command -v uv >/dev/null || { echo "Install uv first: https://docs.astral.sh/uv/"; exit 1; }
[ -d "$REPO/.venv" ] || (cd "$REPO" && uv venv -q .venv && uv pip install -q --python .venv/bin/python -e ".[lg]")
[ -d "$GW/.venv" ] || (cd "$GW" && uv venv -q .venv && uv pip install -q --python .venv/bin/python -r requirements.txt)
(cd "$REPO" && .venv/bin/uvicorn porch_hub.main:app --host 127.0.0.1 --port 8100) &
HUB=$!
trap 'kill $HUB 2>/dev/null' EXIT
for i in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:8100/devices && break; sleep 0.5; done
cd "$GW" && .venv/bin/python porch_gateway.py
