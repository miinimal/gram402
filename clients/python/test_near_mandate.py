"""NEAR mandate client against the reference server and a simulated NEAR RPC whose state the pay callback updates.
Run: python3 clients/python/test_near_mandate.py"""
import io
import os
import sys
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "..", "server"), HERE]
os.chdir(os.path.join(HERE, "..", "..", "server"))
import test_near as T  # noqa: E402  (runs the server tests, leaves the app and the simulated contract state)
from near_mandate import NearMandateClient, PaymentError  # noqa: E402

T.RPC["down"] = False
CALLS = []


def pay(contract, method, args):
    """Simulates the agent's signed call: the contract records the payment under the invoice id."""
    assert contract == "mandate.gram402.testnet" and method == "pay"
    CALLS.append(args)
    h = "PYTX%d" % len(CALLS)
    T.TXS[h] = T.make_tx(args["invoice"], args["amount"], merchant=args["merchant"], mandate_id=args["mandate_id"])
    return {"tx_hash": h, "sender": "agent.testnet"}


class Resp:
    def __init__(self, r):
        self.status, self.headers, self._b = r.status_code, r.headers, r.content

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(req, timeout=None):
    r = T.cl.request(req.get_method(), req.full_url.replace("http://test", ""), content=req.data,
                     headers=dict(req.header_items()))
    if r.status_code >= 400:
        raise urllib.error.HTTPError(req.full_url, r.status_code, "", r.headers, io.BytesIO(r.content))
    return Resp(r)


agent = NearMandateClient("http://test", mandate_id=7, pay=pay, max_yocto=10**21, poll_s=0, opener=opener)
out = agent.chat(T.REQ)
assert out["choices"][0]["message"]["content"].startswith("echo:")
assert CALLS[-1]["mandate_id"] == 7 and agent.last_payment["network"] == "near-testnet"
try:
    NearMandateClient("http://test", 7, pay, max_yocto=1, opener=opener).chat(T.REQ)
    raise AssertionError("price cap not enforced")
except PaymentError as e:
    assert "max_yocto" in str(e)
print("near mandate client OK")
