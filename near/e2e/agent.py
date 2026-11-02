"""Live end-to-end on NEAR testnet: an agent hits HTTP 402, pays through the mandate contract, gets the answer.

Run by near/e2e/run.sh in CI. The payment is made with near-cli-rs signing as the agent account; the GRAM-402
reference server verifies it with a view call on the contract. Author: Aleksandr Shcherbakov (@miinimal).
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("GRAM402_URL", "http://127.0.0.1:8402")
CONTRACT, AGENT, MANDATE = os.environ["CONTRACT"], os.environ["AGENT"], int(os.environ["MANDATE_ID"])
REQ = {"model": "echo", "messages": [{"role": "user", "content": "Hello from an agent paying on NEAR"}]}


def post(headers=None):
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=json.dumps(REQ).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"{}")


def near_pay(args):
    cmd = ["near", "contract", "call-function", "as-transaction", CONTRACT, "pay", "json-args",
           json.dumps({"mandate_id": MANDATE, **args}), "prepaid-gas", "30 Tgas", "attached-deposit", "0 NEAR",
           "sign-as", AGENT, "network-config", "testnet", "sign-with-legacy-keychain", "send"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    out = p.stdout + p.stderr
    print(out[-1500:], flush=True)
    return p.returncode == 0 and "Error" not in out.split("Transaction ID")[0][-400:], out


def one_round(label, amount_override=None):
    st, hd, req = post()
    assert st == 402, (st, req)
    acc = next(a for a in req["accepts"] if a["scheme"] == "near-mandate")
    args = dict(acc["extra"]["args"])
    if amount_override:
        args["amount"] = amount_override
    print(f"\n== {label}: 402 offer {acc['maxAmountRequired']} yocto, invoice {acc['extra']['invoice']}", flush=True)
    ok, out = near_pay(args)
    if not ok:
        return False, out
    xpay = base64.b64encode(json.dumps({"x402Version": 1, "scheme": "near-mandate", "payload": {
        "invoice": acc["extra"]["invoice"], "secret": acc["extra"]["secret"]}}).encode()).decode()
    for _ in range(20):
        st, hd, out = post({"X-PAYMENT": xpay})
        if st == 402 and any(k.lower() == "retry-after" for k in hd):
            time.sleep(3)
            continue
        break
    assert st == 200, (st, out)
    print(f"== {label}: answer {out['choices'][0]['message']['content']!r}; receipt payment {out['gram402']['payment']}",
          flush=True)
    return True, out


if os.environ.get("ONLY_ONE"):
    ok, _ = one_round("sandboxed agent payment (function-call key)")
    assert ok, "sandboxed agent could not pay"
    print("\nE2E OK: sandboxed agent paid through the mandate", flush=True)
    sys.exit(0)
ok1, _ = one_round("payment 1")
ok2, _ = one_round("payment 2")
assert ok1 and ok2, "paid rounds failed"
# Over the per-payment cap: the contract itself must refuse.
over = str(int(os.environ["CAP_YOCTO"]) + 1)
ok3, out3 = one_round("over the cap (must be refused on chain)", amount_override=over)
assert not ok3 and "per-payment cap" in out3, "the contract did not refuse an amount over the cap"
print("\nE2E OK: two paid requests, over-cap payment refused by the contract", flush=True)
sys.exit(0)
