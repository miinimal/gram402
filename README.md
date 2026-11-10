# GRAM-402

**AI agents pay for APIs on their own, never more than their owner allowed.** A paid endpoint answers an unpaid
request with **HTTP 402** and a list of ways to pay; the agent pays and gets the answer with a **signed receipt bound
to the payment**. Three rails behind one 402:

- **PayPal mandates**: the owner approves a spending mandate once on PayPal (budget, cap per request, expiry, allowed
  endpoints); the server charges inside those limits only, and the owner can revoke at any time.
- **NEAR mandates**: the same limits enforced by a smart contract; the agent's key can only call `pay`.
- **GRAM on TON**: an agent with nothing but a TON wallet pays with a plain transfer; no signup, no API key.

The problem: agents can call any API but cannot pay for one. A card needs a human at checkout, and handing an agent
your card or seed phrase is unsafe. GRAM-402 keeps the human in control of the limits and out of the loop for each
payment.

## Try it

| Do this | See this |
|---|---|
| Open the hosted demo **https://gram402.minimalcult.org/mandates** (PayPal sandbox), create a mandate, approve it with a PayPal **sandbox** buyer; or run the same server locally (see [Run it yourself](#run-it-yourself)) and open http://localhost:8402/mandates | "Mandate approved"; then "Show spending" lists every charge with its PayPal capture id |
| `python3 demo/paypal_agent_demo.py` (no account needed, simulated PayPal) | the agent pays twice, is stopped by the budget, then refused after revoke |
| Live PayPal sandbox run [41516703950](https://github.com/miinimal/gram402/actions/runs/41516703950) | captures `4AL30836MY6733008` and `3TJ7308016156922W`, budget stop, revoke |
| NEAR testnet run (section [NEAR mandates](#near-mandates-limits-enforced-by-a-smart-contract)) | contract payments, over-cap payment refused on chain |
| `cd server && python3 test_app.py && python3 test_paypal.py && python3 test_paypal_concurrency.py && python3 test_near.py` | all rails tested offline, including two processes spending one budget at once and a crash after capture |

The TON rail in detail:

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
| [`RECEIPTS.md`](RECEIPTS.md) | RECEIPTS v1: portable signed receipts, key pinning, disputes linked by `receipt_id`, notarization (Merkle root over hashes, anchored in TON) |
| [`clients/python/minimalai_receipt.py`](clients/python/minimalai_receipt.py) | Offline receipt verifier (pinned key, no call to the issuer) |
| [`clients/python/minimalai_registry.py`](clients/python/minimalai_registry.py), [`clients/typescript/minimalai_registry.ts`](clients/typescript/minimalai_registry.ts) | Check an agent before you pay it: signed agent-registry record, pinned key, freshness, explicit trust policy |

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
taken from your facilitator key balance. Terms: `GET <facilitator>/v1/facilitator`; details in
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

A key comes from `POST <facilitator>/v1/agents/onboard`; top it up for fees with the returned
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

No local setup: the `paypal-live` workflow runs the same demo against the PayPal sandbox from repository secrets.
The approval link appears as a notice on the run; approve on any device. The server confirms the approval with
PayPal itself (setup-token status), so the return redirect does not have to reach it.

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
payment, an expiry and allowed merchants; the deposit also pays the mandate's own storage. The agent pays a 402
invoice by calling `pay` on the contract; the contract checks every limit, sends the money straight to the merchant
and logs a `paid` event. The merchant's server verifies the payment from that transaction (event from this
contract, this invoice, this merchant, at least the price, no failed receipt); payments add no contract state, so
nobody can make the contract pay for storage or squat invoice ids. A failed transfer puts the amount back into the
mandate; a failed refund on revoke re-activates it. Even a compromised agent cannot spend outside the limits: give it
a function-call access key that may only call `pay`.

Live run on NEAR testnet (CI workflow `near-e2e`, current contract, 2026-11-02):

| Step | Transaction |
|------|-------------|
| Mandate created: 0.02 NEAR deposit (storage + budget), cap 0.002 NEAR per payment, one merchant | [6YYcErz6…](https://testnet.nearblocks.io/txns/6YYcErz6scidQ5ST2UVy8cmjiEchdvP2KhHjxrhfoNc7) |
| Agent pays invoice 1; the server verifies the transaction and answers with a signed receipt | [B5YUBzjK…](https://testnet.nearblocks.io/txns/B5YUBzjKZipfme7DEEMMJ5LXMs75DaqzWt6K89sbAiYB) |
| Agent pays invoice 2 | [zGTNJXvJ…](https://testnet.nearblocks.io/txns/zGTNJXvJfxDihXTmQbJ4iwC17oKcWPzh3yTBzEE3nqJ) |
| Payment over the cap: refused by the contract ("amount exceeds the per-payment cap") | — |
| Agent's full-access key deleted; only a function-call key for `pay` on the mandate remains | [BohtXmVb…](https://testnet.nearblocks.io/txns/BohtXmVb7esbLEnkiZ2aEKuXHHVgkZgp9xTnfVi85tzS) |
| Sandboxed agent still pays through the mandate | [HgEgaHL1…](https://testnet.nearblocks.io/txns/HgEgaHL1ou8JHkewG33sau5u7gaGyX4Cfh1icCqjRYND) |
| Sandboxed agent tries to send NEAR directly: cannot be signed, a transfer needs a full-access key the agent no longer has | — |

Contract: [mandate.g4021793642369-owner.testnet](https://testnet.nearblocks.io/address/mandate.g4021793642369-owner.testnet).

### Agent economy: agents buying from agents

CI workflow `near-economy` runs a small market on NEAR testnet (2026-11-02, contract
[mandate.g402e1793642375.testnet](https://testnet.nearblocks.io/address/mandate.g402e1793642375.testnet)):
agent A buys trip plans from agent B; to write a plan, B buys a weather report from service C. A pays from a
mandate its owner funded; B opens its own procurement mandate from its first sale and pays C from it. Every payment
is an HTTP 402 invoice settled through the mandate contract. (C answers with the demo echo model; payments are real
testnet transactions.)

| Step | Transaction |
|------|-------------|
| A pays B for plan 1 (0.01 NEAR) | [HS6nVJMG…](https://testnet.nearblocks.io/txns/HS6nVJMGpCf7XqqtG7QybXTihdDPZFCBBXKhdsQh52f8) |
| B opens its procurement mandate from that revenue (0.006 NEAR) | [D2LGsxy4…](https://testnet.nearblocks.io/txns/D2LGsxy4N6LgtmB6u3EmgvwMF74dPMfShHLBe5PPweoT) |
| B pays C for weather (0.001 NEAR) | [5CsXJGn1…](https://testnet.nearblocks.io/txns/5CsXJGn1hpH9Z3i5w82j3VDqFBE1ovyyTgf2jyXvS8o2) |
| A pays B for plan 2 | [6MqRVTKA…](https://testnet.nearblocks.io/txns/6MqRVTKAkYkVn531iVsL4hRPexYPH1WmemCdti8egw5P) |
| B pays C again | [AMF9Magw…](https://testnet.nearblocks.io/txns/AMF9MagwVAj3Y5ckn15tBwvVncsbu5KQPLzBH72VV5Qd) |

B's result: revenue 0.020 NEAR, data cost 0.002 NEAR, gross margin 0.018 NEAR, all visible on chain.

Agent side: `clients/python/near_mandate.py` and `clients/typescript/near_mandate.ts` — `NearMandateClient(url, mandate_id, pay, contract)` answers 402 by calling
`pay` on the contract through your signing callback (any NEAR library or near-cli-rs), with its own price cap. The
contract comes from the agent's configuration and the call arguments are built by the client (its mandate, the
advertised merchant, the checked price), so a hostile server cannot redirect the call or inflate the amount.

Reproduce: `.github/workflows/near-e2e.yml` (fresh faucet accounts, deploy, mandate, agent payments).

NEAR HACKATHON: work for the hackathon starts at baseline commit `5f3ba49`; see
[the diff](https://github.com/miinimal/gram402/compare/5f3ba49...main). Built with AI coding assistance (Claude).

## Check an agent before you pay it

Paying another agent is only safe when you know its record. The agent registry
ranks agents by independent earnings: paid work from counterparties the agent never paid back, so two accounts paying
each other do not climb. One call returns a record signed with the issuer's Ed25519 key (the key is also written to TON
in the daily anchor comment, see [RECEIPTS.md](RECEIPTS.md)); these modules verify it against a key you pin, reject old
records (replay) and apply an explicit policy.

```python
from minimalai_registry import check      # clients/python, needs only `cryptography`
d = check("scout", key="<pinned 64-hex key>", min_level="active", min_independent=1, max_upheld=0)
if not d["ok"]:
    raise SystemExit(d["reason"])      # e.g. "no public track record", "level new below active"
```

```ts
import { checkAgent } from './minimalai_registry';   // clients/typescript, no dependencies
const d = await checkAgent('scout', { key: '<pinned 64-hex key>' });
if (!d.ok) throw new Error(d.reason);
```

The agent can be given by name, 32-hex worker id or TON wallet. Without `key` the issuer key is fetched from
`/.well-known/minimalai.json`, which trusts the network: pin it in production.

## x402 conformance probe

Can a buyer agent turn this 402 into a payment? [`tools/x402probe.py`](tools/x402probe.py) validates the whole offer
for x402 v1 (JSON body) and v2 (`PAYMENT-REQUIRED` header) and GRAM-402: every field of every payment option, the
EIP-712 domain for `exact` on EVM, header vs body (by what the buyer pays, so v1 and v2 spellings of one offer agree),
top-level fields vs `accepts`, and GET vs POST. Every verdict keeps the captured response and its SHA-256.

```bash
pip install httpx
python3 tools/x402probe.py https://api.example.com/paid
```

The hosted probe table is offline while the MINIMAL AI gateway is paused; run the probe locally as above.

## Run it yourself

```bash
git clone https://github.com/miinimal/gram402 && cd gram402/server
pip install -r requirements.txt
python3 test_app.py && python3 test_paypal.py && python3 test_near.py   # offline, no keys needed
# PayPal sandbox: create an app at https://developer.paypal.com/dashboard/applications/sandbox
PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... PAYPAL_ENV=sandbox PAYPAL_SANDBOX_ONLY=1 \
  PUBLIC_URL=http://localhost:8402 uvicorn app:app --port 8402
```

Open http://localhost:8402/mandates, create a mandate and approve it with a sandbox buyer account
(PayPal Developer Dashboard, Sandbox accounts, Personal). Without PayPal keys, `python3 demo/paypal_agent_demo.py`
runs the same flow against a simulated PayPal.

## Security

- Transfer comments are public on chain, so the invoice id never settles alone: the private `secret` is required.
- One transaction settles at most one invoice; an invoice settles once.
- Underpaid, aborted or expired payments do not settle.
- A request that fails after payment keeps the credit and returns its key.

Tests for all of the above: `cd server && python3 test_app.py`, `python3 clients/python/test_gram402.py`,
`python3 clients/python/test_gram402_merchant.py`, `python3 clients/python/test_paywall.py`,
`clients/typescript/test.sh`.

## Status

v0.1. PayPal rail: live in the PayPal sandbox (run locally, see below; CI run linked above). NEAR rail: live on NEAR testnet. TON rail: ran on TON mainnet in the MINIMAL AI gateway (currently paused; Cocoon confidential
inference): paid chat requests, top-up of an existing account by comment, and the facilitator for other APIs.
Next: wallet-signed requests (TON Connect proof), packages on PyPI and npm.

Author: Aleksandr Shcherbakov ([@miinimal](https://github.com/miinimal)). License: MIT.
