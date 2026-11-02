"""NEAR mandate rail against a simulated NEAR RPC (contract state in memory). Run: python3 test_near.py"""
import base64
import json
import os
import tempfile

os.environ.update({"GRAM402_DB": tempfile.mktemp(suffix=".db"), "PAY402_NETWORK": "testnet",
                   "PAY402_ADDRESS": "0QDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVQ_U",
                   "NEAR_MANDATE_CONTRACT": "mandate.gram402.testnet", "NEAR_MERCHANT": "shop.testnet",
                   "NEAR_PRICE_YOCTO": "1000000000000000000000"})

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402
import near402  # noqa: E402
import receipts  # noqa: E402

PAID = {}  # invoice -> Payment, as the contract would store it
RPC = {"down": False}


def rpc(req: httpx.Request):
    if RPC["down"]:
        return httpx.Response(503)
    b = json.loads(req.content)
    p = b["params"]
    assert p["account_id"] == "mandate.gram402.testnet" and p["method_name"] == "get_payment" and p["finality"] == "final"
    inv = json.loads(base64.b64decode(p["args_base64"]))["invoice"]
    res = json.dumps(PAID.get(inv)).encode()
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": b["id"], "result": {"result": list(res), "logs": []}})


near402._transport = httpx.MockTransport(rpc)
cl = TestClient(A.app)
REQ = {"model": "echo", "messages": [{"role": "user", "content": "hi near"}]}


def offer():
    r = cl.post("/v1/chat/completions", json=REQ)
    assert r.status_code == 402
    acc = {a["scheme"]: a for a in r.json()["accepts"]}
    assert "near-mandate" in acc and "ton-transfer" in acc
    a = acc["near-mandate"]
    assert a["payTo"] == "shop.testnet" and a["extra"]["contract"] == "mandate.gram402.testnet"
    assert a["extra"]["args"]["invoice"] == a["extra"]["invoice"]
    return a


def xpay(inv, sec):
    return base64.b64encode(json.dumps({"x402Version": 1, "scheme": "near-mandate",
                                        "payload": {"invoice": inv, "secret": sec}}).encode()).decode()


a = offer()
inv, sec = a["extra"]["invoice"], a["extra"]["secret"]

# Not paid yet -> 402 with Retry-After.
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(inv, sec)})
assert r.status_code == 402 and r.headers.get("retry-after") == "3"

# The agent pays through the contract (simulated state).
PAID[inv] = {"mandate_id": 7, "merchant": "shop.testnet", "amount": a["maxAmountRequired"], "resource": "/v1/chat/completions",
             "paid_at_ms": 1}

# Wrong secret (front-running from the public invoice id) is refused.
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(inv, "stolen")})
assert r.status_code == 402 and "secret" in r.json()["error"]

# Correct claim -> answer + receipt bound to the contract payment.
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(inv, sec)})
assert r.status_code == 200, r.text
body = r.json()
rc = body["gram402"]["receipt"]
assert rc["payment"]["network"] == "near-testnet" and rc["payment"]["transaction"] == f"mandate.gram402.testnet/{inv}"
assert rc["payment"]["charged_yocto"] == a["maxAmountRequired"] and receipts.verify(rc, REQ, body)

# Settles once.
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(inv, sec)})
assert r.status_code == 402 and "already settled" in r.json()["error"]

# Underpaid, other merchant, other resource.
for bad, why in [({"amount": "1"}, "underpaid"), ({"merchant": "evil.testnet"}, "another merchant"),
                 ({"resource": "/v1/other"}, "another resource")]:
    o = offer()
    PAID[o["extra"]["invoice"]] = {"mandate_id": 7, "merchant": "shop.testnet", "amount": o["maxAmountRequired"],
                                   "resource": "/v1/chat/completions", "paid_at_ms": 1, **bad}
    r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(o["extra"]["invoice"], o["extra"]["secret"])})
    assert r.status_code == 402 and why in r.json()["error"], (why, r.text)

# RPC outage -> retryable 402, nothing settled.
o = offer()
RPC["down"] = True
r = cl.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(o["extra"]["invoice"], o["extra"]["secret"])})
assert r.status_code == 402 and r.headers.get("retry-after") == "3"
print("near rail: all tests passed")
