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
print("paypal paywall OK")
