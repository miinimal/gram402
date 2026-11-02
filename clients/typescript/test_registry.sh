#!/bin/bash
# Standalone TS test of the registry check (no server): bundle with esbuild, run with node.
set -e
cd "$(dirname "$0")"
OUT=$(mktemp -d)
npx -y esbuild@0.24.0 test_registry.ts --bundle --platform=node --format=cjs --outfile="$OUT/t.js" --log-level=warning
node "$OUT/t.js"
