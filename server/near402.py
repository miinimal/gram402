"""NEAR rail for GRAM-402: agents pay 402 invoices through an on-chain mandate (near/contract).

Scheme "near-mandate": the 402 offer names the mandate contract, the merchant account, the amount in yoctoNEAR and an
invoice id. The agent calls `pay(mandate_id, merchant, amount, invoice)` on the contract with its own NEAR account
(ideally a function-call key limited to that method). The contract enforces the owner's limits, sends the money to
the merchant and logs a `paid` event. The agent repeats the request with X-PAYMENT = base64 JSON {x402Version:1,
scheme:"near-mandate", payload:{invoice, secret, tx_hash, sender}}; the server fetches that transaction (final) and
requires a `paid` event emitted by the mandate contract for this invoice, this merchant and at least the price, with
no failed receipt. A transaction settles at most one invoice.

The invoice id is public, so the private secret proves the claimant asked for the invoice (front-running guard).
If the answer fails after payment, the same X-PAYMENT is honoured once more without paying again.
Author: Aleksandr Shcherbakov (@miinimal).
"""
import base64
import hashlib
import json
import os
import secrets
import time

import httpx

ENV = os.environ.get
CONTRACT = ENV("NEAR_MANDATE_CONTRACT", "")
MERCHANT = ENV("NEAR_MERCHANT", "")
NETWORK = ENV("NEAR_NETWORK", "testnet")
RPC = ENV("NEAR_RPC", {"testnet": "https://rpc.testnet.near.org", "mainnet": "https://rpc.mainnet.near.org"}[NETWORK])
PRICE_YOCTO = int(ENV("NEAR_PRICE_YOCTO", str(10**21)))  # 0.001 NEAR
TTL = int(ENV("PAY402_TTL_S", "1800"))
SCHEME = "near-mandate"

_transport = None  # tests inject httpx.MockTransport


def enabled():
    return bool(CONTRACT and MERCHANT)


def network_id():
    return "near-" + NETWORK


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def setup_db(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS near_invoices(id TEXT PRIMARY KEY, secret_hash TEXT, created INTEGER, expires INTEGER,
        amount_yocto TEXT, resource TEXT, status TEXT NOT NULL DEFAULT 'open', mandate_id INTEGER, tx_hash TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS near_invoices_tx ON near_invoices(tx_hash);
    """)


def create_invoice(c, amount_yocto, resource, now=None):
    now = now or int(time.time())
    inv, sec = "n_" + secrets.token_hex(8), secrets.token_urlsafe(24)
    if secrets.randbelow(50) == 0:
        c.execute("DELETE FROM near_invoices WHERE status='open' AND expires<?", (now - TTL,))
    c.execute("INSERT INTO near_invoices(id,secret_hash,created,expires,amount_yocto,resource) VALUES(?,?,?,?,?,?)",
              (inv, _h(sec), now, now + TTL, str(amount_yocto), resource))
    return {"invoice": inv, "secret": sec, "amount_yocto": int(amount_yocto), "expires": now + TTL}


def accept(inv, resource):
    return {"scheme": SCHEME, "network": network_id(), "asset": "NEAR", "maxAmountRequired": str(inv["amount_yocto"]),
            "payTo": MERCHANT, "resource": resource, "mimeType": "application/json", "maxTimeoutSeconds": TTL,
            "description": "Pay through your NEAR spending mandate; limits are enforced by the contract.",
            "extra": {"invoice": inv["invoice"], "secret": inv["secret"], "contract": CONTRACT, "method": "pay",
                      "decimals": 24, "expiresAt": inv["expires"],
                      "args": {"merchant": MERCHANT, "amount": str(inv["amount_yocto"]), "invoice": inv["invoice"]},
                      "howTo": "call pay(mandate_id, merchant, amount, invoice) on the contract, then repeat the "
                               "request with X-PAYMENT = base64 JSON {x402Version:1, scheme:'near-mandate', "
                               "payload:{invoice, secret, tx_hash, sender}}"}}


def parse_header(value):
    raw = value.strip()
    try:
        d = json.loads(raw if raw.startswith("{") else base64.b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:  # noqa: BLE001
        return None
    if d.get("scheme") != SCHEME:
        return None
    p = d.get("payload") or {}
    if not (p.get("invoice") and p.get("secret")):
        return None
    return {"invoice": str(p["invoice"])[:64], "secret": str(p["secret"])[:128],
            "tx_hash": str(p.get("tx_hash") or "")[:64], "sender": str(p.get("sender") or "")[:64]}


async def rpc(method, params):
    async with httpx.AsyncClient(timeout=30, transport=_transport) as cl:
        r = await cl.post(RPC, json={"jsonrpc": "2.0", "id": "g402", "method": method, "params": params})
        r.raise_for_status()
        d = r.json()
    if "error" in d:
        raise LookupError(json.dumps(d["error"])[:200])
    return d["result"]


def _events(outcome):
    for line in outcome.get("logs") or []:
        if line.startswith("EVENT_JSON:"):
            try:
                ev = json.loads(line[len("EVENT_JSON:"):])
            except ValueError:
                continue
            if ev.get("standard") == "near-mandate":
                yield ev


def verify_tx(tx, invoice, amount_yocto):
    """The transaction must hold a `paid` event from the mandate contract for this invoice and merchant, at least the
    price, and no failed receipt (a failed transfer to the merchant is a failed receipt)."""
    outcomes = tx.get("receipts_outcome") or []
    if "Failure" in (tx.get("status") or {}) or any("Failure" in ((o.get("outcome") or {}).get("status") or {})
                                                     for o in outcomes):
        return "the payment transaction failed"
    for o in outcomes:
        out = o.get("outcome") or {}
        if out.get("executor_id") != CONTRACT:
            continue
        for ev in _events(out):
            if ev.get("event") == "pay_failed":
                return "the transfer to the merchant failed"
            if ev.get("event") != "paid":
                continue
            for d in ev.get("data") or []:
                if d.get("invoice") == invoice and d.get("merchant") == MERCHANT:
                    if int(d.get("amount", 0)) < int(amount_yocto):
                        return "underpaid"
                    return {"mandate_id": int(d.get("mandate_id", 0)), "amount": int(d["amount"])}
    return "no payment for this invoice to this merchant in that transaction"


async def settle(c, claim, now=None):
    now = now or int(time.time())
    row = c.execute("SELECT * FROM near_invoices WHERE id=?", (claim["invoice"],)).fetchone()
    if not row or not secrets.compare_digest(row["secret_hash"], _h(claim["secret"])):
        return {"ok": False, "error": "unknown invoice or wrong secret"}
    if row["status"] == "credit":  # paid earlier, answer failed: serve once more without a new payment
        c.execute("UPDATE near_invoices SET status='paid' WHERE id=? AND status='credit'", (row["id"],))
        return {"ok": True, "network": network_id(), "transaction": row["tx_hash"],
                "payer": f"mandate:{row['mandate_id']}", "charged_yocto": int(row["amount_yocto"]), "invoice": row["id"]}
    if row["status"] != "open":
        return {"ok": False, "error": "invoice already settled"}
    if not claim.get("tx_hash") or not claim.get("sender"):
        return {"ok": False, "error": "X-PAYMENT must carry tx_hash and sender of the pay() transaction"}
    if c.execute("SELECT 1 FROM near_invoices WHERE tx_hash=?", (claim["tx_hash"],)).fetchone():
        return {"ok": False, "error": "this transaction already settled another invoice"}
    try:
        tx = await rpc("tx", {"tx_hash": claim["tx_hash"], "sender_account_id": claim["sender"], "wait_until": "FINAL"})
    except LookupError as e:
        if now > row["expires"]:
            return {"ok": False, "error": "invoice expired and the transaction was not found"}
        return {"ok": False, "error": "transaction not found yet: " + str(e)[:100], "retry": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "NEAR RPC unavailable: " + str(e)[:120], "retry": True}
    res = verify_tx(tx, claim["invoice"], row["amount_yocto"])
    if isinstance(res, str):
        return {"ok": False, "error": res}
    try:
        cur = c.execute("UPDATE near_invoices SET status='paid', mandate_id=?, tx_hash=? WHERE id=? AND status='open'",
                        (res["mandate_id"], claim["tx_hash"], claim["invoice"]))
    except Exception:  # noqa: BLE001  (unique tx_hash: a concurrent claim used the same transaction)
        return {"ok": False, "error": "this transaction already settled another invoice"}
    if cur.rowcount != 1:
        return {"ok": False, "error": "invoice already settled"}
    return {"ok": True, "network": network_id(), "transaction": claim["tx_hash"], "payer": f"mandate:{res['mandate_id']}",
            "charged_yocto": res["amount"], "invoice": claim["invoice"]}


def credit(c, invoice):
    """The answer failed after a verified payment: the same X-PAYMENT will be served once more."""
    c.execute("UPDATE near_invoices SET status='credit' WHERE id=? AND status='paid'", (invoice,))


def response_header(s):
    d = {"success": True, "scheme": SCHEME, "network": s["network"], "transaction": s["transaction"],
         "payer": s["payer"], "charged_yocto": str(s["charged_yocto"])}
    return base64.b64encode(json.dumps(d, separators=(",", ":")).encode()).decode()
