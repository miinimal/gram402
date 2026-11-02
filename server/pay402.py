"""GRAM-402: pay-per-request for AI agents with a TON wallet only (no signup, no API key, no human).

Protocol (docs/protocol/GRAM-402.md):
1. Agent calls a paid endpoint without Authorization -> HTTP 402 with x402-shaped payment requirements:
   scheme "ton-transfer", asset GRAM, payTo, maxAmountRequired, extra.invoice (public, goes into the transfer
   comment "mai:<invoice>") and extra.secret (private, proves who asked for the invoice).
2. Agent sends a plain GRAM transfer to payTo with comment "mai:<invoice>".
3. Agent repeats the request with header X-PAYMENT = base64(JSON {x402Version, scheme, network,
   payload: {invoice, secret}}).
4. Gateway finds the transfer on chain, checks amount, comment and secret, credits the full paid amount to a new
   wallet-bound account, runs the request, charges the actual cost and returns X-PAYMENT-RESPONSE with the settlement
   (tx hash, payer, credited, api_key for the remaining credit). The remaining credit is spent with that api_key.

The comment is public on chain, so the invoice id alone never settles: the secret is required (front-running guard).
A transaction hash settles at most one invoice. Author: Aleksandr Shcherbakov (@miinimal) - MINIMALCULT.
"""
import base64
import hashlib
import json
import os
import secrets
import time

import httpx

ENV = os.environ.get
PAY_TO = ENV("PAY402_ADDRESS", "")
NETWORK = ENV("PAY402_NETWORK", "testnet")
MIN_NANO = int(ENV("PAY402_MIN_NANO", "10000000"))  # 0.01 GRAM
TTL = int(ENV("PAY402_TTL_S", "1800"))
TONCENTER_KEY = ENV("TONCENTER_API_KEY", "")  # optional: lifts the 1 request/s public limit
API = {"mainnet": "https://toncenter.com/api/v3", "testnet": "https://testnet.toncenter.com/api/v3"}
SCHEME = "ton-transfer"


def enabled():
    return bool(PAY_TO)


def network_id():
    return "ton-" + NETWORK


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def setup_db(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS pay_invoices(id TEXT PRIMARY KEY, secret_hash TEXT, created INTEGER, expires INTEGER,
        amount_nano INTEGER, resource TEXT, status TEXT NOT NULL DEFAULT 'open', tx_hash TEXT, payer TEXT,
        paid_nano INTEGER, key_hash TEXT);
    CREATE TABLE IF NOT EXISTS pay_txs(tx_hash TEXT PRIMARY KEY, invoice TEXT, ts INTEGER);
    """)


def quote_nano(body, cost_fn, markup):
    """Upper bound for one request: prompt estimated from characters, completion from max_tokens."""
    chars = len(json.dumps(body.get("messages") or [], ensure_ascii=False))
    max_out = int(body.get("max_completion_tokens") or body.get("max_tokens") or 1024)
    est = cost_fn({"prompt": chars // 3 + 16, "cached": 0, "completion": max_out, "reasoning": 0})
    return max(MIN_NANO, int(est * markup))


def create_invoice(c, amount_nano, resource, now=None):
    now = now or int(time.time())
    inv, sec = secrets.token_hex(8), secrets.token_urlsafe(24)
    c.execute("INSERT INTO pay_invoices(id,secret_hash,created,expires,amount_nano,resource) VALUES(?,?,?,?,?,?)",
              (inv, _h(sec), now, now + TTL, amount_nano, resource))
    return {"invoice": inv, "secret": sec, "amount_nano": amount_nano, "expires": now + TTL}


def requirements(inv, resource, error="payment required"):
    return {"x402Version": 1, "error": error, "accepts": [{
        "scheme": SCHEME, "network": network_id(), "asset": "GRAM", "maxAmountRequired": str(inv["amount_nano"]),
        "payTo": PAY_TO, "resource": resource, "mimeType": "application/json", "maxTimeoutSeconds": TTL,
        "description": "One AI request on COCOON (confidential compute on TON). Overpayment stays as credit.",
        "extra": {"invoice": inv["invoice"], "secret": inv["secret"], "comment": "mai:" + inv["invoice"],
                  "expiresAt": inv["expires"], "decimals": 9,
                  "howTo": "send a GRAM transfer to payTo with the exact comment, then repeat the request with header "
                           "X-PAYMENT = base64 JSON {x402Version:1, scheme, network, payload:{invoice, secret}}"}}]}


def parse_header(value):
    raw = value.strip()
    try:
        d = json.loads(raw if raw.startswith("{") else base64.b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:  # noqa: BLE001
        return None
    p = d.get("payload") or d
    if d.get("scheme", SCHEME) != SCHEME or not p.get("invoice") or not p.get("secret"):
        return None
    return {"invoice": str(p["invoice"])[:32], "secret": str(p["secret"])[:128]}


def find_payment(txs, invoice, min_nano, short=None):
    """Pick the incoming transfer with comment mai:<invoice>; returns (tx_hash, payer, value_nano) or None.
    short: optional list; a matching transfer below min_nano (e.g. the wallet took the network fee out of the value)
    is appended to it as (tx_hash, payer, value_nano) so the caller can credit it instead of losing it."""
    want = "mai:" + invoice
    for t in txs:
        im = t.get("in_msg") or {}
        dec = ((im.get("message_content") or {}).get("decoded") or {})
        if dec.get("comment", "").strip() != want or not im.get("source"):
            continue
        desc = t.get("description") or {}
        if desc.get("aborted") or (desc.get("compute_ph") or {}).get("success") is False:
            continue
        value = int(im.get("value") or 0)
        if value >= min_nano:
            return t["hash"], im["source"], value
        if short is not None and value > 0:
            short.append((t["hash"], im["source"], value))
    return None


async def fetch_incoming(client=None, limit=100):
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        r = await client.get(f"{API[NETWORK]}/transactions",
                             params={"account": PAY_TO, "limit": limit, "sort": "desc"},
                             headers={"X-API-Key": TONCENTER_KEY} if TONCENTER_KEY else None)
        r.raise_for_status()
        return r.json().get("transactions", [])
    finally:
        if own:
            await client.aclose()


def settle(c, claim, txs, new_key_fn, now=None):
    """Validate a claim against fetched transactions. Returns dict with ok/error; on success creates the funded key."""
    now = now or int(time.time())
    row = c.execute("SELECT * FROM pay_invoices WHERE id=?", (claim["invoice"],)).fetchone()
    if not row or not secrets.compare_digest(row["secret_hash"], _h(claim["secret"])):
        return {"ok": False, "error": "unknown invoice or wrong secret"}
    if row["status"] == "paid":
        return {"ok": False, "error": "invoice already settled; use the api_key returned with the first settlement"}
    short = []
    found = find_payment(txs, claim["invoice"], int(row["amount_nano"]), short)
    if not found and short:
        # underpaid: never lose the payer's money. Credit what arrived to a key bound to its wallet and say so;
        # no answer is served for this invoice (the price was not covered).
        tx_hash, payer, value = short[0]
        if not c.execute("SELECT 1 FROM pay_txs WHERE tx_hash=?", (tx_hash,)).fetchone() and \
                c.execute("UPDATE pay_invoices SET status='underpaid' WHERE id=? AND status='open'", (claim["invoice"],)).rowcount == 1:
            c.execute("INSERT INTO pay_txs VALUES(?,?,?)", (tx_hash, claim["invoice"], now))
            api_key, key_hash = new_key_fn(c, payer, value, tx_hash)
            c.execute("UPDATE pay_invoices SET tx_hash=?, payer=?, paid_nano=?, key_hash=? WHERE id=?",
                      (tx_hash, payer, value, key_hash, claim["invoice"]))
            return {"ok": False, "underpaid": True, "error": f"underpaid: received {value} nanoGRAM, the invoice needs "
                    f"{int(row['amount_nano'])}. Send transfers with the network fee paid separately (send mode 1). What "
                    "arrived is credited to an account for your wallet: use api_key as Bearer, or request a new invoice.",
                    "api_key": api_key, "credit_nano": value, "tx_hash": tx_hash, "payer": payer}
        return {"ok": False, "error": "invoice closed as underpaid; the amount was credited to the api_key returned then"}
    if row["status"] == "underpaid":
        return {"ok": False, "error": "invoice closed as underpaid; the amount was credited to the api_key returned then"}
    if not found:
        if now > row["expires"]:
            return {"ok": False, "error": "invoice expired and no payment found", "expired": True}
        return {"ok": False, "error": "payment not found on chain yet; retry in a few seconds", "retry": True}
    tx_hash, payer, value = found
    if c.execute("SELECT 1 FROM pay_txs WHERE tx_hash=?", (tx_hash,)).fetchone():
        return {"ok": False, "error": "this transaction already settled another invoice"}
    cur = c.execute("UPDATE pay_invoices SET status='paid' WHERE id=? AND status='open'", (claim["invoice"],))
    if cur.rowcount != 1:
        return {"ok": False, "error": "invoice already settled"}
    c.execute("INSERT INTO pay_txs VALUES(?,?,?)", (tx_hash, claim["invoice"], now))
    api_key, key_hash = new_key_fn(c, payer, value, tx_hash)
    c.execute("UPDATE pay_invoices SET tx_hash=?, payer=?, paid_nano=?, key_hash=? WHERE id=?",
              (tx_hash, payer, value, key_hash, claim["invoice"]))
    return {"ok": True, "api_key": api_key, "key_hash": key_hash, "tx_hash": tx_hash, "payer": payer,
            "paid_nano": value, "network": network_id()}


def response_header(settlement, charged_nano, balance_nano):
    d = {"success": True, "scheme": SCHEME, "network": settlement["network"], "transaction": settlement["tx_hash"],
         "payer": settlement["payer"], "paid_nano": settlement["paid_nano"], "charged_nano": charged_nano,
         "credit_left_nano": balance_nano, "api_key": settlement["api_key"]}
    return base64.b64encode(json.dumps(d, separators=(",", ":")).encode()).decode()
