"""Offline tests of the reference server: mocked TON chain, built-in echo model.  Run: python3 test_app.py"""
import base64
import json
import os
import tempfile

os.environ.update({"GRAM402_DB": tempfile.mktemp(suffix=".db"), "PAY402_NETWORK": "testnet",
                   "PAY402_ADDRESS": "0QDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVQ_U"})

from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402
import pay402  # noqa: E402

PAYER = "0:3C02D2FAE2B263048DA2C03AAF0C2BEB208F6828C3547D4A86E19A98F1BFAABA"
CHAIN = []


def tx(h, comment, value, aborted=False):
    return {"hash": h, "description": {"aborted": aborted, "compute_ph": {"success": True}},
            "in_msg": {"source": PAYER, "value": str(value),
                       "message_content": {"decoded": {"type": "text_comment", "comment": comment}}}}


async def fake_fetch(client=None, limit=100):
    return list(CHAIN)


pay402.fetch_incoming = fake_fetch
REQ = {"model": "echo", "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]}


def xpay(inv, secret):
    return base64.b64encode(json.dumps({"x402Version": 1, "scheme": "ton-transfer",
                                        "payload": {"invoice": inv, "secret": secret}}).encode()).decode()


def main():
    c = TestClient(A.app)
    r = c.post("/v1/chat/completions", json=REQ)
    assert r.status_code == 402, r.text
    acc = r.json()["accepts"][0]
    inv, sec, amount = acc["extra"]["invoice"], acc["extra"]["secret"], int(acc["maxAmountRequired"])
    assert acc["scheme"] == "ton-transfer" and acc["network"] == "ton-testnet" and acc["extra"]["comment"] == "mai:" + inv

    r = c.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(inv, sec)})
    assert r.status_code == 402 and r.headers.get("retry-after") == "5"          # not on chain yet

    CHAIN.append(tx("tx1", "mai:" + inv, amount * 3))
    r = c.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(inv, "guessed")})
    assert r.status_code == 402 and "secret" in r.json()["error"]                 # public comment alone never settles

    r = c.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(inv, sec)})
    assert r.status_code == 200, r.text
    g = r.json()["gram402"]
    assert g["payment"]["transaction"] == "tx1" and g["receipt"]["payment"]["transaction"] == "tx1"
    v = c.post("/v1/receipts/verify", json={"receipt": g["receipt"], "request": REQ, "response": r.json()}).json()
    assert v["valid"], v
    hdr = json.loads(base64.b64decode(r.headers["x-payment-response"]))
    assert hdr["success"] and hdr["api_key"] == g["payment"]["api_key"]

    r = c.post("/v1/chat/completions", json=REQ, headers={"Authorization": "Bearer " + g["payment"]["api_key"]})
    assert r.status_code == 200                                                    # credit reused, no new payment

    r = c.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(inv, sec)})
    assert r.status_code == 402 and "already settled" in r.json()["error"]         # invoice settles once

    r2 = c.post("/v1/chat/completions", json=REQ).json()["accepts"][0]["extra"]
    CHAIN.append(tx("tx1", "mai:" + r2["invoice"], amount * 3))
    r = c.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(r2["invoice"], r2["secret"])})
    assert r.status_code == 402 and "another" in r.json()["error"]                 # one tx pays one invoice

    r3 = c.post("/v1/chat/completions", json=REQ).json()["accepts"][0]["extra"]
    CHAIN.append(tx("tx3", "mai:" + r3["invoice"], 1))
    r = c.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(r3["invoice"], r3["secret"])})
    assert r.status_code == 402 and r.json()["error"].startswith("underpaid")      # underpayment does not settle...
    assert r.json()["underpaid"]["credit_nano"] == 1                               # ...but is credited, not lost
    r = c.post("/v1/chat/completions", json=REQ, headers={"X-PAYMENT": xpay(r3["invoice"], r3["secret"])})
    assert "closed as underpaid" in r.json()["error"]                              # credited once
    print("gram402 server OK")


def test_upstream_body_pins_model_and_caps_output():
    import app as A
    old = (A.UPSTREAM_MODEL, A.UPSTREAM_MAX_TOKENS)
    try:
        A.UPSTREAM_MODEL, A.UPSTREAM_MAX_TOKENS = "", 0
        assert A.upstream_body({"model": "x", "n": 3})["n"] == 3  # unpinned: passed through as before
        A.UPSTREAM_MODEL, A.UPSTREAM_MAX_TOKENS = "cheap-model", 300
        b = A.upstream_body({"model": "expensive", "messages": [], "max_tokens": 100000, "n": 3, "stream": True})
        assert b == {"model": "cheap-model", "messages": [], "max_tokens": 300, "stream": False}
        assert A.upstream_body({"messages": []})["max_tokens"] == 300
        assert A.upstream_body({"messages": [], "max_tokens": 50})["max_tokens"] == 50
    finally:
        A.UPSTREAM_MODEL, A.UPSTREAM_MAX_TOKENS = old


if __name__ == "__main__":
    main()
    test_upstream_body_pins_model_and_caps_output()
    print("upstream pin test ok")
