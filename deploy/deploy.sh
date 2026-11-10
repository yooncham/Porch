#!/usr/bin/env bash
# Deploy the latest main to the demo server, from your laptop (run inside your clone of the repo):
#   deploy/deploy.sh                      deploy origin/main
#   REF=my-branch deploy/deploy.sh        deploy another branch or commit (e.g. to try a PR)
#   SETUP_ARGS=--with-hermes deploy/deploy.sh   first-time setup of a fresh server, including Hermes
#
# It exports the commit with `git archive` and copies it to /opt/porch-app with rsync, so the server
# needs no GitHub access. Then it reruns deploy/setup_droplet.sh and the end-to-end check.
# Override the server with PORCH_HOST and PORCH_KEY.
set -euo pipefail
HOST=${PORCH_HOST:-root@<demo-server>}
KEY=${PORCH_KEY:-$HOME/.ssh/porch_ed25519}
REF=${REF:-origin/main}
SSH="ssh -o IdentitiesOnly=yes -i $KEY"

cd "$(git rev-parse --show-toplevel)"
git fetch -q origin
COMMIT=$(git rev-parse --short "$REF")
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git archive "$REF" | tar -x -C "$TMP"
echo "$COMMIT $(git log -1 --format=%s "$REF")" > "$TMP/DEPLOYED_COMMIT"
echo "deploying $(cat "$TMP/DEPLOYED_COMMIT") to $HOST"

$SSH "$HOST" "mkdir -p /opt/porch-app"
rsync -az --delete --exclude '.venv/' --exclude 'audit.jsonl' -e "$SSH" "$TMP/" "$HOST:/opt/porch-app/"
$SSH "$HOST" "set -e; bash /opt/porch-app/deploy/setup_droplet.sh ${SETUP_ARGS:-}
  cd /opt/porch-app/porch_gateway && .venv/bin/python check_demo.py | tail -1"
