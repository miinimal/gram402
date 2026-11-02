"""Python client against the reference server in-process (mocked chain).  Run: python3 clients/python/test_gram402.py"""
import io
import os
import sys
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "server")); sys.path.insert(0, HERE)
import test_app as T  # noqa: E402  (env, mocked chain)
from fastapi.testclient import TestClient  # noqa: E402
from gram402 import Gram402Client, PaymentError  # noqa: E402


class _Resp(io.BytesIO):
    def __init__(self, r):
        super().__init__(r.content); self.status, self.headers = r.status_code, r.headers
    def __enter__(self): return self
    def __exit__(self, *a): pass


def opener_for(c):
    def opener(req, timeout=0):
        r = c.post(req.full_url.split("http://gw", 1)[1], content=req.data, headers=dict(req.header_items()))
        if r.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, r.status_code, "", r.headers, io.BytesIO(r.content))
        return _Resp(r)
    return opener


paid = []
def pay(to, nano, comment):
    paid.append(nano); T.CHAIN.append(T.tx("py%d" % len(paid), comment, nano * 4))


c = TestClient(T.A.app)
cli = Gram402Client("http://gw", pay, poll_s=0, opener=opener_for(c))
assert cli.chat(T.REQ)["choices"][0]["message"]["content"] == "echo: hello" and len(paid) == 1 and cli.api_key
cli.chat(T.REQ); assert len(paid) == 1
try:
    Gram402Client("http://gw", pay, max_nano=1, opener=opener_for(c)).chat(T.REQ); raise AssertionError("cap")
except PaymentError as e:
    assert "exceeds max_nano" in str(e)
print("python client OK")
