#!/bin/bash
# Merchant paywall test (TypeScript): bundle with esbuild, run with node. No network.
set -e
cd "$(dirname "$0")"
OUT=$(mktemp -d)
npx -y esbuild@0.24.0 test_merchant.ts --bundle --platform=node --format=cjs --outfile="$OUT/t.js" --log-level=warning
node "$OUT/t.js"
