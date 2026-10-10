#!/usr/bin/env bash
# Push Smite node agent updates to the foreign host once SSH is back.
set -euo pipefail
HOST="${1:-bestify}"
SRC_DIR="${2:-/opt/smite/node-update}"
ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" "mkdir -p /tmp/smite-node-update"
scp -o BatchMode=yes -o ConnectTimeout=15 \
  "$SRC_DIR/path_bench.py" \
  "$SRC_DIR/agent.py" \
  "$SRC_DIR/tunnel_live.py" \
  "$SRC_DIR/gre_setup.py" \
  "$HOST:/tmp/smite-node-update/"
ssh -o BatchMode=yes "$HOST" '
  docker cp /tmp/smite-node-update/path_bench.py smite-node:/app/app/path_bench.py
  docker cp /tmp/smite-node-update/agent.py smite-node:/app/app/routers/agent.py
  docker cp /tmp/smite-node-update/tunnel_live.py smite-node:/app/app/tunnel_live.py
  docker cp /tmp/smite-node-update/gre_setup.py smite-node:/app/app/gre_setup.py
  docker restart smite-node
  sleep 2
  curl -s -m 3 http://127.0.0.1:8888/api/agent/status || true
'
echo "Foreign node update applied."
