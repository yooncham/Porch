#!/usr/bin/env bash
# Creates the six Hermes profiles used by the demo and the HW7 benchmark, as copies of the
# default profile (which must already have its model and API key set: run set-anthropic-key).
#
#   C. Porch:              energy, concierge             → Porch gateway, each with its own agent key
#   A. Direct baseline:    direnergy, dirconcierge       → hub_direct_mcp.py, no rules
#   B. Rules in prompt:    rulesenergy, rulesconcierge   → hub_direct_mcp.py + bench/policy.md in SOUL.md
#
# Every profile can use only its MCP server's tools (no shell or web), with tool search off.
#   deploy/hermes_profiles.sh            create the missing profiles
#   deploy/hermes_profiles.sh --force    recreate all six
#   PREFIX=t deploy/hermes_profiles.sh   create test copies (tenergy, ...) to try it out
set -euo pipefail
HERMES=${HERMES:-/root/.local/bin/hermes}
POLICY="$(cd "$(dirname "$0")/.." && pwd)/porch_gateway/bench/policy.md"
PREFIX=${PREFIX:-}
FORCE=${1:-}

make_profile() {  # name  mcp-name  url  bearer-key-or-empty  with-rules(0/1)
  local name="$PREFIX$1" server=$2 url=$3 key=$4 rules=$5
  if [ -d "${HERMES_ROOT:-$HOME/.hermes}/profiles/$name" ]; then
    [ "$FORCE" = "--force" ] || { echo "skip $name (exists)"; return; }
    "$HERMES" profile delete "$name" --yes >/dev/null
  fi
  "$HERMES" profile create "$name" --clone --no-alias --description "Porch: $name" >/dev/null
  local dir; dir="$(dirname "$("$HERMES" -p "$name" config path)")"
  {
    echo
    echo "# Porch: this agent's only MCP server"
    echo "mcp_servers:"
    echo "  $server:"
    echo "    url: \"$url\""
    if [ -n "$key" ]; then echo "    headers:"; echo "      Authorization: \"Bearer $key\""; fi
  } >> "$dir/config.yaml"
  sed -i "s/^  cli: \[hermes-cli\]$/  cli: [$server]/" "$dir/config.yaml"
  "$HERMES" -p "$name" config set tools.tool_search.enabled off >/dev/null
  if [ "$rules" = 1 ]; then
    { echo; echo "## Household rules you must follow"; echo; sed '1,/^$/d; /^This is the ground truth/d' "$POLICY"; } >> "$dir/SOUL.md"
  fi
  echo "created $name → $server ($url)"
}

make_profile energy         porch http://127.0.0.1:8000/mcp porch_demo_key_energy    0
make_profile concierge      porch http://127.0.0.1:8000/mcp porch_demo_key_concierge 0
make_profile direnergy      home  http://127.0.0.1:8200/mcp ""                       0
make_profile dirconcierge   home  http://127.0.0.1:8200/mcp ""                       0
make_profile rulesenergy    home  http://127.0.0.1:8200/mcp ""                       1
make_profile rulesconcierge home  http://127.0.0.1:8200/mcp ""                       1
