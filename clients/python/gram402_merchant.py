"""Sell any HTTP API to AI agents for GRAM through the GRAM-402 facilitator (payments go to your own wallet).

    m = Merchant(api_key="mai-...", pay_to="UQ...your wallet")
    payment = request.headers.get("X-PAYMENT")
    if not payment:
        return 402, m.invoice(amount_nano=50_000_000, resource="/my/endpoint")["x402"]
    s = m.settle(payment)
    if not s.get("success"):
        return 402, {"error": s.get("invalidReason")}
    # serve the request; add header X-PAYMENT-RESPONSE = s["x_payment_response"]

Standard library only. Author: Aleksandr Shcherbakov (@miinimal) - MINIMALCULT.
"""
import json
import urllib.error
import urllib.request

BASE = "https://ai.minimalcult.org"


class Merchant:
    def __init__(self, api_key, pay_to, base=BASE, timeout=20):
        self.key, self.pay_to, self.base, self.timeout = api_key, pay_to, base.rstrip("/"), timeout

    def _post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"success": False, "isValid": False, "status": e.code, "invalidReason": e.read().decode()[:300]}

    def invoice(self, amount_nano, resource="", description=""):
        return self._post("/v1/facilitator/invoices", {"pay_to": self.pay_to, "amount_nano": int(amount_nano),
                                                       "resource": resource, "description": description})

    def verify(self, x_payment):
        return self._post("/v1/facilitator/verify", {"payment": x_payment})

    def settle(self, x_payment):
        return self._post("/v1/facilitator/settle", {"payment": x_payment})
