"""NEAR rail for GRAM-402: agents pay 402 invoices through an on-chain mandate (near/contract).

Scheme "near-mandate": the 402 offer names the mandate contract, the merchant account, the amount in yoctoNEAR and an
invoice id. The agent calls `pay(mandate_id, merchant, amount, invoice, resource)` on the contract with its own
NEAR account (ideally a function-call key limited to that method). The contract enforces the owner's limits and sends
the money to the merchant. The agent then repeats the request with X-PAYMENT = base64 JSON {x402Version:1,
scheme:"near-mandate", payload:{invoice, secret}}; the server reads `get_payment(invoice)` from the contract (view
call, final finality) and checks merchant, amount and resource before answering.

The invoice id is public on chain, so the private secret proves the claimant is the one who asked for the invoice
(front-running guard, same as ton-transfer). Author: Aleksandr Shcherbakov (@miinimal).
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
        amount_yocto TEXT, resource TEXT, status TEXT NOT NULL DEFAULT 'open', mandate_id INTEGER);
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
                      "args": {"merchant": MERCHANT, "amount": str(inv["amount_yocto"]), "invoice": inv["invoice"],
                               "resource": resource},
                      "howTo": "call pay(mandate_id, merchant, amount, invoice, resource) on the contract, then repeat "
                               "the request with X-PAYMENT = base64 JSON {x402Version:1, scheme:'near-mandate', "
                               "payload:{invoice, secret}}"}}


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
    return {"invoice": str(p["invoice"])[:64], "secret": str(p["secret"])[:128]}


async def view(method, args):
    body = {"jsonrpc": "2.0", "id": "g402", "method": "query", "params": {
        "request_type": "call_function", "finality": "final", "account_id": CONTRACT, "method_name": method,
        "args_base64": base64.b64encode(json.dumps(args).encode()).decode()}}
    async with httpx.AsyncClient(timeout=20, transport=_transport) as cl:
        r = await cl.post(RPC, json=body)
        r.raise_for_status()
        d = r.json()
    if "error" in d or "error" in (d.get("result") or {}):
        raise RuntimeError("near rpc: " + json.dumps(d.get("error") or d["result"].get("error"))[:200])
    return json.loads(bytes(d["result"]["result"]).decode() or "null")


async def settle(c, claim, now=None):
    now = now or int(time.time())
    row = c.execute("SELECT * FROM near_invoices WHERE id=?", (claim["invoice"],)).fetchone()
    if not row or not secrets.compare_digest(row["secret_hash"], _h(claim["secret"])):
        return {"ok": False, "error": "unknown invoice or wrong secret"}
    if row["status"] != "open":
        return {"ok": False, "error": "invoice already settled"}
    try:
        p = await view("get_payment", {"invoice": claim["invoice"]})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "NEAR RPC unavailable: " + str(e)[:120], "retry": True}
    if not p:
        if now > row["expires"]:
            return {"ok": False, "error": "invoice expired and no payment found"}
        return {"ok": False, "error": "payment not found on chain yet; retry in a few seconds", "retry": True}
    if p.get("merchant") != MERCHANT:
        return {"ok": False, "error": "paid to another merchant"}
    if int(p.get("amount", 0)) < int(row["amount_yocto"]):
        return {"ok": False, "error": "underpaid"}
    if (p.get("resource") or "") not in ("", row["resource"]):
        return {"ok": False, "error": "payment is for another resource"}
    cur = c.execute("UPDATE near_invoices SET status='paid', mandate_id=? WHERE id=? AND status='open'",
                    (int(p.get("mandate_id", 0)), claim["invoice"]))
    if cur.rowcount != 1:
        return {"ok": False, "error": "invoice already settled"}
    return {"ok": True, "network": network_id(), "transaction": f"{CONTRACT}/{claim['invoice']}",
            "payer": f"mandate:{p.get('mandate_id')}", "charged_yocto": int(p["amount"])}


def response_header(s):
    d = {"success": True, "scheme": SCHEME, "network": s["network"], "transaction": s["transaction"],
         "payer": s["payer"], "charged_yocto": str(s["charged_yocto"])}
    return base64.b64encode(json.dumps(d, separators=(",", ":")).encode()).decode()
