"""GRAM-402 reference server: any OpenAI-compatible chat endpoint, paid per request in GRAM by AI agents.

Run:   PAY402_ADDRESS=<your TON wallet> PAY402_NETWORK=testnet uvicorn app:app --port 8402
       optional: UPSTREAM_URL=https://api.example/v1  UPSTREAM_KEY=...  (otherwise a built-in echo model answers)
       PRICE_NANO_PER_1K=200000 (price per 1000 tokens, nano-GRAM), MIN_NANO via PAY402_MIN_NANO.

Flow: POST /v1/chat/completions without Authorization -> 402 invoice; GRAM transfer with the invoice comment;
repeat with X-PAYMENT -> answer + signed receipt bound to the payment; overpayment stays as credit spent with the
returned api_key (Authorization: Bearer). Spec: ../SPEC.md. Author: Aleksandr Shcherbakov (@miinimal).
"""
import hashlib
import os
import secrets
import sqlite3
import time

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import pay402
import near402
import paypal402
import receipts
import throttle

DB = os.environ.get("GRAM402_DB", "gram402.db")
UPSTREAM = os.environ.get("UPSTREAM_URL", "").rstrip("/")
UPSTREAM_KEY = os.environ.get("UPSTREAM_KEY", "")
# Optional pins for a public deployment: one model, a reply-length cap, and only the request fields below go upstream,
# so a caller cannot pick an expensive model or long output on the operator's key.
UPSTREAM_MODEL = os.environ.get("UPSTREAM_MODEL", "").strip()
UPSTREAM_MAX_TOKENS = int(os.environ.get("UPSTREAM_MAX_TOKENS", "0") or 0)
UPSTREAM_FIELDS = ("model", "messages", "max_tokens", "temperature", "stop", "tools", "tool_choice")


def upstream_body(body):
    if not UPSTREAM_MODEL:
        return {**body, "stream": False}
    out = {k: body[k] for k in UPSTREAM_FIELDS if k in body}
    out["model"] = UPSTREAM_MODEL
    if UPSTREAM_MAX_TOKENS:
        asked = out.get("max_tokens")
        out["max_tokens"] = min(int(asked), UPSTREAM_MAX_TOKENS) if isinstance(asked, int) and asked > 0 else UPSTREAM_MAX_TOKENS
    out["stream"] = False
    return out
PRICE_1K = int(os.environ.get("PRICE_NANO_PER_1K", "200000"))

app = FastAPI(title="GRAM-402 reference server")


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def h(k):
    return hashlib.sha256(k.encode()).hexdigest()


with db() as _c:
    _c.execute("CREATE TABLE IF NOT EXISTS keys(key_hash TEXT PRIMARY KEY, owner TEXT, created INTEGER, balance_nano INTEGER)")
    pay402.setup_db(_c)
    paypal402.setup_db(_c)
    near402.setup_db(_c)


def cost_nano(u):
    return (u["prompt"] + u["completion"] + u["reasoning"]) * PRICE_1K // 1000


def new_key(c, payer, value, tx_hash):
    key = "g402-" + secrets.token_urlsafe(32)
    c.execute("INSERT INTO keys VALUES(?,?,?,?)", (h(key), payer, int(time.time()), value))
    return key, h(key)


async def answer(body):
    if not UPSTREAM:  # built-in echo model, for tests and demos
        text = "echo: " + str((body.get("messages") or [{}])[-1].get("content", ""))[:200]
        return {"id": "g402-" + secrets.token_hex(6), "object": "chat.completion", "created": int(time.time()),
                "model": body.get("model", "echo"), "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": len(str(body.get("messages"))) // 4, "completion_tokens": len(text) // 4}}
    async with httpx.AsyncClient(timeout=180) as cli:
        r = await cli.post(f"{UPSTREAM}/chat/completions", json=upstream_body(body),
                           headers={"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else None)
    if r.status_code >= 400:
        raise HTTPException(502, f"upstream {r.status_code}")
    return r.json()


@app.get("/health")
def health():
    return {"ok": True, "gram402": pay402.enabled(), "network": pay402.network_id(),
            "paypal": paypal402.enabled() and paypal402.network_id(),
            "near": near402.enabled() and near402.network_id(), "receipt_key": receipts.public_key_hex()}


@app.post("/v1/mandates")
async def mandate_create(request: Request):
    """Owner side: create a PayPal spending mandate for an agent. Returns the secret once and the approval link."""
    if not paypal402.enabled():
        raise HTTPException(404, "PayPal rail is not configured")
    b = await request.json()
    try:
        with db() as c:
            return await paypal402.create_mandate(c, b.get("budget_cents", 500), b.get("per_request_cents", 25),
                                                  b.get("days", 30), b.get("resources"),
                                                  ip=_ip(request))
    except paypal402.RateLimited as e:
        raise HTTPException(429, str(e))


@app.on_event("startup")
async def _reconcile_loop():
    """Every 5 minutes: settle or release PayPal invoices left in 'paying' by agents that never came back."""
    import asyncio

    async def loop():
        while True:
            await asyncio.sleep(300)
            if paypal402.enabled():
                try:
                    with db() as c:
                        await paypal402.reconcile(c)
                except Exception:  # noqa: BLE001
                    pass
    asyncio.get_event_loop().create_task(loop())


OWNER_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "owner.html")


@app.get("/mandates", response_class=HTMLResponse)
def owner_page():
    """Owner UI: create a mandate, approve it on PayPal, watch spending, revoke."""
    return HTMLResponse(open(OWNER_PAGE, encoding="utf-8").read())


def _result_page(title, text, ok):
    color = "#16834a" if ok else "#5d6e7e"
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><body style="margin:0;font:16px/1.5 system-ui,sans-serif;background:#f2f5f8;color:#0f1d2b">
<main style="max-width:480px;margin:15vh auto 0;padding:0 16px"><h1 style="color:{color};font-size:24px">{title}</h1>
<p>{text}</p><p><a href="{paypal402.PUBLIC_URL}/mandates" style="color:#0070e0">Check spending or revoke</a></p></main></body>""")


def _wants_html(request):
    return "text/html" in (request.headers.get("accept") or "")


@app.get("/v1/mandates/{mid}/approved")
async def mandate_approved(mid: str, request: Request):
    with db() as c:
        r = await paypal402.activate(c, mid)
    if not r["ok"]:
        if _wants_html(request):
            return _result_page("Mandate not activated", "PayPal approval could not be confirmed: " + r["error"], False)
        raise HTTPException(400, r["error"])
    msg = "Mandate approved. Your agent can now pay within its limits."
    if _wants_html(request):
        return _result_page("Mandate approved", msg + " You can return to your agent.", True)
    return {"mandate": mid, "status": "active", "message": msg}


@app.get("/v1/mandates/{mid}/cancelled")
def mandate_cancelled(mid: str, request: Request):
    msg = "Approval cancelled on PayPal; nothing was charged."
    if _wants_html(request):
        return _result_page("Approval cancelled", msg, False)
    return {"mandate": mid, "status": "pending", "message": msg}


@app.get("/v1/mandates/{mid}")
async def mandate_status(mid: str, x_mandate_secret: str = Header(None)):
    with db() as c:
        r = await paypal402.refresh(c, mid, x_mandate_secret)
    if not r:
        raise HTTPException(404, "unknown mandate or wrong secret")
    return r


@app.delete("/v1/mandates/{mid}")
def mandate_revoke(mid: str, x_mandate_secret: str = Header(None)):
    with db() as c:
        if not paypal402.revoke(c, mid, x_mandate_secret):
            raise HTTPException(404, "unknown mandate or wrong secret")
    return {"mandate": mid, "status": "revoked"}


@app.post("/v1/receipts/verify")
async def verify(request: Request):
    b = await request.json()
    try:
        return {"valid": bool(receipts.verify(b["receipt"], b.get("request"), b.get("response")))}
    except Exception as e:  # noqa: BLE001
        return {"valid": False, "error": str(e)[:200]}


def _ip(request):
    return throttle.client_ip(request.client.host if request.client else None, request.headers)


@app.post("/v1/chat/completions")
async def chat(request: Request, authorization: str = Header(None)):
    body = await request.json()
    settlement = None
    if authorization and authorization.lower().startswith("bearer "):
        key_hash = h(authorization[7:].strip())
    else:
        if not pay402.enabled() and not paypal402.enabled() and not near402.enabled():
            raise HTTPException(401, "missing bearer key")
        xp = request.headers.get("x-payment")
        if xp and not throttle.allow(_ip(request), kind="pay"):
            return JSONResponse({"error": "too many payment attempts from this address; slow down"}, status_code=429,
                                headers={"Retry-After": "60"})
        if not xp:
            if not throttle.allow(_ip(request)):
                return JSONResponse({"error": "too many unpaid requests from this address; slow down"}, status_code=429,
                                    headers={"Retry-After": "60"})
            res = "/v1/chat/completions"
            with db() as c:
                req = {"x402Version": 1, "error": "payment required", "accepts": []}
                if pay402.enabled():
                    req = pay402.requirements(pay402.create_invoice(c, pay402.quote_nano(body, cost_nano, 1.0), res), res)
                if paypal402.enabled():
                    req["accepts"].append(paypal402.accept(
                        paypal402.create_invoice(c, paypal402.PRICE_CENTS, res), res))
                if near402.enabled():
                    req["accepts"].append(near402.accept(near402.create_invoice(c, near402.PRICE_YOCTO, res), res))
            return JSONResponse(req, status_code=402)
        pclaim = paypal402.parse_header(xp) if paypal402.enabled() else None
        if pclaim:
            return await chat_paypal(body, pclaim)
        nclaim = near402.parse_header(xp) if near402.enabled() else None
        if nclaim:
            return await chat_near(body, nclaim)
        claim = pay402.parse_header(xp)
        if not claim:
            return JSONResponse({"x402Version": 1, "error": "malformed X-PAYMENT header"}, status_code=400)
        txs = await pay402.fetch_incoming()
        with db() as c:
            settlement = pay402.settle(c, claim, txs, new_key)
        if not settlement["ok"]:
            extra = {k: settlement[k] for k in ("api_key", "credit_nano", "tx_hash", "payer") if settlement.get("underpaid") and k in settlement}
            return JSONResponse({"x402Version": 1, "error": settlement["error"], **({"underpaid": extra} if extra else {})},
                                status_code=402, headers={"Retry-After": "5"} if settlement.get("retry") else None)
        key_hash = settlement["key_hash"]
    row = db().execute("SELECT balance_nano FROM keys WHERE key_hash=?", (key_hash,)).fetchone()
    if not row:
        raise HTTPException(401, "invalid key")
    if row["balance_nano"] <= 0:
        return JSONResponse({"x402Version": 1, "error": "credit spent: repeat without Authorization to pay again"},
                            status_code=402)
    try:
        resp = await answer(body)
    except HTTPException as e:
        if not settlement:
            raise
        return JSONResponse({"error": {"message": e.detail}, "payment": {"api_key": settlement["api_key"],
                             "credit_left_nano": row["balance_nano"], "transaction": settlement["tx_hash"]}},
                            status_code=e.status_code)
    u = resp.get("usage") or {}
    usage = {"prompt": int(u.get("prompt_tokens") or 0), "completion": int(u.get("completion_tokens") or 0),
             "reasoning": int(((u.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)}
    charged = min(cost_nano(usage), row["balance_nano"])
    with db() as c:
        c.execute("UPDATE keys SET balance_nano=balance_nano-? WHERE key_hash=?", (charged, key_hash))
        left = c.execute("SELECT balance_nano FROM keys WHERE key_hash=?", (key_hash,)).fetchone()[0]
    pay = None
    if settlement:
        pay = {"network": settlement["network"], "transaction": settlement["tx_hash"], "payer": settlement["payer"],
               "charged_nano": charged}
    resp["gram402"] = {"charged_nano": charged, "credit_left_nano": left,
                       "receipt": receipts.make(body, resp, "upstream" if UPSTREAM else "echo", None, pay)}
    headers = {}
    if settlement:
        resp["gram402"]["payment"] = {"transaction": settlement["tx_hash"], "payer": settlement["payer"],
                                      "paid_nano": settlement["paid_nano"], "api_key": settlement["api_key"]}
        headers["X-PAYMENT-RESPONSE"] = pay402.response_header(settlement, charged, left)
    return JSONResponse(resp, headers=headers)


async def chat_paypal(body, claim):
    """Pay-per-request from a PayPal mandate: capture first, then answer; the receipt binds the PayPal capture."""
    with db() as c:
        st = await paypal402.settle(c, claim, expect_resource="/v1/chat/completions",
                                    expect_cents=paypal402.PRICE_CENTS)
    if not st["ok"]:
        return JSONResponse({"x402Version": 1, "error": st["error"]}, status_code=402,
                            headers={"Retry-After": "5"} if st.get("retry") else None)
    pay = {"network": st["network"], "transaction": st["transaction"], "payer": st["payer"], "charged_nano": None,
           "charged_cents": st["charged_cents"], "currency": paypal402.CURRENCY}
    try:
        resp = await answer(body)
    except Exception as e:  # noqa: BLE001  (HTTP error, timeout, connection failure: not served -> refund)
        with db() as c:
            rf = await paypal402.refund(c, st)
        code = e.status_code if isinstance(e, HTTPException) else 502
        detail = e.detail if isinstance(e, HTTPException) else "upstream unavailable"
        return JSONResponse({"error": {"message": detail}, "payment": pay, "refund": rf}, status_code=code)
    resp["gram402"] = {"charged_cents": st["charged_cents"], "mandate_left_cents": st["mandate_left_cents"],
                       "receipt": receipts.make(body, resp, "upstream" if UPSTREAM else "echo", None, pay),
                       "payment": pay}
    return JSONResponse(resp, headers={"X-PAYMENT-RESPONSE": paypal402.response_header(st)})


async def chat_near(body, claim):
    """Pay-per-request through the NEAR mandate contract: verify the on-chain payment, then answer with a receipt."""
    with db() as c:
        st = await near402.settle(c, claim)
    if not st["ok"]:
        return JSONResponse({"x402Version": 1, "error": st["error"]}, status_code=402,
                            headers={"Retry-After": "3"} if st.get("retry") else None)
    pay = {"network": st["network"], "transaction": st["transaction"], "payer": st["payer"], "charged_nano": None,
           "charged_yocto": str(st["charged_yocto"])}
    try:
        resp = await answer(body)
    except Exception as e:  # noqa: BLE001
        with db() as c:
            near402.credit(c, st["invoice"])
        code = e.status_code if isinstance(e, HTTPException) else 502
        return JSONResponse({"error": {"message": e.detail if isinstance(e, HTTPException) else "upstream unavailable"},
                             "payment": pay,
                             "credit": "repeat the same X-PAYMENT once to get the answer without paying again"},
                            status_code=code)
    resp["gram402"] = {"receipt": receipts.make(body, resp, "upstream" if UPSTREAM else "echo", None, pay),
                       "payment": pay}
    return JSONResponse(resp, headers={"X-PAYMENT-RESPONSE": near402.response_header(st)})
