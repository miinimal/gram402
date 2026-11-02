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
| [`clients/python/gram402_merchant.py`](clients/python/gram402_merchant.py) | Sell your API for GRAM via the hosted facilitator, stdlib only |
| [`clients/typescript/gram402_merchant.ts`](clients/typescript/gram402_merchant.ts) | The same for JavaScript: fetch-style and Express paywalls |
| [`RECEIPTS.md`](RECEIPTS.md) | RECEIPTS v1: portable signed receipts, key pinning, disputes linked by `receipt_id` |
| [`clients/python/minimalai_receipt.py`](clients/python/minimalai_receipt.py) | Offline receipt verifier (pinned key, no call to the issuer) |

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

## Sell your API through the facilitator

No chain code on your side: the hosted facilitator issues invoices to your own wallet and confirms payments on chain.
Agents pay you directly; the facilitator never holds the money. Fee: max(0.001 GRAM, 0.5 %) per settled payment,
taken from your facilitator key balance. Terms: `GET https://ai.minimalcult.org/v1/facilitator`; details in
[`SPEC.md`](SPEC.md#facilitator-for-other-apis).

```python
from gram402_merchant import Merchant
m = Merchant(api_key="mai-...", pay_to="UQ...your wallet")
payment = request.headers.get("X-PAYMENT")
if not payment:
    return 402, m.invoice(amount_nano=50_000_000, resource="/my/endpoint")["x402"]
s = m.settle(payment)
if not s.get("success"):
    return 402, {"error": s.get("invalidReason")}
# serve the request; set header X-PAYMENT-RESPONSE to s["x_payment_response"]
```

Or wrap any ASGI app (FastAPI, Starlette) in one line; unpaid calls get the 402 invoice, paid calls pass through
with `X-PAYMENT-RESPONSE`, and a payment not yet visible on chain gets `402` + `Retry-After` so clients poll:

```python
from gram402_merchant import Merchant, paywall
app = paywall(app, Merchant("mai-...", "UQ...your wallet"), {"/v1/weather": 20_000_000})  # path -> nano-GRAM
```

JavaScript (Express, Hono, Bun, Deno, Cloudflare Workers), no dependencies:

```ts
import { Merchant, withPaywall, expressPaywall } from './gram402_merchant';
const m = new Merchant('mai-...', 'UQ...your wallet');
export default { fetch: withPaywall(m, { '/v1/weather': 20_000_000n }, handler) };  // fetch-style
app.use(expressPaywall(m, { '/v1/weather': 20_000_000n }));                          // Express
```

An agent pays any such endpoint with the client: `Gram402Client(base, pay=...).call(url, method="GET")` (Python) or
`new Gram402Client(base, pay).call(url)` (TypeScript).

A key comes from `POST https://ai.minimalcult.org/v1/agents/onboard`; top it up for fees with the returned
`top_up` comment.

## PayPal mandates: agents pay with PayPal, within limits the owner set once

The same 402 endpoint also accepts PayPal. The agent's owner approves a spending mandate once on PayPal: total
budget, cap per request, expiry, allowed endpoints. From then on the agent pays each request on its own, gets the
answer with a signed receipt bound to the PayPal capture, and is stopped the moment a limit would be exceeded. The
owner can check or revoke the mandate at any time.

```bash
pip install -r server/requirements.txt
python3 demo/paypal_agent_demo.py        # simulated PayPal, no account needed
PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... python3 demo/paypal_agent_demo.py   # live PayPal sandbox

# an LLM agent shops in a small API marketplace with a 30-cent mandate; the server refuses what is over the cap
LLM_URL=https://api.openai.com/v1 LLM_KEY=... LLM_MODEL=gpt-4o-mini python3 demo/ai_shopper.py   # any OpenAI-compatible LLM
```

| Piece | File |
|-------|------|
| Server rail (Vault v3 mandates, Orders v2 captures, limits, idempotency) | `server/paypal402.py` |
| Agent clients with a price cap | `clients/python/paypal_mandate.py`, `clients/typescript/paypal_mandate.ts` |
| Owner page: create, approve, spending meter, revoke | `GET /mandates` (`server/static/owner.html`) |
| MCP server: any MCP agent can pay from the mandate (`mandate_status`, `paid_chat`, `paid_call`) | `clients/python/mandate_mcp.py` |
| Protocol | `SPEC.md`, section "PayPal mandate rail" |
| Tests (fake PayPal API) and a live sandbox check in CI | `server/test_paypal.py`, `server/smoke_paypal_sandbox.py` |

Give the mandate to Claude Desktop (or any MCP client) and the model pays for its own calls:

```json
{"mcpServers": {"gram402-mandate": {
  "command": "python3", "args": ["/path/to/gram402/clients/python/mandate_mcp.py"],
  "env": {"GRAM402_URL": "https://your-gateway", "MANDATE_ID": "m_...", "MANDATE_SECRET": "...", "MANDATE_MAX_CENTS": "25"}}}}
```

The secret stays in the MCP server's environment; the model only sees the tools `mandate_status`, `paid_chat` and
`paid_call`, and every limit is enforced by the server, not by the model.

Sell your own API to PayPal-funded agents, money straight to your PayPal business account, one line for any ASGI app:

```python
from paypal_paywall import paypal_paywall          # server/paypal_paywall.py
app = paypal_paywall(app, {"/v1/weather": 5})      # path -> price in cents; adds the /v1/mandates endpoints

# both rails on one path: GRAM agents and PayPal agents buy the same endpoint
from gram402_merchant import Merchant                # clients/python
app = paypal_paywall(app, {"/v1/weather": {"cents": 5, "nano": 50_000_000}}, gram=Merchant(api_key, pay_to))
```

## NEAR mandates: limits enforced by a smart contract

`near/contract` turns the mandate into a NEAR contract. The owner funds a mandate for an agent account with a cap per
payment, an expiry and allowed merchants. The agent pays a 402 invoice by calling `pay` on the contract; the
contract checks every limit and sends the money straight to the merchant, and the merchant's server verifies the
payment with one view call (`get_payment`). Even a compromised agent cannot spend outside the limits: give it a
function-call access key that may only call `pay`.

Live run on NEAR testnet (CI workflow `near-e2e`, 2026-11-02):

| Step | Transaction |
|------|-------------|
| Mandate created: 0.01 NEAR, cap 0.002 NEAR per payment, one merchant | [3aux4eL8…](https://testnet.nearblocks.io/txns/3aux4eL83GbEJbzZkgcYtVoqCxMK7Nax9wGnK65K7Nob) |
| Agent pays invoice 1, gets the answer with a signed receipt | [5Bw3geXM…](https://testnet.nearblocks.io/txns/5Bw3geXMC9Z3wcy8T4XQsbJLC2L18hmDt2Z8MetNCFbZ) |
| Agent pays invoice 2 | [9Yut41uR…](https://testnet.nearblocks.io/txns/9Yut41uR5sRD6uNHVpMYDPKG67kSmcsr7dZ2XTtRY6Pi) |
| Payment over the cap: refused by the contract ("amount exceeds the per-payment cap") | — |
| Agent's full-access key deleted; only a function-call key for `pay` on the mandate remains | [DVku76ah…](https://testnet.nearblocks.io/txns/DVku76ahMpsfZNNgpsH3oUe1AHktc3uVEexWibzPiUbu) |
| Sandboxed agent still pays through the mandate | [ChgEQwAk…](https://testnet.nearblocks.io/txns/ChgEQwAkw2EEt5pepeZFx2U6SrnADEmggoERZJnFtia3) |
| Sandboxed agent tries to send NEAR directly: cannot be signed, a transfer needs a full-access key the agent no longer has | — |

Agent side: `clients/python/near_mandate.py` and `clients/typescript/near_mandate.ts` — `NearMandateClient(url, mandate_id, pay)` answers 402 by calling
`pay` on the contract through your signing callback (any NEAR library or near-cli-rs), with its own price cap.

Contract: [mandate.g4021793638522-owner.testnet](https://testnet.nearblocks.io/address/mandate.g4021793638522-owner.testnet).
Reproduce: `.github/workflows/near-e2e.yml` (fresh faucet accounts, deploy, mandate, agent payments).

NEAR HACKATHON: work for the hackathon starts at baseline commit `5f3ba49`; see
[the diff](https://github.com/miinimal/gram402/compare/5f3ba49...main). Built with AI coding assistance (Claude).

## Security

- Transfer comments are public on chain, so the invoice id never settles alone: the private `secret` is required.
- One transaction settles at most one invoice; an invoice settles once.
- Underpaid, aborted or expired payments do not settle.
- A request that fails after payment keeps the credit and returns its key.

Tests for all of the above: `cd server && python3 test_app.py`, `python3 clients/python/test_gram402.py`,
`python3 clients/python/test_gram402_merchant.py`, `python3 clients/python/test_paywall.py`,
`clients/typescript/test.sh`.

## Status

Draft v0.1. Running on TON mainnet in the MINIMAL AI gateway (`https://ai.minimalcult.org`, Cocoon confidential
inference): paid chat requests, top-up of an existing account by comment, and the facilitator for other APIs.
Next: wallet-signed requests (TON Connect proof), packages on PyPI and npm.

Author: Aleksandr Shcherbakov ([@miinimal](https://github.com/miinimal)). License: MIT.
