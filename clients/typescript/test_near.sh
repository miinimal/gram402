#!/bin/bash
# TypeScript NEAR mandate client against the reference server with a simulated NEAR RPC on 127.0.0.1:18404.
set -e
cd "$(dirname "$0")"
DB=$(mktemp -d)
( cd ../../server && GRAM402_DB=$DB/t.db PAY402_NETWORK=testnet PAY402_ADDRESS=0QDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVQ_U \
  NEAR_MANDATE_CONTRACT=mandate.gram402.testnet NEAR_MERCHANT=shop.testnet NEAR_PRICE_YOCTO=1000000000000000000000 \
  python3 -c "
import base64, json, httpx, uvicorn, app, near402
from fastapi import Request
TXS = {}
def rpc(req):
    b = json.loads(req.content)
    return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': TXS[b['params']['tx_hash']]})
near402._transport = httpx.MockTransport(rpc)
@app.app.post('/_test/near_pay')
async def near_pay(request: Request):
    a = await request.json()
    ev = {'standard': 'near-mandate', 'event': 'paid', 'data': [{'mandate_id': a['mandate_id'], 'merchant': a['merchant'], 'amount': a['amount'], 'invoice': a['invoice']}]}
    h = 'TSTX%d' % len(TXS)
    TXS[h] = {'status': {'SuccessValue': ''}, 'receipts_outcome': [{'outcome': {'executor_id': 'mandate.gram402.testnet', 'logs': ['EVENT_JSON:' + json.dumps(ev)], 'status': {'SuccessValue': ''}}}]}
    return {'tx': h}
uvicorn.run(app.app, host='127.0.0.1', port=18404, log_level='warning')" ) & H=$!
trap 'kill $H 2>/dev/null' EXIT
for i in $(seq 1 40); do curl -sf -m 1 http://127.0.0.1:18404/health >/dev/null && break; sleep 0.5; done
OUT=$(mktemp -d)
npx -y esbuild@0.24.0 test_near.ts --bundle --platform=node --format=cjs --outfile="$OUT/t.js" --log-level=warning
node "$OUT/t.js"
