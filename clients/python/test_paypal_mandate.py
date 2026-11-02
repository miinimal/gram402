"""Agent client against the reference server with a fake PayPal API. Run: python3 clients/python/test_paypal_mandate.py"""
import io
import os
import sys
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "..", "server"), HERE]
os.chdir(os.path.join(HERE, "..", "..", "server"))
import test_paypal as T  # noqa: E402  (runs the server tests, leaves a configured app and fake PayPal)
from paypal_mandate import MandateClient, PaymentError  # noqa: E402


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
    path = req.full_url.replace("http://test", "")
    r = T.cl.request(req.get_method(), path, content=req.data, headers=dict(req.header_items()))
    if r.status_code >= 400:
        raise urllib.error.HTTPError(req.full_url, r.status_code, "", r.headers, io.BytesIO(r.content))
    return Resp(r)


m = T.cl.post("/v1/mandates", json={"budget_cents": 30, "per_request_cents": 10, "days": 1}).json()
T.cl.get(f"/v1/mandates/{m['mandate']}/approved")
agent = MandateClient("http://test", m["mandate"], m["secret"], max_cents=10, opener=opener)
out = agent.chat(T.REQ)
assert out["choices"][0]["message"]["content"].startswith("echo:")
assert agent.last_payment["transaction"].startswith("CAP") and agent.last_payment["mandate_left_cents"] == 20
assert agent.status()["spent_cents"] == 10
try:
    MandateClient("http://test", m["mandate"], m["secret"], max_cents=5, opener=opener).chat(T.REQ)
    raise AssertionError("price cap not enforced")
except PaymentError as e:
    assert "max_cents" in str(e)
print("paypal mandate client OK")
