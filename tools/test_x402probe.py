"""Offline fixtures for the x402 conformance probe. Run: python3 tools/test_x402probe.py"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import x402probe as X  # noqa: E402

PAYTO, USDC, URL = "0x" + "ab" * 20, "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "https://api.example.com/v1/x"
V1 = {"x402Version": 1, "accepts": [{"scheme": "exact", "network": "base", "maxAmountRequired": "10000", "resource": URL,
                                      "payTo": PAYTO, "maxTimeoutSeconds": 60, "asset": USDC, "extra": {"name": "USD Coin", "version": "2"}}]}
V2 = {"x402Version": 2, "resource": {"url": URL}, "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "10000",
                                                               "payTo": PAYTO, "maxTimeoutSeconds": 60, "asset": USDC, "extra": {"name": "USD Coin", "version": "2"}}]}


def cap(status, body=None, header=None):
    h = {"payment-required": base64.b64encode(json.dumps(header).encode()).decode()} if header else {}
    return {"status": status, "headers": h, "body": json.dumps(body) if body is not None else ""}


def bad(r):
    return {f["id"] for f in r["findings"] if not f["pass"] and f["severity"] == "error"}


assert X.analyze(cap(402, V1), URL)["payable"]
assert X.analyze(cap(402, {}, V2), URL)["payable"]
assert "header_vs_body" not in bad(X.analyze(cap(402, V1, V2), URL))          # one offer, two spellings
assert bad(X.analyze(cap(402, {"message": "pay"}), URL)) == {"requirements"}
dec = json.loads(json.dumps(V1)); dec["accepts"][0]["maxAmountRequired"] = "0.002"
assert "accepts[0].amount" in bad(X.analyze(cap(402, dec), URL))
noex = json.loads(json.dumps(V1)); noex["accepts"][0]["extra"] = {}
assert "accepts[0].extra.eip712" in bad(X.analyze(cap(402, noex), URL))
other = json.loads(json.dumps(V1)); other["accepts"][0]["payTo"] = "0x" + "cd" * 20
assert "header_vs_body" in bad(X.analyze(cap(402, other, V2), URL))
assert X.check_url("http://example.com") and X.check_url("https://10.0.0.1/x")
print("x402probe OK")
