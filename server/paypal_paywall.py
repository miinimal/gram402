"""Sell any HTTP API to AI agents for PayPal, one line, with your own PayPal business account.

    from paypal_paywall import paypal_paywall
    app = paypal_paywall(app, {"/v1/weather": 5, "/v1/search": 10})   # path -> price in cents

Environment: PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET (your app at developer.paypal.com), PAYPAL_ENV (sandbox|live),
PUBLIC_URL (where PayPal sends the owner back after approval), PAYWALL_DB (SQLite file, default paypal_paywall.db).

What the wrapper adds to any ASGI app (FastAPI, Starlette, ...):
- the mandate endpoints for agent owners: POST /v1/mandates, GET /v1/mandates/{id}/approved|cancelled,
  GET and DELETE /v1/mandates/{id} (header X-Mandate-Secret);
- on the priced paths: HTTP 402 with a `paypal-mandate` offer when unpaid; with X-PAYMENT the limits are checked,
  the PayPal capture is made, and the request is passed to your app with X-PAYMENT-RESPONSE on the response.
Money goes straight to your PayPal account; nobody sits in between. Author: Aleksandr Shcherbakov (@miinimal).
"""
import json
import os
import re
import sqlite3

import paypal402

_MID = re.compile(r"^/v1/mandates/(m_[0-9a-f]{16})(/approved|/cancelled)?$")


def paypal_paywall(app, prices, db_path=None):
    path_db = db_path or os.environ.get("PAYWALL_DB", "paypal_paywall.db")

    def db():
        c = sqlite3.connect(path_db, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    with db() as c:
        paypal402.setup_db(c)

    async def reply(send, status, body, headers=()):
        data = json.dumps(body).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                                *headers]})
        await send({"type": "http.response.body", "body": data})

    async def read_body(receive):
        chunks, more = [], True
        while more:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        return b"".join(chunks)

    async def mandates(scope, receive, send, hdrs):
        path, method = scope["path"], scope["method"]
        if path == "/v1/mandates" and method == "POST":
            try:
                b = json.loads(await read_body(receive) or b"{}")
            except ValueError:
                return await reply(send, 400, {"error": "body must be JSON"})
            with db() as c:
                m = await paypal402.create_mandate(c, b.get("budget_cents", 500), b.get("per_request_cents", 25),
                                                   b.get("days", 30), b.get("resources"))
            return await reply(send, 200, m)
        mm = _MID.match(path)
        if not mm:
            return await reply(send, 404, {"error": "not found"})
        mid, tail = mm.group(1), mm.group(2)
        if tail == "/approved" and method == "GET":
            with db() as c:
                r = await paypal402.activate(c, mid)
            return await reply(send, 200 if r["ok"] else 400, {"mandate": mid, **r})
        if tail == "/cancelled" and method == "GET":
            return await reply(send, 200, {"mandate": mid, "status": "pending", "message": "approval cancelled"})
        secret = hdrs.get("x-mandate-secret")
        with db() as c:
            if method == "GET" and not tail:
                st = paypal402.status(c, mid, secret)
                return await reply(send, 200, st) if st else await reply(send, 404, {"error": "unknown mandate or wrong secret"})
            if method == "DELETE" and not tail:
                ok = paypal402.revoke(c, mid, secret)
                return await reply(send, 200 if ok else 404, {"mandate": mid, "status": "revoked"} if ok
                                   else {"error": "unknown mandate or wrong secret"})
        return await reply(send, 405, {"error": "method not allowed"})

    async def wrapped(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)
        hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        path = scope.get("path", "")
        if path == "/v1/mandates" or path.startswith("/v1/mandates/"):
            return await mandates(scope, receive, send, hdrs)
        if path not in prices:
            return await app(scope, receive, send)
        xpay = hdrs.get("x-payment")
        claim = paypal402.parse_header(xpay) if xpay else None
        if not claim:
            with db() as c:
                inv = paypal402.create_invoice(c, int(prices[path]), path)
            return await reply(send, 402, {"x402Version": 1, "error": "payment required",
                                           "accepts": [paypal402.accept(inv, path)]})
        with db() as c:
            st = await paypal402.settle(c, claim)
        if not st["ok"]:
            return await reply(send, 402, {"x402Version": 1, "error": st["error"]})
        hdr = paypal402.response_header(st).encode()

        async def send2(msg):
            if msg["type"] == "http.response.start":
                msg = {**msg, "headers": [*msg.get("headers", []), (b"x-payment-response", hdr)]}
            await send(msg)
        return await app(scope, receive, send2)

    return wrapped
