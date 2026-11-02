"""NEAR mandate rail against a simulated NEAR RPC that serves transactions with contract event logs.
Run: python3 test_near.py"""
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

TXS = {}  # tx_hash -> transaction result, as the RPC `tx` method returns it
RPC = {"down": False}
CONTRACT = "mandate.gram402.testnet"


def make_tx(invoice, amount, merchant="shop.testnet", executor=CONTRACT, transfer_fails=False, mandate_id=7):
    ev = {"standard": "near-mandate", "version": "2.0.0", "event": "paid",
          "data": [{"mandate_id": mandate_id, "merchant": merchant, "amount": str(amount), "invoice": invoice}]}
    outs = [{"id": "r1", "outcome": {"executor_id": executor, "logs": ["EVENT_JSON:" + json.dumps(ev)],
                                     "status": {"SuccessReceiptId": "r2"}}},
            {"id": "r2", "outcome": {"executor_id": merchant, "logs": [],
                                     "status": {"Failure": {"ActionError": {}}} if transfer_fails else {"SuccessValue": ""}}}]
    return {"status": {"SuccessValue": ""}, "receipts_outcome": outs}


def rpc(req: httpx.Request):
    if RPC["down"]:
        return httpx.Response(503)
    b = json.loads(req.content)
    assert b["method"] == "tx" and b["params"]["wait_until"] == "FINAL"
    tx = TXS.get(b["params"]["tx_hash"])
    if tx is None:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": b["id"], "error": {"name": "HANDLER_ERROR",
                                                                                  "cause": {"name": "UNKNOWN_TRANSACTION"}}})
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": b["id"], "result": tx})


near402._transport = httpx.MockTransport(rpc)
cl = TestClient(A.app)
REQ = {"model": "echo", "messages": [{"role": "user", "content": "hi near"}]}


def offer():
    r = cl.post("/v1/chat/completions", json=REQ)
    assert r.status_code == 402
    acc = {a["scheme"]: a for a in r.json()["accepts"]}
    assert "near-mandate" in acc and "ton-transfer" in acc
    a = acc["near-mandate"]
    assert a["payTo"] == "shop.testnet" and a["extra"]["contract"] == CONTRACT
    assert a["extra"]["args"]["invoice"] == a["extra"]["invoice"] and "resource" not in a["extra"]["args"]
    return a


def xpay(inv, sec, tx="", sender="agent.testnet"):
    return base64.b64encode(json.dumps({"x402Version": 1, "scheme": "near-mandate", "payload": {
        "invoice": inv, "secret": sec, "tx_hash": tx, "sender": sender}}).encode()).decode()


def claim(a, tx, **kw):
    return cl.post("/v1/chat/completions", json=REQ,
                   headers={"X-PAYMENT": xpay(a["extra"]["invoice"], kw.get("secret", a["extra"]["secret"]), tx)})


a = offer()
price = a["maxAmountRequired"]

# No transaction hash -> refused; unknown transaction -> retryable.
assert "tx_hash" in claim(a, "").json()["error"]
r = claim(a, "TXPENDING")
assert r.status_code == 402 and r.headers.get("retry-after") == "3"

TXS["TX1"] = make_tx(a["extra"]["invoice"], price)
# Wrong secret (front-running from the public invoice id) is refused.
r = claim(a, "TX1", secret="stolen")
assert r.status_code == 402 and "secret" in r.json()["error"]
# Correct claim -> answer + receipt bound to the transaction.
r = claim(a, "TX1")
assert r.status_code == 200, r.text
body = r.json()
rc = body["gram402"]["receipt"]
assert rc["payment"]["network"] == "near-testnet" and rc["payment"]["transaction"] == "TX1"
assert rc["payment"]["charged_yocto"] == price and receipts.verify(rc, REQ, body)
# Settles once; the same transaction cannot settle another invoice.
assert "already settled" in claim(a, "TX1").json()["error"]
b2 = offer()
assert "another invoice" in claim(b2, "TX1").json()["error"]

# Bad transactions: underpaid, other merchant, event from another contract, failed transfer.
cases = [(dict(amount=1), "underpaid"), (dict(merchant="evil.testnet"), "no payment for this invoice"),
         (dict(executor="fake.testnet"), "no payment for this invoice"), (dict(transfer_fails=True), "failed")]
for i, (kw, why) in enumerate(cases):
    o = offer()
    args = {"invoice": o["extra"]["invoice"], "amount": price, **kw}
    TXS[f"BAD{i}"] = make_tx(**args)
    r = claim(o, f"BAD{i}")
    assert r.status_code == 402 and why in r.json()["error"], (why, r.text)

# Paid but the answer failed -> the same X-PAYMENT is served once more, then never again.
o = offer()
TXS["TX2"] = make_tx(o["extra"]["invoice"], price)
real = A.answer


async def broken(body):
    raise A.HTTPException(502, "upstream down")
A.answer = broken
r = claim(o, "TX2")
assert r.status_code == 502 and "credit" in r.json()
A.answer = real
assert claim(o, "TX2").status_code == 200
assert claim(o, "TX2").status_code == 402

# RPC outage -> retryable 402, nothing settled.
o = offer()
RPC["down"] = True
r = claim(o, "TX3")
assert r.status_code == 402 and r.headers.get("retry-after") == "3"
RPC["down"] = False
print("near rail: all tests passed")
