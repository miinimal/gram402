# Security policy

GRAM-402 moves money for AI agents, so security reports get priority over features.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** in this repository
(private vulnerability reporting). Do not open a public issue for a vulnerability.

Include the affected component (server rail, paywall, client, MCP server, NEAR contract), the version or commit,
a description and a reproduction. We acknowledge within 72 hours and aim to ship a fix for confirmed critical and
high issues within 7 days, then publish an advisory with credit unless you ask otherwise.

## Scope

- `server/` — GRAM-402 reference server and rails: `pay402.py` (GRAM on TON), `paypal402.py` (PayPal mandates),
  `near402.py` (NEAR mandates), `paypal_paywall.py`, `receipts.py`.
- `near/contract` — NEAR mandate contract.
- `clients/` — Python and TypeScript clients, MCP server.

Out of scope: the demo scripts in `demo/` when run with simulated providers, third-party services (PayPal, NEAR,
TON infrastructure) and their own bugs.

## Design rules we hold ourselves to

- Spending limits are enforced by the server or the contract, never by the model or the agent's code.
- Secrets are stored only as hashes; PayPal vault ids never leave the server.
- Every external payment call is idempotent; an invoice settles at most once; a public invoice id never settles
  without its private secret.
- Unpaid requests and payment attempts are rate-limited per address (IPv6 per /64); `X-Forwarded-For` is honoured
  only with `TRUST_X_FORWARDED_FOR=1` behind your own proxy. Public demos set `PAYPAL_SANDBOX_ONLY=1`.
- Agent clients never execute a server-chosen contract call: the NEAR contract comes from client configuration and
  the arguments are built by the client.
- Every change runs the full test suite in CI; payment changes come with tests for the abuse cases they touch.
