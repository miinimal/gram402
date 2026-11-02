"""Merchant helper test against a local stub facilitator. Run: python3 clients/python/test_gram402_merchant.py"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from gram402_merchant import Merchant  # noqa: E402

CALLS = []


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        CALLS.append((self.path, self.headers["Authorization"], body))
        if self.headers["Authorization"] != "Bearer k":
            self.send_response(401); self.end_headers(); self.wfile.write(b'{"detail":"invalid key"}'); return
        out = {"/v1/facilitator/invoices": {"invoice": "i1", "x402": {"x402Version": 1, "accepts": [{"payTo": body.get("pay_to")}]}},
               "/v1/facilitator/settle": {"success": True, "transaction": "t1", "x_payment_response": "e30="},
               "/v1/facilitator/verify": {"isValid": True}}[self.path]
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)


srv = HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
m = Merchant("k", "UQmerchant", base=base)
inv = m.invoice(50_000_000, "/x")
assert inv["x402"]["accepts"][0]["payTo"] == "UQmerchant" and CALLS[-1][2]["amount_nano"] == 50_000_000
assert m.verify("hdr")["isValid"] and CALLS[-1][2] == {"payment": "hdr"}
assert m.settle("hdr")["transaction"] == "t1"
bad = Merchant("wrong", "UQmerchant", base=base).settle("hdr")
assert bad["success"] is False and bad["status"] == 401
srv.shutdown()
print("merchant OK")
