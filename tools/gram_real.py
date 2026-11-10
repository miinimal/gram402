"""One real GRAM payment through GRAM-402 on TON mainnet, with the owner paying from his own wallet.

Used by the CI workflow gram-real (never by the hosted demo). The server runs inside the job with
PAY402_NETWORK=mainnet and PAY402_ADDRESS = the owner's own receiving address, so the money goes from his phone wallet
to his own address: the real cost is only the network fee.

Steps:
1. Ask the paid endpoint without payment -> HTTP 402 with a ton-transfer invoice (payTo, amount, comment mai:<id>).
2. Print what the owner sends: address, amount in GRAM, the exact comment, and a ton://transfer link that pre-fills
   a TON wallet. Also written to the GitHub step summary when it runs in Actions.
3. Repeat the request with X-PAYMENT every POLL seconds until the transfer is on chain, the invoice expires or
   --max-wait-min passes. On success: print the transaction hash, payer and the signed receipt, then check the receipt
   with /v1/receipts/verify.

Exit codes: 0 paid and receipt valid; 1 server answered something unexpected or the receipt is invalid;
2 no payment arrived in time (nothing was charged; a later transfer still lands on the owner's own address);
3 the transfer arrived but was below the price (the server credited it to a key, printed below).
Author: Aleksandr Shcherbakov (@miinimal).
"""
import argparse
import asyncio
import base64
import json
import os
import sys
import time
import urllib.parse

import httpx

REQ = {"model": "echo", "max_tokens": 16, "messages": [{"role": "user", "content": "gram402 real payment check"}]}
POLL = 15


def xpayment(invoice, secret, network):
    return base64.b64encode(json.dumps({"x402Version": 1, "scheme": "ton-transfer", "network": network,
                                        "payload": {"invoice": invoice, "secret": secret}}).encode()).decode()


def transfer_link(pay_to, amount_nano, comment):
    return "ton://transfer/" + pay_to + "?" + urllib.parse.urlencode({"amount": str(amount_nano), "text": comment})


def _crc16(data):
    reg = 0
    for byte in data + b"\x00\x00":
        for i in range(8):
            reg = (reg << 1) | (1 if byte & (0x80 >> i) else 0)
            if reg > 0xFFFF:
                reg = (reg & 0xFFFF) ^ 0x1021
    return reg.to_bytes(2, "big")


def friendly_address(raw, bounceable=False, testnet=False):
    """'0:<64 hex>' -> user-friendly base64url address (non-bounceable UQ... by default, as wallets show it)."""
    wc, hexpart = raw.split(":")
    body = bytes([(0x11 if bounceable else 0x51) | (0x80 if testnet else 0), int(wc) & 0xFF]) + bytes.fromhex(hexpart)
    return base64.urlsafe_b64encode(body + _crc16(body)).decode()


def gram(nano):
    return f"{int(nano) / 1e9:.9f}".rstrip("0").rstrip(".")


def say(lines, summary=None):
    text = "\n".join(lines)
    print(text, flush=True)
    path = summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n\n")


async def run(client, max_wait_s, poll=POLL, summary=None, now=time.time, sleep=asyncio.sleep):
    r = await client.post("/v1/chat/completions", json=REQ)
    if r.status_code != 402:
        say([f"unexpected answer to the unpaid request: HTTP {r.status_code} {r.text[:300]}"], summary)
        return 1
    offers = [a for a in r.json().get("accepts", []) if a.get("scheme") == "ton-transfer"]
    if not offers:
        say(["the server offered no ton-transfer payment (is PAY402_ADDRESS set?)"], summary)
        return 1
    acc = offers[0]
    ex = acc["extra"]
    amount = int(acc["maxAmountRequired"])
    deadline = min(now() + max_wait_s, float(ex.get("expiresAt") or now() + max_wait_s))
    say(["## Send this payment from your TON wallet",
         f"- Network: {acc['network']}",
         f"- To address: `{acc['payTo']}`",
         f"- Amount: **{gram(amount)} GRAM** (exactly this or more)",
         f"- Comment (must be exact): `{ex['comment']}`",
         f"- Link that pre-fills a wallet: {transfer_link(acc['payTo'], amount, ex['comment'])}",
         f"- Waiting until {time.strftime('%H:%M:%S UTC', time.gmtime(deadline))}"], summary)
    header = {"X-PAYMENT": xpayment(ex["invoice"], ex["secret"], acc["network"])}
    while True:
        r = await client.post("/v1/chat/completions", json=REQ, headers=header)
        if r.status_code == 200:
            body = r.json()
            g = body["gram402"]
            v = (await client.post("/v1/receipts/verify",
                                   json={"receipt": g["receipt"], "request": REQ, "response": body})).json()
            say(["## Paid",
                 f"- Transaction: `{g['payment']['transaction']}`",
                 f"- Payer: `{g['payment']['payer']}`",
                 f"- Paid: {gram(g['payment']['paid_nano'])} GRAM, charged {gram(g['charged_nano'])} GRAM, "
                 f"credit left {gram(g['credit_left_nano'])} GRAM",
                 f"- Receipt valid: {v.get('valid')}",
                 "```json", json.dumps(g["receipt"], indent=1), "```"], summary)
            return 0 if v.get("valid") else 1
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 402 and data.get("underpaid"):
            say([f"the transfer arrived below the price: {json.dumps(data, indent=1)}"], summary)
            return 3
        if r.status_code not in (402, 429):
            say([f"unexpected answer while waiting: HTTP {r.status_code} {r.text[:300]}"], summary)
            return 1
        if now() >= deadline:
            say(["no payment arrived in time; nothing was charged. A transfer sent later still lands on the "
                 "receiving address above."], summary)
            return 2
        await sleep(poll)


async def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--server", default="http://127.0.0.1:8402")
    ap.add_argument("--max-wait-min", type=float, default=60)
    ap.add_argument("--friendly", metavar="RAW", help="only print the user-friendly form of a raw 0:<hex> address")
    a = ap.parse_args()
    if a.friendly:
        print(friendly_address(a.friendly))
        return 0
    async with httpx.AsyncClient(base_url=a.server, timeout=60) as client:
        return await run(client, a.max_wait_min * 60)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
