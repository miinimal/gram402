# GRAM-402

Pay-per-request for AI agents on TON. An agent that holds only a TON wallet calls a paid HTTP endpoint, gets
**HTTP 402** with an invoice, pays with a plain GRAM transfer and receives the answer with a **signed receipt bound to
the on-chain payment**. No signup, no API key, no human in the loop.

GRAM-402 uses the message shapes of [x402](https://docs.x402.org) (`accepts`, `X-PAYMENT`, `X-PAYMENT-RESPONSE`) with
a TON-native scheme `ton-transfer`: no facilitator, any TON wallet can pay, the server verifies the transfer on TON.

```
agent ── POST (no auth) ───────────────▶ server
      ◀── 402 {payTo, amount, comment "mai:<invoice>", secret}
agent ── GRAM transfer with the comment ─▶ TON
agent ── POST + X-PAYMENT {invoice, secret} ▶ server: finds the transfer, checks amount and secret
      ◀── 200 answer + receipt {request, response, payment tx} + api_key for the remaining credit
```

## Contents

| Path | What |
|---|---|
| [`SPEC.md`](SPEC.md) | Protocol: messages, statuses, security rules, receipts |
| [`server/pay402.py`](server/pay402.py) | Server module: invoices, on-chain verification (toncenter v3), settlement |
| [`server/app.py`](server/app.py) | Reference server: any OpenAI-compatible chat API, paid per request |
| [`clients/python/gram402.py`](clients/python/gram402.py) | Python client, stdlib only |
| [`clients/typescript/gram402.ts`](clients/typescript/gram402.ts) | TypeScript client, no dependencies |

## Run the reference server

```bash
pip install -r server/requirements.txt
cd server
PAY402_ADDRESS=<your TON wallet> PAY402_NETWORK=testnet uvicorn app:app --port 8402
# optional: UPSTREAM_URL=https://api.example/v1 UPSTREAM_KEY=...  (otherwise a built-in echo model answers)
```

## Pay from an agent

```python
from gram402 import Gram402Client
cli = Gram402Client("http://127.0.0.1:8402", pay=lambda to, nano, comment: my_wallet.transfer(to, nano, comment),
                    max_nano=50_000_000)
print(cli.chat({"model": "any", "messages": [{"role": "user", "content": "hi"}]}))
```

```ts
import { Gram402Client } from './gram402';
const cli = new Gram402Client('http://127.0.0.1:8402', async (to, nano, comment) => { /* send GRAM with comment */ });
console.log(await cli.chat({ model: 'any', messages: [{ role: 'user', content: 'hi' }] }));
```

The wallet is a callback, so any TON wallet library works. The client pays on 402, retries until the transfer is on
chain, keeps the returned key for the remaining credit and pays again when it runs out; `max_nano` caps one request.

## Security

- Transfer comments are public on chain, so the invoice id never settles alone: the private `secret` is required.
- One transaction settles at most one invoice; an invoice settles once.
- Underpaid, aborted or expired payments do not settle.
- A request that fails after payment keeps the credit and returns its key.

Tests for all of the above: `cd server && python3 test_app.py`, `python3 clients/python/test_gram402.py`,
`clients/typescript/test.sh`.

## Status

Draft v0.1. Integrated in the MINIMAL AI gateway code (Cocoon confidential inference on TON); public endpoints on
TON testnet and mainnet are the next step. Also next: top-up of an
existing account by comment, wallet-signed requests (TON Connect proof), packages on PyPI and npm.

Author: Aleksandr Shcherbakov ([@miinimal](https://github.com/miinimal)). License: MIT.
