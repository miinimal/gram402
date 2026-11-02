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
import secrets
import sqlite3

import paypal402
import throttle

_MID = re.compile(r"^/v1/mandates/(m_[0-9a-f]{16})(/approved|/cancelled)?$")


def _price(p):
    """A price is cents (int) or {"cents": .., "nano": ..} to sell on both rails."""
    return (p, None) if isinstance(p, int) else (p.get("cents"), p.get("nano"))


def paypal_paywall(app, prices, db_path=None, gram=None, description=""):
    """`gram`: optional gram402_merchant.Merchant; then a path priced {"cents": c, "nano": n} offers both rails."""
    import asyncio
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
            try:
                client = scope.get("client") or (None,)
                with db() as c:
                    m = await paypal402.create_mandate(c, b.get("budget_cents", 500), b.get("per_request_cents", 25),
                                                       b.get("days", 30), b.get("resources"),
                                                       ip=throttle.client_ip(client[0], hdrs))
            except paypal402.RateLimited as e:
                return await reply(send, 429, {"error": str(e)})
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
        cents, nano = _price(prices[path])
        xpay = hdrs.get("x-payment")
        claim = paypal402.parse_header(xpay) if xpay else None
        ip = throttle.client_ip((scope.get("client") or (None,))[0], hdrs)
        if xpay and not throttle.allow(ip, kind="pay"):
            return await reply(send, 429, {"error": "too many payment attempts from this address; slow down"},
                               [(b"retry-after", b"60")])
        if not xpay:
            if not throttle.allow(ip):
                return await reply(send, 429, {"error": "too many unpaid requests from this address; slow down"},
                                   [(b"retry-after", b"60")])
            accepts = []
            if gram is not None and nano:
                inv = await asyncio.to_thread(gram.invoice, nano, path, description)
                accepts += (inv.get("x402") or {}).get("accepts", [])
            if cents:
                with db() as c:
                    accepts.append(paypal402.accept(paypal402.create_invoice(c, int(cents), path), path))
                    if secrets.randbelow(50) == 0:  # sweep invoices abandoned mid-payment
                        try:
                            await paypal402.reconcile(c)
                        except Exception:  # noqa: BLE001
                            pass
            if not accepts:
                return await reply(send, 503, {"error": "payment service unavailable"})
            return await reply(send, 402, {"x402Version": 1, "error": "payment required", "accepts": accepts})
        st = None
        if claim:
            if not cents:
                return await reply(send, 402, {"x402Version": 1, "error": "this path is not sold for PayPal"})
            with db() as c:
                st = await paypal402.settle(c, claim, expect_resource=path, expect_cents=int(cents))
            if not st["ok"]:
                return await reply(send, 402, {"x402Version": 1, "error": st["error"]},
                                   [(b"retry-after", b"5")] if st.get("retry") else [])
            hdr = paypal402.response_header(st).encode()
        elif gram is not None and nano:
            s = await asyncio.to_thread(gram.settle, xpay)
            if not s.get("success"):
                extra = [(b"retry-after", b"5")] if s.get("retry") else []
                return await reply(send, 402, {"x402Version": 1, "error": s.get("invalidReason") or "payment rejected"},
                                   extra)
            hdr = s["x_payment_response"].encode()
        else:
            return await reply(send, 402, {"x402Version": 1, "error": "unsupported payment scheme"})

        status = {"code": 200}

        async def send2(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg.get("status", 200)
                msg = {**msg, "headers": [*msg.get("headers", []), (b"x-payment-response", hdr)]}
            await send(msg)
        try:
            await app(scope, receive, send2)
        except Exception:
            status["code"] = 500
            raise
        finally:
            if st and status["code"] >= 500:  # paid but not served: give the money back
                with db() as c:
                    await paypal402.refund(c, st)

    return wrapped
