"""ASGI paywall + generic client call, against a stub facilitator. Run: python3 clients/python/test_paywall.py
Needs fastapi and httpx (server/requirements.txt)."""
import base64
import io
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gram402 import Gram402Client  # noqa: E402
from gram402_merchant import paywall  # noqa: E402

WALLET, PAID, POLLS = "UQmerchant", [], {"left": 2}


class StubMerchant:
    def invoice(self, amount, resource="", description=""):
        acc = {"scheme": "ton-transfer", "network": "ton-testnet", "payTo": WALLET, "maxAmountRequired": str(amount),
               "extra": {"invoice": "i1", "secret": "s1", "comment": "f402:i1"}}
        return {"x402": {"x402Version": 1, "accepts": [acc]}}

    def settle(self, xpay):
        p = json.loads(base64.b64decode(xpay))["payload"]
        if p != {"invoice": "i1", "secret": "s1"} or not PAID:
            return {"success": False, "invalidReason": "unknown invoice or wrong secret"}
        if POLLS["left"]:
            POLLS["left"] -= 1
            return {"success": False, "isValid": False, "retry": True, "invalidReason": "payment not found on chain yet"}
        if PAID[-1] == "settled":
            return {"success": False, "invalidReason": "already settled"}
        PAID.append("settled")
        return {"success": True, "x_payment_response": base64.b64encode(b'{"transaction":"t1"}').decode()}


api = FastAPI()


@api.get("/v1/weather")
def weather():
    return {"temp_c": 19}


shop = TestClient(paywall(api, StubMerchant(), {"/v1/weather": 20_000_000}))


class Resp(io.BytesIO):
    def __init__(self, r):
        super().__init__(r.content)
        self.status, self.headers = r.status_code, dict(r.headers)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(req, timeout=None):
    r = shop.request(req.get_method(), req.full_url.replace("https://shop.example", ""), headers=dict(req.header_items()),
                     content=req.data)
    if r.status_code >= 400:
        raise urllib.error.HTTPError(req.full_url, r.status_code, "", r.headers, io.BytesIO(r.content))
    return Resp(r)


agent = Gram402Client("https://shop.example", pay=lambda to, amt, c: PAID.append((to, amt, c)), max_nano=10**8,
                      poll_s=0, opener=opener)
st, hd, out = agent.call("https://shop.example/v1/weather", method="GET")
assert st == 200 and out == {"temp_c": 19}, (st, out)
assert PAID[0] == (WALLET, 20_000_000, "f402:i1") and json.loads(base64.b64decode(agent.last_payment))["transaction"] == "t1"
again = shop.get("/v1/weather", headers={"X-PAYMENT": base64.b64encode(json.dumps(
    {"payload": {"invoice": "i1", "secret": "s1"}}).encode()).decode()})
assert again.status_code == 402 and "already" in again.json()["error"]
print("paywall OK")
