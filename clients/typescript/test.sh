#!/bin/bash
set -e
cd "$(dirname "$0")"
python3 harness.py & H=$!
trap 'kill $H 2>/dev/null' EXIT
for i in $(seq 1 40); do curl -sf -m 1 http://127.0.0.1:18402/health >/dev/null && break; sleep 0.5; done
OUT=$(mktemp -d)
npx -y esbuild@0.24.0 test.ts --bundle --platform=node --format=cjs --outfile="$OUT/t.js" --log-level=warning
node "$OUT/t.js"
