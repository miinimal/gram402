"""Sell any HTTP API to AI agents for GRAM through the GRAM-402 facilitator (payments go to your own wallet).

    m = Merchant(api_key="mai-...", pay_to="UQ...your wallet")
    payment = request.headers.get("X-PAYMENT")
    if not payment:
        return 402, m.invoice(amount_nano=50_000_000, resource="/my/endpoint")["x402"]
    s = m.settle(payment)
    if not s.get("success"):
        return 402, {"error": s.get("invalidReason")}
    # serve the request; add header X-PAYMENT-RESPONSE = s["x_payment_response"]

ASGI paywall (FastAPI, Starlette, any ASGI app), one line:

    app = paywall(app, Merchant(api_key, pay_to), {"/v1/weather": 20_000_000})   # path -> price in nano-GRAM

Unpaid requests get HTTP 402 with the invoice; a request with X-PAYMENT is settled once and passed through with
X-PAYMENT-RESPONSE; a payment not yet visible on chain gets 402 with Retry-After so GRAM-402 clients poll.
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


def paywall(app, merchant, prices, description=""):
    """Wrap an ASGI app: requests to the paths in `prices` must be paid in GRAM through the facilitator."""
    import asyncio

    async def send_json(send, status, body, headers=()):
        data = json.dumps(body).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                                *headers]})
        await send({"type": "http.response.body", "body": data})

    async def wrapped(scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") not in prices:
            return await app(scope, receive, send)
        hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        xpay = hdrs.get("x-payment")
        if not xpay:
            inv = await asyncio.to_thread(merchant.invoice, prices[scope["path"]], scope["path"], description)
            if "x402" not in inv:
                return await send_json(send, 503, {"error": "payment service unavailable", "detail": inv.get("invalidReason")})
            return await send_json(send, 402, inv["x402"])
        s = await asyncio.to_thread(merchant.settle, xpay)
        if not s.get("success"):
            if s.get("retry"):
                return await send_json(send, 402, {"error": s.get("invalidReason")}, [(b"retry-after", b"5")])
            return await send_json(send, 402, {"error": s.get("invalidReason") or "payment rejected"})
        resp = s["x_payment_response"].encode()

        async def send2(msg):
            if msg["type"] == "http.response.start":
                msg = {**msg, "headers": [*msg.get("headers", []), (b"x-payment-response", resp)]}
            await send(msg)
        return await app(scope, receive, send2)
    return wrapped
