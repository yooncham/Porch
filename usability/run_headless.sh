#!/bin/zsh
# One cold-start run with a fresh, headless Claude: Claude Code in --bare mode, which loads no
# settings, memory or CLAUDE.md, has no tools of its own (--tools ""), and reaches only the stand-in
# Porch, as Fresh agent. The run is recorded in usability/runs/NAME/ and checked at the end.
#
#   usability/run_headless.sh NAME [--wife-in office] [--model sonnet]
#
# Needs your own Anthropic API key, in ~/.config/porch-standin/anthropic-key (one line, chmod 600).
# The key goes to claude only: it is never printed, and never written into the run.
set -euo pipefail
HERE=${0:A:h}
REPO=${HERE:h}
NAME=${1:?usage: usability/run_headless.sh NAME [--wife-in ROOM] [--model MODEL]}
shift
WIFE=() MODEL=sonnet
while (( $# )); do
  case $1 in
    --wife-in) WIFE=(--wife-in "$2"); shift 2 ;;
    --model) MODEL=$2; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
SETTINGS=${PORCH_STANDIN_DIR:-$HOME/.config/porch-standin}
KEYFILE=$SETTINGS/anthropic-key
[[ -f $KEYFILE ]] || { echo "Put your Anthropic API key in $KEYFILE first (chmod 600)." >&2; exit 1; }
[[ $(stat -f %Lp "$KEYFILE") == 600 ]] || { echo "Make it private first: chmod 600 $KEYFILE" >&2; exit 1; }
PY=$REPO/porch_gateway/.venv/bin/python
mkdir -p "$REPO/usability/runs"

"$PY" "$REPO/usability/standin.py" start "$NAME" "${WIFE[@]}" > "$REPO/usability/runs/.standin-$NAME.log" 2>&1 &
STANDIN=$!
trap 'kill -INT $STANDIN 2>/dev/null; wait $STANDIN 2>/dev/null || true' EXIT
for _ in {1..80}; do
  grep -q "Stand-in Porch is up" "$REPO/usability/runs/.standin-$NAME.log" 2>/dev/null && break
  sleep 0.5
done
grep -q "Stand-in Porch is up" "$REPO/usability/runs/.standin-$NAME.log" || { cat "$REPO/usability/runs/.standin-$NAME.log" >&2; exit 1; }

RUN=$REPO/usability/runs/$NAME
python3 "$REPO/usability/prompt.py" "$NAME" > /dev/null
WORK=$(mktemp -d)  # the agent's working folder: empty, outside the repository
MCP=$(mktemp -t porch-fresh-mcp)  # names the agent, holds no key
"$PY" "$REPO/usability/standin.py" desktop | "$PY" -c 'import json,sys; print(json.dumps({"mcpServers": json.load(sys.stdin)}))' > "$MCP"
echo "Running a fresh agent ($MODEL) on the stand-in. Watch: http://127.0.0.1:8102/flow?engine=ws://127.0.0.1:8002/ws/stage"
( cd "$WORK" && env -i HOME="$HOME" PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" LANG=en_US.UTF-8 TERM=xterm-256color \
    ANTHROPIC_API_KEY="$(<"$KEYFILE")" \
    claude --bare -p "$(<"$RUN/prompt.txt")" --model "$MODEL" --tools "" \
      --strict-mcp-config --mcp-config "$MCP" --allowedTools mcp__porch-fresh \
      --no-session-persistence --max-budget-usd 2 --output-format stream-json --verbose ) > "$RUN/transcript.jsonl"
rm -f "$MCP"; rmdir "$WORK"
kill -INT $STANDIN; wait $STANDIN || true
trap - EXIT
python3 "$REPO/usability/check.py" "$NAME"
