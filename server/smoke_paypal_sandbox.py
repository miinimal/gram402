"""Live check against the PayPal sandbox: OAuth, mandate setup token with an approval link.
Runs only when PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are set (CI secrets); otherwise exits 0 with a note.
Run: PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... python3 smoke_paypal_sandbox.py"""
import asyncio
import os
import sqlite3
import sys

if not (os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET")):
    print("PayPal sandbox check skipped: no credentials")
    sys.exit(0)
os.environ["PAYPAL_ENV"] = "sandbox"

import paypal402  # noqa: E402


async def main():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    paypal402.setup_db(c)
    m = await paypal402.create_mandate(c, 500, 25, 7, ["/v1/chat/completions"])
    assert m["approve_url"] and "paypal.com" in m["approve_url"], m
    print("PayPal sandbox OK: mandate", m["mandate"], "approval link issued")


asyncio.run(main())
