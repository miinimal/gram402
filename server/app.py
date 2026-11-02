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
from fastapi.responses import JSONResponse

import pay402
import receipts

DB = os.environ.get("GRAM402_DB", "gram402.db")
UPSTREAM = os.environ.get("UPSTREAM_URL", "").rstrip("/")
UPSTREAM_KEY = os.environ.get("UPSTREAM_KEY", "")
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
        r = await cli.post(f"{UPSTREAM}/chat/completions", json={**body, "stream": False},
                           headers={"Authorization": f"Bearer {UPSTREAM_KEY}"} if UPSTREAM_KEY else None)
    if r.status_code >= 400:
        raise HTTPException(502, f"upstream {r.status_code}")
    return r.json()


@app.get("/health")
def health():
    return {"ok": True, "gram402": pay402.enabled(), "network": pay402.network_id(), "receipt_key": receipts.public_key_hex()}


@app.post("/v1/receipts/verify")
async def verify(request: Request):
    b = await request.json()
    try:
        return {"valid": bool(receipts.verify(b["receipt"], b.get("request"), b.get("response")))}
    except Exception as e:  # noqa: BLE001
        return {"valid": False, "error": str(e)[:200]}


@app.post("/v1/chat/completions")
async def chat(request: Request, authorization: str = Header(None)):
    body = await request.json()
    settlement = None
    if authorization and authorization.lower().startswith("bearer "):
        key_hash = h(authorization[7:].strip())
    else:
        if not pay402.enabled():
            raise HTTPException(401, "missing bearer key")
        xp = request.headers.get("x-payment")
        if not xp:
            with db() as c:
                inv = pay402.create_invoice(c, pay402.quote_nano(body, cost_nano, 1.0), "/v1/chat/completions")
            return JSONResponse(pay402.requirements(inv, "/v1/chat/completions"), status_code=402)
        claim = pay402.parse_header(xp)
        if not claim:
            return JSONResponse({"x402Version": 1, "error": "malformed X-PAYMENT header"}, status_code=400)
        txs = await pay402.fetch_incoming()
        with db() as c:
            settlement = pay402.settle(c, claim, txs, new_key)
        if not settlement["ok"]:
            return JSONResponse({"x402Version": 1, "error": settlement["error"]}, status_code=402,
                                headers={"Retry-After": "5"} if settlement.get("retry") else None)
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
