"""Paywall for any ASGI app, paid from PayPal mandates (simulated PayPal API). Run: python3 test_paypal_paywall.py"""
import base64
import json
import os
import tempfile

os.environ.update({"PAYPAL_CLIENT_ID": "x", "PAYPAL_CLIENT_SECRET": "x", "PUBLIC_URL": "https://merchant.example"})

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import fake_paypal  # noqa: E402
import paypal402  # noqa: E402
from paypal_paywall import paypal_paywall  # noqa: E402

paypal402._transport = fake_paypal.transport()
api = FastAPI()


@api.get("/v1/weather")
def weather(city: str = "Lisbon"):
    return {"city": city, "temp_c": 21}


@api.get("/free")
def free():
    return {"free": True}


cl = TestClient(paypal_paywall(api, {"/v1/weather": 5}, tempfile.mktemp(suffix=".db")))
assert cl.get("/free").json() == {"free": True}

r = cl.get("/v1/weather")
assert r.status_code == 402
acc = r.json()["accepts"][0]
assert acc["scheme"] == "paypal-mandate" and acc["maxAmountRequired"] == "5"

m = cl.post("/v1/mandates", json={"budget_cents": 12, "per_request_cents": 5, "days": 1}).json()
assert m["approve_url"]
assert cl.get(f"/v1/mandates/{m['mandate']}/approved").json()["ok"]


def pay(invoice):
    return base64.b64encode(json.dumps({"x402Version": 1, "scheme": "paypal-mandate", "payload": {
        "mandate": m["mandate"], "secret": m["secret"], "invoice": invoice}}).encode()).decode()


r = cl.get("/v1/weather?city=Porto", headers={"X-PAYMENT": pay(acc["extra"]["invoice"])})
assert r.status_code == 200 and r.json() == {"city": "Porto", "temp_c": 21}
hdr = json.loads(base64.b64decode(r.headers["x-payment-response"]))
assert hdr["transaction"].startswith("CAP") and hdr["mandate_left_cents"] == 7

r = cl.get("/v1/weather", headers={"X-PAYMENT": pay(acc["extra"]["invoice"])})
assert r.status_code == 402 and "already used" in r.json()["error"]

st = cl.get(f"/v1/mandates/{m['mandate']}", headers={"X-Mandate-Secret": m["secret"]}).json()
assert st["spent_cents"] == 5
assert cl.get(f"/v1/mandates/{m['mandate']}", headers={"X-Mandate-Secret": "bad"}).status_code == 404
assert cl.delete(f"/v1/mandates/{m['mandate']}", headers={"X-Mandate-Secret": m["secret"]}).json()["status"] == "revoked"
inv = cl.get("/v1/weather").json()["accepts"][0]["extra"]["invoice"]
r = cl.get("/v1/weather", headers={"X-PAYMENT": pay(inv)})
assert r.status_code == 402 and "revoked" in r.json()["error"]
# An invoice from a cheap path cannot pay for an expensive one (review finding: price and allowlist bypass).
@api.get("/premium")
def premium():
    return {"premium": True}


multi = TestClient(paypal_paywall(api, {"/v1/weather": 1, "/premium": 100}, tempfile.mktemp(suffix=".db")))
mm = multi.post("/v1/mandates", json={"budget_cents": 500, "per_request_cents": 100, "days": 1,
                                      "resources": ["/v1/weather"]}).json()
multi.get(f"/v1/mandates/{mm['mandate']}/approved")
cheap = multi.get("/v1/weather").json()["accepts"][0]["extra"]["invoice"]
xp = base64.b64encode(json.dumps({"x402Version": 1, "scheme": "paypal-mandate", "payload": {
    "mandate": mm["mandate"], "secret": mm["secret"], "invoice": cheap}}).encode()).decode()
r = multi.get("/premium", headers={"X-PAYMENT": xp})
assert r.status_code == 402 and "another resource" in r.json()["error"], r.text

# Downstream 5xx after payment: refunded.
@api.get("/boom")
def boom():
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "down"}, status_code=503)


bc = TestClient(paypal_paywall(api, {"/boom": 3}, tempfile.mktemp(suffix=".db")))
mb = bc.post("/v1/mandates", json={"budget_cents": 50, "per_request_cents": 5, "days": 1}).json()
bc.get(f"/v1/mandates/{mb['mandate']}/approved")
ib = bc.get("/boom").json()["accepts"][0]["extra"]["invoice"]
n_ref = len(fake_paypal.STATE["refunds"])
r = bc.get("/boom", headers={"X-PAYMENT": base64.b64encode(json.dumps({"x402Version": 1, "scheme": "paypal-mandate",
           "payload": {"mandate": mb["mandate"], "secret": mb["secret"], "invoice": ib}}).encode()).decode()})
assert r.status_code == 503 and len(fake_paypal.STATE["refunds"]) == n_ref + 1
assert bc.get(f"/v1/mandates/{mb['mandate']}", headers={"X-Mandate-Secret": mb["secret"]}).json()["spent_cents"] == 0

# Per-address limit on new mandates.
paypal402.MAX_NEW_PER_IP_HOUR = 1
r = cl.post("/v1/mandates", json={"budget_cents": 10, "per_request_cents": 5, "days": 1})
assert r.status_code == 429, r.text

# Both rails on one path: GRAM through a facilitator merchant, PayPal through the mandate.
class FakeGram:
    def invoice(self, nano, resource, description=""):
        return {"x402": {"accepts": [{"scheme": "ton-transfer", "maxAmountRequired": str(nano), "resource": resource,
                                      "extra": {"invoice": "g1"}}]}}

    def settle(self, xp):
        return {"success": True, "x_payment_response": "gram-ok"} if "g1" in xp else {"success": False, "retry": True,
                                                                                      "invalidReason": "not on chain yet"}


paypal402.MAX_NEW_PER_HOUR = 60
both = TestClient(paypal_paywall(api, {"/v1/weather": {"cents": 5, "nano": 50_000_000}}, tempfile.mktemp(suffix=".db"),
                                 gram=FakeGram()))
r = both.get("/v1/weather")
assert r.status_code == 402 and [a["scheme"] for a in r.json()["accepts"]] == ["ton-transfer", "paypal-mandate"]
r = both.get("/v1/weather", headers={"X-PAYMENT": "g1-claim"})
assert r.status_code == 200 and r.headers["x-payment-response"] == "gram-ok"
r = both.get("/v1/weather", headers={"X-PAYMENT": "other"})
assert r.status_code == 402 and r.headers.get("retry-after") == "5"
import throttle  # noqa: E402
throttle.LIMIT = 3
throttle._hits.clear()
tc = TestClient(paypal_paywall(api, {"/v1/weather": 5}, tempfile.mktemp(suffix=".db")))
codes = [tc.get("/v1/weather").status_code for _ in range(5)]
assert codes == [402, 402, 402, 429, 429], codes
throttle.LIMIT = 60
print("paypal paywall OK")
