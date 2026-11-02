"""PayPal rail for GRAM-402: AI agents pay HTTP 402 invoices from a PayPal spending mandate.

A human approves a mandate once (PayPal Vault v3, buyer present). The mandate holds limits: total budget, a cap per
request, an expiry and an optional list of allowed resources. After that the agent pays 402 invoices on its own: the
server charges the vaulted PayPal account (Orders v2, buyer not present) within the limits and returns the answer
with a signed receipt bound to the PayPal capture. The human can revoke the mandate at any time.

Scheme "paypal-mandate" sits next to "ton-transfer" in the same x402 `accepts` list, so one endpoint sells to agents
that hold GRAM and to agents whose owner pays with PayPal.

Mandate flow:
  POST /v1/mandates {budget_cents, per_request_cents, days, resources?}
      -> {mandate, secret, approve_url}            (the agent keeps the secret; the human opens approve_url)
  human approves on PayPal -> PayPal redirects to /v1/mandates/{mandate}/approved -> vault token stored, status active
  agent: request without auth -> 402 accepts [ton-transfer, paypal-mandate]
  agent: repeat with X-PAYMENT = base64 JSON {x402Version:1, scheme:"paypal-mandate", payload:{mandate, secret, invoice}}
      -> limits checked, PayPal order captured, answer + receipt {network:"paypal-<env>", transaction:<capture id>}

Secrets: only hashes of mandate secrets are stored; the PayPal vault id never leaves the server.
Author: Aleksandr Shcherbakov (@miinimal) - MINIMALCULT.
"""
import base64
import hashlib
import json
import os
import secrets
import time

import httpx

ENV = os.environ.get
CLIENT_ID = ENV("PAYPAL_CLIENT_ID", "")
CLIENT_SECRET = ENV("PAYPAL_CLIENT_SECRET", "")
PP_ENV = ENV("PAYPAL_ENV", "sandbox")
BASE = {"sandbox": "https://api-m.sandbox.paypal.com", "live": "https://api-m.paypal.com"}
PUBLIC_URL = ENV("PUBLIC_URL", "http://localhost:8402").rstrip("/")
PRICE_CENTS = int(ENV("PAYPAL_PRICE_CENTS", "5"))
CURRENCY = ENV("PAYPAL_CURRENCY", "USD")
TTL = int(ENV("PAY402_TTL_S", "1800"))
SCHEME = "paypal-mandate"
MAX_BUDGET_CENTS = int(ENV("PAYPAL_MAX_BUDGET_CENTS", "50000"))

_transport = None  # tests inject httpx.MockTransport
_token = {"value": None, "exp": 0}


def enabled():
    return bool(CLIENT_ID and CLIENT_SECRET)


def network_id():
    return "paypal-" + PP_ENV


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _money(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def setup_db(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS pp_mandates(id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, created INTEGER,
        expires INTEGER, budget_cents INTEGER, per_request_cents INTEGER, spent_cents INTEGER NOT NULL DEFAULT 0,
        resources TEXT, status TEXT NOT NULL DEFAULT 'pending', setup_token TEXT, vault_id TEXT, payer TEXT);
    CREATE TABLE IF NOT EXISTS pp_invoices(id TEXT PRIMARY KEY, mandate TEXT, created INTEGER, expires INTEGER,
        amount_cents INTEGER, resource TEXT, status TEXT NOT NULL DEFAULT 'open', capture_id TEXT);
    """)


def _client():
    return httpx.AsyncClient(base_url=BASE[PP_ENV], timeout=30, transport=_transport)


async def _auth(cl):
    if _token["value"] and _token["exp"] > time.time() + 60:
        return _token["value"]
    r = await cl.post("/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=(CLIENT_ID, CLIENT_SECRET))
    r.raise_for_status()
    d = r.json()
    _token.update(value=d["access_token"], exp=time.time() + int(d.get("expires_in", 300)))
    return _token["value"]


async def _call(method, path, body=None, request_id=None):
    async with _client() as cl:
        headers = {"Authorization": "Bearer " + await _auth(cl), "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        r = await cl.request(method, path, json=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"paypal {path} {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}


async def create_mandate(c, budget_cents, per_request_cents, days, resources=None, now=None):
    """Create a pending mandate and a PayPal setup token; returns the secret once and the approval link."""
    now = now or int(time.time())
    budget_cents = max(1, min(int(budget_cents), MAX_BUDGET_CENTS))
    per_request_cents = max(1, min(int(per_request_cents), budget_cents))
    mid, sec = "m_" + secrets.token_hex(8), secrets.token_urlsafe(24)
    st = await _call("POST", "/v3/vault/setup-tokens", {"payment_source": {"paypal": {
        "usage_type": "MERCHANT", "usage_pattern": "UNSCHEDULED_POSTPAID",
        "description": f"AI agent spending mandate {mid}: up to {_money(budget_cents)} {CURRENCY}",
        "experience_context": {"return_url": f"{PUBLIC_URL}/v1/mandates/{mid}/approved",
                               "cancel_url": f"{PUBLIC_URL}/v1/mandates/{mid}/cancelled",
                               "brand_name": ENV("PAYPAL_BRAND", "GRAM-402"), "shipping_preference": "NO_SHIPPING"}}}},
        request_id="setup-" + mid)
    approve = next((l["href"] for l in st.get("links", []) if l.get("rel") == "approve"), None)
    c.execute("INSERT INTO pp_mandates(id,secret_hash,created,expires,budget_cents,per_request_cents,resources,"
              "setup_token) VALUES(?,?,?,?,?,?,?,?)",
              (mid, _h(sec), now, now + int(days) * 86400, budget_cents, per_request_cents,
               json.dumps(resources or []), st["id"]))
    return {"mandate": mid, "secret": sec, "approve_url": approve, "budget_cents": budget_cents,
            "per_request_cents": per_request_cents, "expires": now + int(days) * 86400, "currency": CURRENCY}


async def activate(c, mid):
    """Called on the PayPal return URL: turn the approved setup token into a vault payment token."""
    row = c.execute("SELECT * FROM pp_mandates WHERE id=?", (mid,)).fetchone()
    if not row:
        return {"ok": False, "error": "unknown mandate"}
    if row["status"] == "active":
        return {"ok": True, "status": "active"}
    if row["status"] != "pending":
        return {"ok": False, "error": "mandate is " + row["status"]}
    pt = await _call("POST", "/v3/vault/payment-tokens",
                     {"payment_source": {"token": {"id": row["setup_token"], "type": "SETUP_TOKEN"}}},
                     request_id="vault-" + mid)
    payer = ((pt.get("payment_source") or {}).get("paypal") or {}).get("email_address", "")
    c.execute("UPDATE pp_mandates SET status='active', vault_id=?, payer=? WHERE id=? AND status='pending'",
              (pt["id"], _h(payer) if payer else "", mid))
    return {"ok": True, "status": "active"}


def status(c, mid, secret):
    row = c.execute("SELECT * FROM pp_mandates WHERE id=?", (mid,)).fetchone()
    if not row or not secrets.compare_digest(row["secret_hash"], _h(secret or "")):
        return None
    return {"mandate": mid, "status": row["status"], "budget_cents": row["budget_cents"],
            "spent_cents": row["spent_cents"], "left_cents": row["budget_cents"] - row["spent_cents"],
            "per_request_cents": row["per_request_cents"], "expires": row["expires"],
            "resources": json.loads(row["resources"] or "[]"), "currency": CURRENCY}


def revoke(c, mid, secret):
    if not status(c, mid, secret):
        return False
    c.execute("UPDATE pp_mandates SET status='revoked' WHERE id=?", (mid,))
    return True


def create_invoice(c, amount_cents, resource, now=None):
    now = now or int(time.time())
    inv = "p_" + secrets.token_hex(8)
    c.execute("INSERT INTO pp_invoices(id,created,expires,amount_cents,resource) VALUES(?,?,?,?,?)",
              (inv, now, now + TTL, amount_cents, resource))
    return {"invoice": inv, "amount_cents": amount_cents, "expires": now + TTL}


def accept(inv, resource):
    """One entry for the x402 `accepts` list."""
    return {"scheme": SCHEME, "network": network_id(), "asset": CURRENCY, "maxAmountRequired": str(inv["amount_cents"]),
            "payTo": "merchant", "resource": resource, "mimeType": "application/json", "maxTimeoutSeconds": TTL,
            "description": "Pay from a PayPal spending mandate approved once by the agent's owner.",
            "extra": {"invoice": inv["invoice"], "decimals": 2, "expiresAt": inv["expires"],
                      "mandateUrl": f"{PUBLIC_URL}/v1/mandates",
                      "howTo": "repeat the request with header X-PAYMENT = base64 JSON {x402Version:1, scheme:"
                               "'paypal-mandate', payload:{mandate, secret, invoice}}"}}


def parse_header(value):
    raw = value.strip()
    try:
        d = json.loads(raw if raw.startswith("{") else base64.b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:  # noqa: BLE001
        return None
    if d.get("scheme") != SCHEME:
        return None
    p = d.get("payload") or {}
    if not (p.get("mandate") and p.get("secret") and p.get("invoice")):
        return None
    return {"mandate": str(p["mandate"])[:40], "secret": str(p["secret"])[:128], "invoice": str(p["invoice"])[:40]}


def _check(c, claim, now):
    m = c.execute("SELECT * FROM pp_mandates WHERE id=?", (claim["mandate"],)).fetchone()
    if not m or not secrets.compare_digest(m["secret_hash"], _h(claim["secret"])):
        return None, None, "unknown mandate or wrong secret"
    if m["status"] != "active":
        return None, None, "mandate is " + m["status"] + ("; the owner must approve it first" if m["status"] == "pending" else "")
    if now > m["expires"]:
        return None, None, "mandate expired"
    inv = c.execute("SELECT * FROM pp_invoices WHERE id=?", (claim["invoice"],)).fetchone()
    if not inv or inv["status"] != "open":
        return None, None, "unknown or already used invoice"
    if now > inv["expires"]:
        return None, None, "invoice expired"
    allowed = json.loads(m["resources"] or "[]")
    if allowed and inv["resource"] not in allowed:
        return None, None, "resource not allowed by this mandate"
    if inv["amount_cents"] > m["per_request_cents"]:
        return None, None, "price exceeds the mandate's per-request cap"
    if m["spent_cents"] + inv["amount_cents"] > m["budget_cents"]:
        return None, None, "mandate budget exhausted"
    return m, inv, None


async def settle(c, claim, now=None):
    """Check limits, reserve the amount, capture on PayPal; on failure release the reservation."""
    now = now or int(time.time())
    m, inv, err = _check(c, claim, now)
    if err:
        return {"ok": False, "error": err}
    cur = c.execute("UPDATE pp_mandates SET spent_cents=spent_cents+? WHERE id=? AND spent_cents+?<=budget_cents",
                    (inv["amount_cents"], m["id"], inv["amount_cents"]))
    cur2 = c.execute("UPDATE pp_invoices SET status='paying', mandate=? WHERE id=? AND status='open'",
                     (m["id"], inv["id"]))
    if cur.rowcount != 1 or cur2.rowcount != 1:
        c.rollback()
        return {"ok": False, "error": "concurrent payment; retry"}
    c.commit()
    try:
        order = await _call("POST", "/v2/checkout/orders", {
            "intent": "CAPTURE",
            "purchase_units": [{"amount": {"currency_code": CURRENCY, "value": _money(inv["amount_cents"])},
                                "invoice_id": inv["id"], "custom_id": m["id"],
                                "description": ("AI request " + inv["resource"])[:127]}],
            "payment_source": {"paypal": {"vault_id": m["vault_id"],
                                          "stored_credential": {"payment_initiator": "MERCHANT",
                                                                "usage": "SUBSEQUENT",
                                                                "usage_pattern": "UNSCHEDULED_POSTPAID"}}}},
            request_id="order-" + inv["id"])
        if order.get("status") != "COMPLETED":
            order = await _call("POST", f"/v2/checkout/orders/{order['id']}/capture", {},
                                request_id="capture-" + inv["id"])
        cap = order["purchase_units"][0]["payments"]["captures"][0]
        if order.get("status") != "COMPLETED" or cap.get("status") not in ("COMPLETED", "PENDING"):
            raise RuntimeError("capture status " + str(cap.get("status")))
    except Exception as e:  # noqa: BLE001
        c.execute("UPDATE pp_mandates SET spent_cents=spent_cents-? WHERE id=?", (inv["amount_cents"], m["id"]))
        c.execute("UPDATE pp_invoices SET status='open' WHERE id=?", (inv["id"],))
        c.commit()
        return {"ok": False, "error": "PayPal payment failed: " + str(e)[:200]}
    c.execute("UPDATE pp_invoices SET status='paid', capture_id=? WHERE id=?", (cap["id"], inv["id"]))
    c.commit()
    left = m["budget_cents"] - m["spent_cents"] - inv["amount_cents"]
    return {"ok": True, "network": network_id(), "transaction": cap["id"], "payer": m["id"],
            "charged_cents": inv["amount_cents"], "mandate_left_cents": left}


def response_header(s):
    d = {"success": True, "scheme": SCHEME, "network": s["network"], "transaction": s["transaction"],
         "payer": s["payer"], "charged_cents": s["charged_cents"], "mandate_left_cents": s["mandate_left_cents"]}
    return base64.b64encode(json.dumps(d, separators=(",", ":")).encode()).decode()
