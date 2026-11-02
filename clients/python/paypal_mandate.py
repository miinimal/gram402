"""Agent-side client for the GRAM-402 PayPal mandate rail (spec: SPEC.md, section "PayPal mandate rail").

    from paypal_mandate import MandateClient
    cli = MandateClient("https://gateway.example", mandate="m_...", secret="...", max_cents=25)
    answer = cli.chat({"model": "echo", "messages": [{"role": "user", "content": "hi"}]})

The owner creates and approves the mandate once (POST /v1/mandates, then the PayPal link); the agent only keeps
`mandate` and `secret`. On HTTP 402 the client picks the `paypal-mandate` offer, refuses prices above `max_cents`,
pays with X-PAYMENT and returns the answer; `last_payment` holds the PayPal capture and the remaining budget.
Stdlib only. Author: Aleksandr Shcherbakov (@miinimal).
"""
import base64
import json
import urllib.error
import urllib.request

SCHEME = "paypal-mandate"


class PaymentError(Exception):
    pass


class MandateClient:
    def __init__(self, base_url, mandate, secret, max_cents=100, opener=None):
        self.base = base_url.rstrip("/")
        self.mandate, self.secret, self.max_cents = mandate, secret, max_cents
        self.last_payment = None
        self._open = opener or urllib.request.urlopen

    def _req(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with self._open(req, timeout=180) as r:
                return r.status, dict(r.headers), json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read() or b"{}")

    def status(self):
        st, _, out = self._req("GET", f"/v1/mandates/{self.mandate}", headers={"X-Mandate-Secret": self.secret})
        if st != 200:
            raise PaymentError(f"HTTP {st}: {out}")
        return out

    def call(self, path, body):
        st, hd, req = self._req("POST", path, body)
        if st != 402:
            return st, req
        acc = next((a for a in req.get("accepts", []) if a.get("scheme") == SCHEME), None)
        if not acc:
            raise PaymentError("server offers no paypal-mandate payment")
        price = int(acc["maxAmountRequired"])
        if price > self.max_cents:
            raise PaymentError(f"price {price} cents exceeds max_cents {self.max_cents}")
        xpay = base64.b64encode(json.dumps({"x402Version": 1, "scheme": SCHEME, "payload": {
            "mandate": self.mandate, "secret": self.secret, "invoice": acc["extra"]["invoice"]}}).encode()).decode()
        st, hd, out = self._req("POST", path, body, {"X-PAYMENT": xpay})
        self.last_payment = (out.get("gram402") or {}).get("payment")
        if self.last_payment is not None:
            self.last_payment = {**self.last_payment, "mandate_left_cents": out["gram402"].get("mandate_left_cents")}
        return st, out

    def chat(self, body):
        st, out = self.call("/v1/chat/completions", body)
        if st != 200:
            raise PaymentError(f"HTTP {st}: {json.dumps(out)[:300]}")
        return out
