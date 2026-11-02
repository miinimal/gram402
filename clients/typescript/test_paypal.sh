#!/bin/bash
# TypeScript PayPal mandate client against the reference server with a simulated PayPal API on 127.0.0.1:18403.
set -e
cd "$(dirname "$0")"
DB=$(mktemp -d)
( cd ../../server && GRAM402_DB=$DB/t.db PAYPAL_CLIENT_ID=x PAYPAL_CLIENT_SECRET=x PAYPAL_PRICE_CENTS=10 \
  PUBLIC_URL=http://127.0.0.1:18403 python3 -c "
import uvicorn, app, paypal402, fake_paypal
paypal402._transport = fake_paypal.transport()
uvicorn.run(app.app, host='127.0.0.1', port=18403, log_level='warning')" ) & H=$!
trap 'kill $H 2>/dev/null' EXIT
for i in $(seq 1 40); do curl -sf -m 1 http://127.0.0.1:18403/health >/dev/null && break; sleep 0.5; done
OUT=$(mktemp -d)
npx -y esbuild@0.24.0 test_paypal.ts --bundle --platform=node --format=cjs --outfile="$OUT/t.js" --log-level=warning
node "$OUT/t.js"
