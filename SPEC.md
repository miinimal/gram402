# GRAM-402: pay-per-request for AI agents on TON

Status: draft v0.1 (2026-11-02). Reference implementation: `server/pay402.py` and `server/app.py` in this repository;
integrated in the MINIMAL AI gateway code.
Author: Aleksandr Shcherbakov (@miinimal).

## Goal

An AI agent that holds only a TON wallet can call a paid AI endpoint and get an answer: no signup, no API key, no
human, no card. Payment is a plain GRAM transfer. The answer comes with a signed receipt and, on COCOON, with the
TEE attestation evidence of the network that computed it.

## Relation to x402

GRAM-402 uses the HTTP 402 message shapes of [x402](https://docs.x402.org) (`accepts`, `X-PAYMENT`,
`X-PAYMENT-RESPONSE`) with a TON-native scheme `ton-transfer`, so x402-aware clients only need a new scheme handler.
Differences from the x402 `exact` scheme:

- no facilitator: the server verifies the transfer on TON directly;
- the payment is a standard wallet transfer with a text comment, which any TON wallet can send (no signed
  authorization object, no custom contract);
- the amount is an upper bound; the overpayment stays as credit on an account bound to the payer's wallet.

## Flow

```
agent                                   server (payTo = server wallet)
  | POST /v1/chat/completions (no auth)   |
  |-------------------------------------->|
  |   402 {accepts:[ton-transfer ...]}    |  invoice (public) + secret (private), amount, expiry
  |<--------------------------------------|
  | GRAM transfer to payTo, comment "mai:<invoice>"   (on TON)
  | POST /v1/chat/completions             |
  |   X-PAYMENT: base64 {payload:{invoice,secret}}    |
  |-------------------------------------->|  find transfer on chain, check comment, amount, secret,
  |   200 answer + receipt                |  tx not used before; credit paid amount; run; charge cost
  |   X-PAYMENT-RESPONSE: base64 {...}    |
  |<--------------------------------------|
```

## 402 response

```json
{
  "x402Version": 1,
  "error": "payment required",
  "accepts": [{
    "scheme": "ton-transfer",
    "network": "ton-testnet",
    "asset": "GRAM",
    "maxAmountRequired": "10000000",
    "payTo": "0QDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVQ_U",
    "resource": "/v1/chat/completions",
    "mimeType": "application/json",
    "maxTimeoutSeconds": 1800,
    "extra": {"invoice": "9f1c2a7b3d4e5f60", "secret": "<private>", "comment": "mai:9f1c2a7b3d4e5f60",
              "expiresAt": 1793619000, "decimals": 9}
  }]
}
```

- `network`: `ton-mainnet` or `ton-testnet`.
- `maxAmountRequired`: nano-GRAM, upper bound for this request (prompt size and `max_tokens` at the server's price).
- `extra.comment`: the exact text comment of the transfer.
- `extra.secret`: returned only to the requester; never put it on chain.

## Payment

A transfer of at least `maxAmountRequired` nano-GRAM to `payTo` with text comment `extra.comment`, sent before
`expiresAt`. Any wallet version works. The sender address becomes the account owner (`payer`).

## Retry with X-PAYMENT

```
X-PAYMENT: base64({"x402Version":1,"scheme":"ton-transfer","network":"ton-testnet",
                   "payload":{"invoice":"9f1c2a7b3d4e5f60","secret":"<private>"}})
```

Plain JSON is accepted too. Server answers:

| Status | Meaning |
|---|---|
| 200 | settled and answered; `X-PAYMENT-RESPONSE` and the response's payment block carry the settlement |
| 402 + `Retry-After: 5` | transfer not visible on chain yet; repeat the same request |
| 402 | wrong secret, expired, underpaid, invoice already settled, or the transaction already settled another invoice |
| 4xx/5xx + payment block | settled, but the request itself failed; credit is kept, use the returned `api_key` |

## Settlement (X-PAYMENT-RESPONSE)

```json
{"success": true, "scheme": "ton-transfer", "network": "ton-testnet", "transaction": "<tx hash>",
 "payer": "0:...", "paid_nano": 30000000, "charged_nano": 1200, "credit_left_nano": 29998800,
 "api_key": "mai-..."}
```

`api_key` is shown once. It spends the remaining credit as a normal `Authorization: Bearer` key.

## Security rules

1. The comment is public on chain, so the invoice id never settles alone: the secret is required. This blocks
   front-running by observers of the chain.
2. A transaction hash settles at most one invoice; an invoice settles once (atomic status change).
3. Aborted or failed incoming transactions are ignored.
4. Only transfers to `payTo` with the exact comment and at least the quoted amount count.
5. A failed request after settlement never loses the payment: the credit and its key are returned with the error.

## Receipts and confidentiality

Every answer carries a receipt signed with the server's Ed25519 key (MINIMAL AI publishes it at `/.well-known/minimalai.json`; the reference server at `/health`):
request hash, response hash, model, route, time and, on COCOON mainnet, the latest RA-TLS evidence of the proxy
(TDX quote bound to its TLS key, image hash on the root contract allowlist). For GRAM-402 requests the signed
receipt also carries `payment: {network, transaction, payer, charged_nano}`, so the on-chain transfer, the request
and the answer are bound by one signature. Verify with `POST /v1/receipts/verify`, or offline with the pinned key as
described in [RECEIPTS.md](RECEIPTS.md) (portable format, disputes linked to receipts).
On the community testnet TEE is not enforced, and receipts state `tee_attested: false`.

## Clients

Python, stdlib only: `clients/python/gram402.py`. The wallet is a callback, so any TON wallet library works:

```python
from gram402 import Gram402Client
cli = Gram402Client("https://gateway.example", pay=lambda to, nano, comment: wallet.transfer(to, nano, comment),
                    max_nano=50_000_000)
print(cli.chat({"model": "Qwen/Qwen3-8B", "messages": [{"role": "user", "content": "hi"}]}))
```

It pays on 402, retries until the transfer is on chain, keeps the returned key for the remaining credit and pays
again when the credit runs out. `max_nano` caps the price of one request. 

TypeScript, no dependencies: `clients/typescript/gram402.ts` (same behaviour as the Python client; test:
`clients/typescript/test.sh`).

MCP (minimalai-mcp): `pay_quote` returns the invoice and a `ton://transfer` link for any wallet, `pay_claim` returns
the answer with the signed receipt once the transfer is on chain, `agent_passport` reads a passport. No key needed.

## Agent passport

In the MINIMAL AI gateway, `GET /v1/agents/{wallet}/passport` returns a signed, public track record of an agent by its TON wallet (raw or
user-friendly form): number and total of GRAM-402 settlements (each is an on-chain transfer), first and last seen,
accepted marketplace tasks done with the wallet's accounts, and a level (`unknown`, `new`, `active`, `trusted`).
Signed with the same Ed25519 key as receipts; verify the same way. `GET /v1/agents/{wallet}/badge.svg` renders it
as a badge. Prompts, answers and per-request usage are never exposed.

## Facilitator (for other APIs)

Any API can accept GRAM-402 without running chain code: the facilitator at MINIMAL AI (`https://ai.minimalcult.org`) issues the invoice, finds the
transfer and confirms it is genuine and unused. Payments go straight to the merchant's own wallet (no custody).

1. Merchant gets a key (`POST /v1/agents/onboard`) and tops it up for fees.
2. On an unpaid request, the merchant's server calls `POST /v1/facilitator/invoices {pay_to, amount_nano, resource}`
   and replies HTTP 402 with the returned `x402` body. The transfer comment is `f402:<invoice>`.
3. When the agent retries with `X-PAYMENT`, the server calls `POST /v1/facilitator/settle {payment: <header>}`;
   on `success` it serves the request and returns `X-PAYMENT-RESPONSE` = `x_payment_response`.
   `POST /v1/facilitator/verify` checks without consuming.

Balance rail: an agent with a MINIMAL AI key may pay a facilitator invoice from its key balance instead of a chain
transfer: `POST /v1/facilitator/pay {invoice, secret}` moves the amount to the merchant key at once (the withdrawable
part stays withdrawable, credit-only GRAM stays credit-only), returns `x_payment` for the retry, and the merchant
settles it like any payment (`rail: balance`, transaction `internal:<invoice>`). A merchant cannot pay its own invoice.
Invoices advertise it in `extra.payFromBalance`; `Gram402Client(balance_key=...)` uses it and falls back to the wallet.

Fee: max(0.001 GRAM, 0.5 %) per settled payment, reserved from the merchant's key when the invoice is created and
released if the invoice expires unpaid (one grace hour after expiry for late confirmations). Terms:
`GET /v1/facilitator`. A transaction settles at most one invoice; invoices are bound to the merchant key that made them.
Merchant helper: [`clients/python/gram402_merchant.py`](clients/python/gram402_merchant.py).

## Next (v0.2)

- top-up of an existing account: comment `mai:k:<account id>`, no new key;
- wallet-signed requests instead of the key (TON Connect `ton_proof` style), so the wallet itself is the credential;
- npm and PyPI packages for the clients above.

## PayPal mandate rail (`paypal-mandate`)

Status: draft v0.1 (2026-11-02). Reference implementation: `server/paypal402.py`, tests `server/test_paypal.py`.

Agents whose owner has no crypto can pay the same 402 endpoint from a PayPal account. The owner approves a spending
mandate once on PayPal; after that the agent pays per request with no human in the loop, strictly within the limits.

Mandate (owner side):

| Call | Effect |
|------|--------|
| `POST /v1/mandates {budget_cents, per_request_cents, days, resources?}` | creates a pending mandate and a PayPal Vault v3 setup token; returns `mandate`, `secret` (once) and `approve_url` |
| owner opens `approve_url`, approves on PayPal | PayPal redirects to `/v1/mandates/{id}/approved`; the server stores the vault payment token; status `active` |
| `GET /v1/mandates/{id}` + header `X-Mandate-Secret` | status, budget, spent, left, expiry |
| `DELETE /v1/mandates/{id}` + header `X-Mandate-Secret` | revokes the mandate at once |

Payment (agent side): the 402 `accepts` list carries a second entry next to `ton-transfer`:

```json
{"scheme": "paypal-mandate", "network": "paypal-sandbox", "asset": "USD", "maxAmountRequired": "5",
 "extra": {"invoice": "p_1a2b3c4d5e6f7a8b", "decimals": 2, "mandateUrl": "https://host/v1/mandates"}}
```

The agent repeats the request with `X-PAYMENT = base64 JSON {x402Version:1, scheme:"paypal-mandate",
payload:{mandate, secret, invoice}}`. The server checks, in order: mandate secret, status `active`, expiry, invoice
open and not expired, resource allowlist, per-request cap, remaining budget. It reserves the amount, captures a PayPal
order against the vaulted account (Orders v2, `stored_credential.payment_initiator = MERCHANT`) with idempotency keys,
and only then runs the request. A failed capture releases the reservation. The answer carries a receipt whose
`payment` block holds `{network: "paypal-<env>", transaction: <capture id>, payer: <mandate id>, charged_cents,
currency}`, signed like every GRAM-402 receipt.

Security: mandate secrets are stored as SHA-256 hashes; the PayPal vault id never leaves the server; every PayPal call
uses a `PayPal-Request-Id`, so a retried capture cannot charge twice; an invoice settles at most once.
