"""PayPal rail under stress: separate processes spending one budget at the same moment, a crash after the capture
but before the result is stored, replay of an invoice across mandates and endpoints, and the structured error object.
Offline (simulated PayPal). Run: python3 test_paypal_concurrency.py"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

if "--child" not in sys.argv:
    os.environ.update({"GRAM402_DB": tempfile.mktemp(suffix=".db"), "PAY402_NETWORK": "testnet",
                       "PAY402_ADDRESS": "0QDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVQ_U",
                       "PAYPAL_CLIENT_ID": "test-id", "PAYPAL_CLIENT_SECRET": "test-secret",
                       "PAYPAL_PRICE_CENTS": "10", "PUBLIC_URL": "https://demo.example",
                       "PAYPAL_MAX_NEW_MANDATES_PER_IP_HOUR": "1000"})

import app as A  # noqa: E402
import fake_paypal  # noqa: E402
import paypal402  # noqa: E402

paypal402._transport = fake_paypal.transport()
STATE = fake_paypal.STATE
RES, PRICE = "/v1/chat/completions", 10


def settle(claim):
    with A.db() as c:
        return asyncio.run(paypal402.settle(c, claim, expect_resource=RES, expect_cents=PRICE))


if "--child" in sys.argv:  # one worker process: wait for the common start time, then try to pay
    _, _, mid, secret, inv, start = sys.argv
    time.sleep(max(0.0, float(start) - time.time()))
    print(json.dumps(settle({"mandate": mid, "secret": secret, "invoice": inv})))
    sys.exit(0)

from fastapi.testclient import TestClient  # noqa: E402

cl = TestClient(A.app)
REQ = {"model": "echo", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


def invoice():
    r = cl.post(RES, json=REQ)
    assert r.status_code == 402, r.text
    return {a["scheme"]: a for a in r.json()["accepts"]}["paypal-mandate"]["extra"]["invoice"]


def mandate(budget, cap=PRICE):
    m = cl.post("/v1/mandates", json={"budget_cents": budget, "per_request_cents": cap, "days": 1}).json()
    assert cl.get(f"/v1/mandates/{m['mandate']}/approved").json()["status"] == "active"
    return m


def spent(m):
    return cl.get(f"/v1/mandates/{m['mandate']}", headers={"X-Mandate-Secret": m["secret"]}).json()["spent_cents"]


# 1. Two processes, two different invoices, one budget that covers only one: exactly one is charged. 15 rounds.
for rnd in range(15):
    m = mandate(budget=15)
    invs = [invoice(), invoice()]
    start = str(time.time() + 1.5)
    procs = [subprocess.Popen([sys.executable, __file__, "--child", m["mandate"], m["secret"], i, start],
                              stdout=subprocess.PIPE, text=True) for i in invs]
    outs = [json.loads(p.communicate(timeout=60)[0].strip().splitlines()[-1]) for p in procs]
    ok = [o for o in outs if o["ok"]]
    bad = [o for o in outs if not o["ok"]]
    assert len(ok) == 1 and len(bad) == 1, (rnd, outs)
    assert bad[0]["error_detail"]["code"] in ("BUDGET_EXHAUSTED", "CONCURRENT_PAYMENT"), bad
    assert spent(m) == PRICE, (rnd, spent(m))
    with A.db() as c:
        rows = c.execute("SELECT status FROM pp_invoices WHERE id IN (?, ?)", invs).fetchall()
    assert sorted(r["status"] for r in rows) == ["open", "paid"], [dict(r) for r in rows]

# 2. Crash after PayPal captured but before the result was stored: the same request finishes it, one charge.
m = mandate(budget=100)
inv = invoice()
real_drive, orders_before = paypal402._drive, STATE["orders"]


async def crashing_drive(*a, **k):
    await real_drive(*a, **k)
    raise KeyboardInterrupt  # the process dies here: not caught as an ordinary error


paypal402._drive = crashing_drive
try:
    settle({"mandate": m["mandate"], "secret": m["secret"], "invoice": inv})
    raise AssertionError("the simulated crash did not happen")
except KeyboardInterrupt:
    pass
finally:
    paypal402._drive = real_drive
with A.db() as c:
    assert c.execute("SELECT status FROM pp_invoices WHERE id=?", (inv,)).fetchone()["status"] == "paying"
st = settle({"mandate": m["mandate"], "secret": m["secret"], "invoice": inv})
assert st["ok"] and STATE["orders"] == orders_before + 1 and spent(m) == PRICE, (st, STATE["orders"])
st = settle({"mandate": m["mandate"], "secret": m["secret"], "invoice": inv})  # a third try is refused, not recharged
assert not st["ok"] and st["error_detail"]["code"] == "INVOICE_INVALID" and STATE["orders"] == orders_before + 1

# 3. Replay: an invoice paid or reserved by mandate A cannot be paid by mandate B; nor bought for another endpoint.
a, b = mandate(budget=100), mandate(budget=100)
inv = invoice()
assert settle({"mandate": a["mandate"], "secret": a["secret"], "invoice": inv})["ok"]
st = settle({"mandate": b["mandate"], "secret": b["secret"], "invoice": inv})
assert not st["ok"] and st["error_detail"]["code"] == "INVOICE_INVALID" and spent(b) == 0, st
inv = invoice()
with A.db() as c:  # A holds the invoice in 'paying' (outcome unknown)
    c.execute("UPDATE pp_invoices SET status='paying', mandate=?, paying_since=? WHERE id=?",
              (a["mandate"], int(time.time()), inv))
    c.commit()
st = settle({"mandate": b["mandate"], "secret": b["secret"], "invoice": inv})
assert not st["ok"] and st["error_detail"]["code"] == "INVOICE_INVALID" and spent(b) == 0, st
inv = invoice()
with A.db() as c:
    st = asyncio.run(paypal402.settle(c, {"mandate": b["mandate"], "secret": b["secret"], "invoice": inv},
                                      expect_resource="/v1/other", expect_cents=PRICE))
assert not st["ok"] and st["error_detail"]["code"] == "INVOICE_MISMATCH", st

# 4. Error object. PayPal NOT_AUTHORIZED: configuration error, not retryable, nothing reserved, debug id kept.
m = mandate(budget=100)
inv = invoice()
STATE["status_next"] = (403, "NOT_AUTHORIZED")
r = cl.post(RES, json=REQ, headers={"X-PAYMENT": __import__("base64").b64encode(json.dumps(
    {"x402Version": 1, "scheme": "paypal-mandate",
     "payload": {"mandate": m["mandate"], "secret": m["secret"], "invoice": inv}}).encode()).decode()})
d = r.json()["error_detail"]
assert r.status_code == 402 and "Retry-After" not in r.headers, r.headers
assert d["code"] == "PAYMENT_PROVIDER_CONFIGURATION_ERROR" and d["retryable"] is False, d
assert d["provider"] == "paypal" and d["upstream_status"] == 403 and d["provider_error_name"] == "NOT_AUTHORIZED"
assert d["provider_debug_id"] == "dbg-not_authorized" and d["operation"] == "POST /v2/checkout/orders", d
assert d["correlation_id"].startswith("order-") and spent(m) == 0, d

# 5. PayPal 5xx during the order: outcome unknown, retryable, the reservation is kept so a repeat finishes it.
inv = invoice()
STATE["status_next"] = (503, "SERVICE_UNAVAILABLE")
st = settle({"mandate": m["mandate"], "secret": m["secret"], "invoice": inv})
assert not st["ok"] and st["retry"] and st["error_detail"]["code"] == "PAYMENT_OUTCOME_UNKNOWN", st
assert st["error_detail"]["retryable"] is True and st["error_detail"]["upstream_status"] == 503 and spent(m) == PRICE
assert settle({"mandate": m["mandate"], "secret": m["secret"], "invoice": inv})["ok"] and spent(m) == PRICE

# 6. Limit errors carry stable codes; budget exhausted is not retryable.
small = mandate(budget=PRICE)
assert settle({"mandate": small["mandate"], "secret": small["secret"], "invoice": invoice()})["ok"]
st = settle({"mandate": small["mandate"], "secret": small["secret"], "invoice": invoice()})
assert st["error_detail"] == {"code": "BUDGET_EXHAUSTED", "retryable": False} and "retry" not in st, st
st = settle({"mandate": small["mandate"], "secret": "wrong", "invoice": invoice()})
assert st["error_detail"]["code"] == "MANDATE_NOT_FOUND", st

# 7. Wrong PayPal app credentials: mandate creation answers 502 with a configuration error, not a 500.
paypal402._token.update(value=None, exp=0)
STATE["oauth_fail"] = True
r = cl.post("/v1/mandates", json={"budget_cents": 100, "per_request_cents": 10, "days": 1})
STATE["oauth_fail"] = False
assert r.status_code == 502, r.text
d = r.json()["error_detail"]
assert d["code"] == "PAYMENT_PROVIDER_CONFIGURATION_ERROR" and d["retryable"] is False, d
assert d["operation"] == "POST /v1/oauth2/token" and d["provider_error_name"] == "invalid_client", d

print("paypal concurrency, crash, replay and error object: ok")
