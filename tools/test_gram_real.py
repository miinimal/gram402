"""Offline test of tools/gram_real.py against the real server app (mocked TON chain). Run: python3 tools/test_gram_real.py"""
import asyncio
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update({"GRAM402_DB": tempfile.mktemp(suffix=".db"), "PAY402_NETWORK": "mainnet",
                   "PAY402_ADDRESS": "UQDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVbRe",
                   "PAY402_MIN_NANO": "200000000", "PRICE_NANO_PER_1K": "1"})
sys.path[:0] = [HERE, os.path.join(HERE, "..", "server")]

import httpx  # noqa: E402

import app as A  # noqa: E402
import gram_real as G  # noqa: E402
import pay402  # noqa: E402

PAYER = "0:3C02D2FAE2B263048DA2C03AAF0C2BEB208F6828C3547D4A86E19A98F1BFAABA"
CHAIN = []


async def fake_fetch(client=None, limit=100):
    return list(CHAIN)


pay402.fetch_incoming = fake_fetch


def tx(h, comment, value):
    return {"hash": h, "description": {"aborted": False, "compute_ph": {"success": True}},
            "in_msg": {"source": PAYER, "value": str(value),
                       "message_content": {"decoded": {"type": "text_comment", "comment": comment}}}}


class Clock:
    def __init__(self):
        self.t = time.time()

    def now(self):
        return self.t


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app), base_url="http://test")


async def scenario(pay_after_polls, value_factor, max_wait_s=600):
    summary = tempfile.mktemp(suffix=".md")
    clock, polls = Clock(), {"n": 0}
    invoice = {}

    async def sleep(s):
        clock.t += s
        polls["n"] += 1
        if polls["n"] == pay_after_polls:
            text = open(summary, encoding="utf-8").read()
            comment = text.split("Comment (must be exact): `")[1].split("`")[0]
            amount = float(text.split("Amount: **")[1].split(" GRAM")[0])
            invoice["comment"] = comment
            CHAIN.append(tx("tx-" + comment, comment, int(round(amount * 1e9 * value_factor))))

    async with client() as c:
        code = await G.run(c, max_wait_s, poll=15, summary=summary, now=clock.now, sleep=sleep)
    return code, open(summary, encoding="utf-8").read(), polls["n"]


async def main():
    # paid on the third poll: exit 0, receipt valid, the exact amount and comment were shown
    code, text, n = await scenario(3, 1.0)
    assert code == 0, text
    assert "Receipt valid: True" in text and "Amount: **0.2 GRAM**" in text and "ton-mainnet" in text, text
    assert "ton://transfer/UQDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVbRe?amount=200000000&text=mai%3A" in text, text
    assert n == 3

    # never paid: exit 2 after the wait, nothing charged
    code, text, n = await scenario(10 ** 6, 1.0, max_wait_s=60)
    assert code == 2 and "nothing was charged" in text, text
    assert n == 4  # 0, 15, 30, 45 s of waiting, stop at 60 s

    # paid below the price: exit 3, the server says what it credited
    code, text, _ = await scenario(1, 0.5)
    assert code == 3 and "below the price" in text, text

    # the link is a valid ton:// transfer link
    assert G.transfer_link("EQabc", 5, "mai:x y") == "ton://transfer/EQabc?amount=5&text=mai%3Ax+y"
    # raw -> user-friendly address (checked against the testnet address used in server/test_app.py)
    raw = "0:c897a712702793569e462e66dbc61f1b9502ef45777ed09e13d5f56ccfbb8455"
    assert G.friendly_address(raw, testnet=True) == "0QDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVQ_U"
    assert G.friendly_address(raw) == "UQDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVbRe"
    print("gram_real OK")


asyncio.run(main())
