"""Offline tests of the PayPal mandate rail: a fake PayPal API (httpx.MockTransport), built-in echo model.
Run: python3 test_paypal.py"""
import base64
import json
import os
import tempfile

os.environ.update({"GRAM402_DB": tempfile.mktemp(suffix=".db"), "PAY402_NETWORK": "testnet",
                   "PAY402_ADDRESS": "0QDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVQ_U",
                   "PAYPAL_CLIENT_ID": "test-id", "PAYPAL_CLIENT_SECRET": "test-secret",
                   "PAYPAL_PRICE_CENTS": "10", "PUBLIC_URL": "https://demo.example"})

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402
import paypal402  # noqa: E402
import receipts  # noqa: E402

STATE = {"orders": 0, "fail_next": False, "calls": []}


def fake_paypal(req: httpx.Request):
    path = req.url.path
    STATE["calls"].append((req.method, path))
    if path == "/v1/oauth2/token":
        assert req.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    assert req.headers.get("authorization") == "Bearer tok"
    if path == "/v3/vault/setup-tokens":
        b = json.loads(req.content)
        assert b["payment_source"]["paypal"]["usage_type"] == "MERCHANT"
        return httpx.Response(201, json={"id": "ST1", "status": "PAYER_ACTION_REQUIRED",
                                         "links": [{"rel": "approve", "href": "https://sandbox.paypal.com/approve?t=ST1"}]})
    if path == "/v3/vault/payment-tokens":
        return httpx.Response(201, json={"id": "VAULT1", "payment_source": {"paypal": {"email_address": "o@example.com"}}})
    if path == "/v2/checkout/orders":
        if STATE["fail_next"]:
            STATE["fail_next"] = False
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"})
        b = json.loads(req.content)
        assert b["payment_source"]["paypal"]["vault_id"] == "VAULT1"
        assert req.headers.get("paypal-request-id", "").startswith("order-")
        STATE["orders"] += 1
        return httpx.Response(201, json={"id": f"O{STATE['orders']}", "status": "COMPLETED", "purchase_units": [
            {"payments": {"captures": [{"id": f"CAP{STATE['orders']}", "status": "COMPLETED"}]}}]})
    return httpx.Response(404, json={"path": path})


paypal402._transport = httpx.MockTransport(fake_paypal)
cl = TestClient(A.app)
REQ = {"model": "echo", "max_tokens": 32, "messages": [{"role": "user", "content": "price of GRAM?"}]}


def xpay(mandate, secret, invoice, scheme="paypal-mandate"):
    d = {"x402Version": 1, "scheme": scheme, "payload": {"mandate": mandate, "secret": secret, "invoice": invoice}}
    return base64.b64encode(json.dumps(d).encode()).decode()


def invoice():
    r = cl.post("/v1/chat/completions", json=REQ)
    assert r.status_code == 402, r.text
    acc = {a["scheme"]: a for a in r.json()["accepts"]}
    assert set(acc) == {"ton-transfer", "paypal-mandate"}, acc
    assert acc["paypal-mandate"]["maxAmountRequired"] == "10" and acc["paypal-mandate"]["asset"] == "USD"
    return acc["paypal-mandate"]["extra"]["invoice"]


# 1. Both rails are offered in one 402.
inv = invoice()

# 2. Mandate: created pending, unusable until the owner approves.
m = cl.post("/v1/mandates", json={"budget_cents": 25, "per_request_cents": 10, "days": 7,
                                  "resources": ["/v1/chat/completions"]}).json()
assert m["approve_url"].startswith("https://sandbox.paypal.com/") and m["secret"]
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m["mandate"], m["secret"], inv)})
assert r.status_code == 402 and "approve" in r.json()["error"], r.text

# 3. Owner approves -> active.
assert cl.get(f"/v1/mandates/{m['mandate']}/approved").json()["status"] == "active"

# 4. Wrong secret is rejected and charges nothing.
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m["mandate"], "nope", inv)})
assert r.status_code == 402 and STATE["orders"] == 0

# 5. Paid request: answer + receipt bound to the PayPal capture, verifiable.
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m["mandate"], m["secret"], inv)})
assert r.status_code == 200, r.text
body = r.json()
g = body["gram402"]
assert g["payment"]["transaction"] == "CAP1" and g["charged_cents"] == 10 and g["mandate_left_cents"] == 15
assert g["receipt"]["payment"]["transaction"] == "CAP1" and g["receipt"]["payment"]["charged_cents"] == 10
assert receipts.verify(g["receipt"], REQ, body)
hdr = json.loads(base64.b64decode(r.headers["X-PAYMENT-RESPONSE"]))
assert hdr["scheme"] == "paypal-mandate" and hdr["transaction"] == "CAP1"

# 6. The same invoice cannot be paid twice.
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m["mandate"], m["secret"], inv)})
assert r.status_code == 402 and STATE["orders"] == 1

# 7. A PayPal failure releases the reservation; the invoice can be retried.
inv2 = invoice()
STATE["fail_next"] = True
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m["mandate"], m["secret"], inv2)})
assert r.status_code == 402 and "PayPal payment failed" in r.json()["error"]
st = cl.get(f"/v1/mandates/{m['mandate']}", headers={"X-Mandate-Secret": m["secret"]}).json()
assert st["spent_cents"] == 10, st
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m["mandate"], m["secret"], inv2)})
assert r.status_code == 200 and r.json()["gram402"]["mandate_left_cents"] == 5

# 8. Budget: 5 cents left, price 10 -> refused without calling PayPal.
inv3 = invoice()
before = STATE["orders"]
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m["mandate"], m["secret"], inv3)})
assert r.status_code == 402 and "budget" in r.json()["error"] and STATE["orders"] == before

# 9. Per-request cap and resource allowlist.
m2 = cl.post("/v1/mandates", json={"budget_cents": 100, "per_request_cents": 5, "days": 1}).json()
cl.get(f"/v1/mandates/{m2['mandate']}/approved")
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m2["mandate"], m2["secret"], inv3)})
assert r.status_code == 402 and "per-request cap" in r.json()["error"]
with A.db() as c:
    c.execute("UPDATE pp_invoices SET resource='/v1/other' WHERE id=?", (inv3,))
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m["mandate"], m["secret"], inv3)})
assert r.status_code == 402 and "not allowed" in r.json()["error"]

# 10. Revoke stops spending; the status endpoint needs the secret.
assert cl.get(f"/v1/mandates/{m2['mandate']}").status_code == 404
assert cl.delete(f"/v1/mandates/{m2['mandate']}", headers={"X-Mandate-Secret": m2["secret"]}).json()["status"] == "revoked"
inv4 = invoice()
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m2["mandate"], m2["secret"], inv4)})
assert r.status_code == 402 and "revoked" in r.json()["error"]

# 11. Secrets are not stored in clear.
with A.db() as c:
    row = c.execute("SELECT * FROM pp_mandates WHERE id=?", (m["mandate"],)).fetchone()
    assert row["secret_hash"] != m["secret"] and m["secret"] not in json.dumps(dict(row))

print("paypal rail: all tests passed;", STATE["orders"], "captures,", len(STATE["calls"]), "PayPal calls")
