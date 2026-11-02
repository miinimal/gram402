"""Agent-side client for the GRAM-402 NEAR mandate rail (scheme "near-mandate").

    from near_mandate import NearMandateClient
    def pay(contract, method, args):            # sign with the agent's NEAR key (any library or near-cli-rs)
        ...                                      # call `method` on `contract` with JSON `args`, 0 deposit, 30 Tgas
        return {"tx_hash": tx_hash, "sender": agent_account}
    agent = NearMandateClient("https://api.example", mandate_id=1, pay=pay, contract="mandate.example.near",
                              max_yocto=10**22)
    answer = agent.chat({"model": "default", "messages": [{"role": "user", "content": "hi"}]})

On HTTP 402 the client takes the near-mandate offer, refuses prices above `max_yocto`, calls `pay` on the mandate
contract through your callback, then retries with X-PAYMENT until the server sees the payment on chain.
The contract enforces the owner's limits; the client only adds its own price cap. Stdlib only.
Author: Aleksandr Shcherbakov (@miinimal).
"""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

SCHEME = "near-mandate"


class PaymentError(Exception):
    pass


class NearMandateClient:
    def __init__(self, base_url, mandate_id, pay, contract, max_yocto=10**22, wait_s=60, poll_s=3, opener=None):
        """`contract`: the mandate contract this agent uses (from its own configuration, never from the server)."""
        self.base = base_url.rstrip("/")
        self.contract = contract
        self.mandate_id, self.pay, self.max_yocto = int(mandate_id), pay, int(max_yocto)
        self.wait_s, self.poll_s = wait_s, poll_s
        self.last_payment = None
        self._open = opener or urllib.request.urlopen

    def _post(self, path, body, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with self._open(req, timeout=120) as r:
                return r.status, dict(r.headers), json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read() or b"{}")

    def _safe(self, path):
        """Paths come from the agent (possibly from the model): never let them change the host the secret goes to."""
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise PaymentError("path must be an absolute path on the configured server")
        if urllib.parse.urlsplit(self.base + path).netloc != urllib.parse.urlsplit(self.base).netloc:
            raise PaymentError("path changes the host")
        return path

    def call(self, path, body):
        path = self._safe(path)
        st, hd, req = self._post(path, body)
        if st != 402:
            return st, req
        acc = next((a for a in req.get("accepts", []) if a.get("scheme") == SCHEME), None)
        if not acc:
            raise PaymentError("server offers no near-mandate payment")
        price = int(acc["maxAmountRequired"])
        if price > self.max_yocto:
            raise PaymentError(f"price {price} yoctoNEAR exceeds max_yocto {self.max_yocto}")
        ex = acc["extra"]
        if ex.get("contract") != self.contract:
            raise PaymentError(f"offer names contract {ex.get('contract')!r}, configured {self.contract!r}")
        # Arguments are built here, not taken from the server: our mandate, the advertised merchant, the checked price.
        args = {"mandate_id": self.mandate_id, "merchant": str(acc["payTo"]), "amount": str(price),
                "invoice": str(ex["invoice"])}
        tx = self.pay(self.contract, "pay", args) or {}
        if not tx.get("tx_hash") or not tx.get("sender"):
            raise PaymentError("pay callback must return {'tx_hash', 'sender'} of the pay() transaction")
        xpay = base64.b64encode(json.dumps({"x402Version": 1, "scheme": SCHEME, "payload": {
            "invoice": ex["invoice"], "secret": ex["secret"], "tx_hash": tx["tx_hash"],
            "sender": tx["sender"]}}).encode()).decode()
        deadline = time.time() + self.wait_s
        while True:
            st, hd, out = self._post(path, body, {"X-PAYMENT": xpay})
            if st == 402 and any(k.lower() == "retry-after" for k in hd) and time.time() < deadline:
                time.sleep(self.poll_s)
                continue
            g = out.get("gram402") if isinstance(out, dict) else None
            self.last_payment = (g or {}).get("payment")
            return st, out

    def chat(self, body):
        st, out = self.call("/v1/chat/completions", body)
        if st != 200:
            raise PaymentError(f"HTTP {st}: {json.dumps(out)[:300]}")
        return out
