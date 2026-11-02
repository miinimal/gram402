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

from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402
import paypal402  # noqa: E402
import receipts  # noqa: E402

import fake_paypal  # noqa: E402

STATE = fake_paypal.STATE
paypal402._transport = fake_paypal.transport()
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
assert r.status_code == 402 and "PayPal refused" in r.json()["error"]
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
assert r.status_code == 402 and ("not allowed" in r.json()["error"] or "another resource" in r.json()["error"])

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

# 13. PayPal 5xx after capturing: reservation kept, retry with the same X-PAYMENT finishes it, charged once.
m5 = cl.post("/v1/mandates", json={"budget_cents": 50, "per_request_cents": 10, "days": 1}).json()
cl.get(f"/v1/mandates/{m5['mandate']}/approved")
inv5 = invoice()
STATE["error_next"] = True
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m5["mandate"], m5["secret"], inv5)})
assert r.status_code == 402 and r.headers.get("retry-after") == "5" and "unknown" in r.json()["error"], r.text
assert cl.get(f"/v1/mandates/{m5['mandate']}", headers={"X-Mandate-Secret": m5["secret"]}).json()["spent_cents"] == 10
orders_before = STATE["orders"]
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m5["mandate"], m5["secret"], inv5)})
assert r.status_code == 200 and STATE["orders"] == orders_before, r.text
assert cl.get(f"/v1/mandates/{m5['mandate']}", headers={"X-Mandate-Secret": m5["secret"]}).json()["spent_cents"] == 10

# 14. Another mandate cannot take over an invoice that is mid-payment.
m6 = cl.post("/v1/mandates", json={"budget_cents": 50, "per_request_cents": 10, "days": 1}).json()
cl.get(f"/v1/mandates/{m6['mandate']}/approved")
inv6 = invoice()
STATE["error_next"] = True
cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m5["mandate"], m5["secret"], inv6)})
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m6["mandate"], m6["secret"], inv6)})
assert r.status_code == 402 and "already used" in r.json()["error"]

# 15. Paid but the answer failed: refunded and the budget given back.
inv7 = invoice()
real_answer = A.answer


async def broken(body):
    raise A.HTTPException(502, "upstream down")
A.answer = broken
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m6["mandate"], m6["secret"], inv7)})
A.answer = real_answer
assert r.status_code == 502 and r.json()["refund"]["refunded"] and STATE["refunds"], r.text
assert cl.get(f"/v1/mandates/{m6['mandate']}", headers={"X-Mandate-Secret": m6["secret"]}).json()["spent_cents"] == 0

# 16. Five parallel retries of one payment whose outcome was unknown: exactly one answer, one charge.
import asyncio  # noqa: E402
import httpx as _hx  # noqa: E402
m8 = cl.post("/v1/mandates", json={"budget_cents": 100, "per_request_cents": 10, "days": 1}).json()
cl.get(f"/v1/mandates/{m8['mandate']}/approved")
inv8 = invoice()
STATE["error_next"] = True
assert cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m8["mandate"], m8["secret"], inv8)}).status_code == 402


async def burst():
    async with _hx.AsyncClient(transport=_hx.ASGITransport(app=A.app), base_url="http://t") as ac:
        rs = await asyncio.gather(*[ac.post("/v1/chat/completions", json=REQ,
                                            headers={"X-PAYMENT": xpay(m8["mandate"], m8["secret"], inv8)}) for _ in range(5)])
    return [r.status_code for r in rs]
codes = asyncio.run(burst())
assert codes.count(200) == 1, codes
assert cl.get(f"/v1/mandates/{m8['mandate']}", headers={"X-Mandate-Secret": m8["secret"]}).json()["spent_cents"] == 10

# 17. A refund runs once even if asked twice; the budget is given back once.
with A.db() as c:
    st = {"transaction": c.execute("SELECT capture_id FROM pp_invoices WHERE id=?", (inv8,)).fetchone()[0],
          "charged_cents": 10, "payer": m8["mandate"]}
    r1 = asyncio.run(paypal402.refund(c, st))
    r2 = asyncio.run(paypal402.refund(c, st))
assert r1["refunded"] and not r2["refunded"]
assert cl.get(f"/v1/mandates/{m8['mandate']}", headers={"X-Mandate-Secret": m8["secret"]}).json()["spent_cents"] == 0

# 18. Upstream timeout (not an HTTPException): refunded.
inv9 = invoice()
real_answer = A.answer


async def timeout(body):
    raise _hx.ReadTimeout("upstream timeout")
A.answer = timeout
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m8["mandate"], m8["secret"], inv9)})
A.answer = real_answer
assert r.status_code == 502 and r.json()["refund"]["refunded"], r.text

# 19. Pending capture abandoned by the agent: the sweep refunds once PayPal completes it.
inv10 = invoice()
STATE["pending_next"] = True
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(m8["mandate"], m8["secret"], inv10)})
assert r.status_code == 402 and "PENDING" in r.json()["error"]
with A.db() as c:
    oid = c.execute("SELECT order_id FROM pp_invoices WHERE id=?", (inv10,)).fetchone()[0]
    STATE["by_order_id"][oid]["purchase_units"][0]["payments"]["captures"][0]["status"] = "COMPLETED"
    n_ref = len(STATE["refunds"])
    asyncio.run(paypal402.reconcile(c, older_than_s=-1))
    assert c.execute("SELECT status FROM pp_invoices WHERE id=?", (inv10,)).fetchone()[0] == "refunded"
assert len(STATE["refunds"]) == n_ref + 1

# 12. Owner page and browser-facing approval result.
r = cl.get("/mandates")
assert r.status_code == 200 and "Agent spending mandates" in r.text
m3 = cl.post("/v1/mandates", json={"budget_cents": 50, "per_request_cents": 10, "days": 1}).json()
r = cl.get(f"/v1/mandates/{m3['mandate']}/approved", headers={"Accept": "text/html"})
assert r.status_code == 200 and "Mandate approved" in r.text
r = cl.get("/v1/mandates/m_unknown/approved", headers={"Accept": "text/html"})
assert "not activated" in r.text

# 19b. The owner sees the payment history with PayPal capture ids.
hist = cl.get(f"/v1/mandates/{m['mandate']}", headers={"X-Mandate-Secret": m["secret"]}).json()["payments"]
assert hist and all(p["capture"] and p["status"] in ("paid", "refunded") for p in hist), hist

# 20. Approval confirmed by polling PayPal from the owner's status check (redirect never reached the server).
m9 = cl.post("/v1/mandates", json={"budget_cents": 20, "per_request_cents": 5, "days": 1}).json()
h9 = {"X-Mandate-Secret": m9["secret"]}
assert cl.get(f"/v1/mandates/{m9['mandate']}", headers=h9).json()["status"] == "pending"
STATE["setup_approved"] = True
paypal402._last_poll.clear()
assert cl.get(f"/v1/mandates/{m9['mandate']}", headers=h9).json()["status"] == "active"
assert cl.get(f"/v1/mandates/{m9['mandate']}", headers={"X-Mandate-Secret": "wrong"}).status_code == 404
STATE["setup_approved"] = False

print("paypal rail: all tests passed;", STATE["orders"], "captures,", len(STATE["calls"]), "PayPal calls")
