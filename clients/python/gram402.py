"""GRAM-402 client: call a paid AI endpoint with only a TON wallet (spec: docs/protocol/GRAM-402.md).

    from gram402 import Gram402Client
    cli = Gram402Client("https://gateway.example", pay=my_wallet_pay)   # pay(pay_to, amount_nano, comment)
    answer = cli.chat({"model": "Qwen/Qwen3-8B", "messages": [{"role": "user", "content": "hi"}]})

The client answers 402 automatically: pays the invoice through the `pay` callback (any wallet library), retries with
X-PAYMENT until the transfer is visible on chain, then keeps the returned api_key and spends the remaining credit
with it; when the credit runs out it pays again. `max_nano` caps what a single request may cost.
Stdlib only. Author: Aleksandr Shcherbakov (@miinimal).
"""
import base64
import json
import time
import urllib.error
import urllib.request


class PaymentError(Exception):
    pass


class Gram402Client:
    def __init__(self, base_url, pay, max_nano=200_000_000, wait_s=120, poll_s=5, api_key=None, opener=None):
        self.base = base_url.rstrip("/")
        self.pay, self.max_nano, self.wait_s, self.poll_s = pay, max_nano, wait_s, poll_s
        self.api_key = api_key
        self.last_payment = None
        self._open = opener or urllib.request.urlopen

    def _post(self, path, body, headers):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with self._open(req, timeout=180) as r:
                return r.status, dict(r.headers), json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read() or b"{}")

    def chat(self, body):
        if self.api_key:
            st, hd, out = self._post("/v1/chat/completions", body, {"Authorization": "Bearer " + self.api_key})
            if st != 402:
                return self._done(st, out)
            self.api_key = None  # credit spent: pay again
        st, hd, req = self._post("/v1/chat/completions", body, {})
        if st != 402:
            return self._done(st, req)
        acc = next((a for a in req.get("accepts", []) if a.get("scheme") == "ton-transfer"), None)
        if not acc:
            raise PaymentError("server offers no ton-transfer payment")
        amount = int(acc["maxAmountRequired"])
        if amount > self.max_nano:
            raise PaymentError(f"price {amount} nano-GRAM exceeds max_nano {self.max_nano}")
        ex = acc["extra"]
        self.pay(acc["payTo"], amount, ex["comment"])
        xpay = base64.b64encode(json.dumps({"x402Version": 1, "scheme": acc["scheme"], "network": acc["network"],
                                            "payload": {"invoice": ex["invoice"], "secret": ex["secret"]}}).encode()).decode()
        deadline = time.time() + self.wait_s
        while True:
            st, hd, out = self._post("/v1/chat/completions", body, {"X-PAYMENT": xpay})
            pay = (out.get("gram402") or out.get("minimalai") or {}).get("payment")
            if pay:
                self.api_key, self.last_payment = pay["api_key"], pay
            if st == 402 and any(k.lower() == "retry-after" for k in hd) and time.time() < deadline:
                time.sleep(self.poll_s)
                continue
            return self._done(st, out)

    @staticmethod
    def _done(status, out):
        if status != 200:
            raise PaymentError(f"HTTP {status}: {json.dumps(out)[:300]}")
        return out
