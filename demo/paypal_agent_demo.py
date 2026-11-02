"""Demo: an AI agent pays for its own API calls from a PayPal spending mandate its owner approved once.

    python3 demo/paypal_agent_demo.py            # offline: simulated PayPal, no account needed
    PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... python3 demo/paypal_agent_demo.py   # live PayPal sandbox

Story: the owner gives the agent a 0.25 USD budget, at most 0.10 USD per request, valid 7 days, for one endpoint
only. The agent asks questions, hits HTTP 402, pays from the mandate on its own, gets answers with signed receipts
bound to PayPal captures, and is stopped by the budget. Then the owner revokes the mandate.
Author: Aleksandr Shcherbakov (@miinimal).
"""
import os
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "server"), os.path.join(ROOT, "clients", "python")]
LIVE = bool(os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"))
PORT = int(os.environ.get("DEMO_PORT", "8402"))
os.environ.setdefault("GRAM402_DB", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("PUBLIC_URL", f"http://localhost:{PORT}")
os.environ.setdefault("PAYPAL_PRICE_CENTS", "10")
os.environ.setdefault("PAYPAL_BRAND", "GRAM-402 demo")
if not LIVE:
    os.environ.update(PAYPAL_CLIENT_ID="simulated", PAYPAL_CLIENT_SECRET="simulated")

import uvicorn  # noqa: E402

import app as A  # noqa: E402
import paypal402  # noqa: E402
import receipts  # noqa: E402
from paypal_mandate import MandateClient, PaymentError  # noqa: E402

if not LIVE:
    import fake_paypal
    paypal402._transport = fake_paypal.transport()

BASE = f"http://localhost:{PORT}"


def say(who, text):
    print(f"\n[{who}] {text}", flush=True)


def owner_post(path, body):
    import json
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main():
    server = uvicorn.Server(uvicorn.Config(A.app, port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    say("setup", "PayPal: " + ("LIVE SANDBOX" if LIVE else "SIMULATED (no credentials given)") + f"; server {BASE}")

    say("owner", "Creating a spending mandate: budget 0.25 USD, max 0.10 USD per request, 7 days, chat endpoint only")
    m = owner_post("/v1/mandates", {"budget_cents": 25, "per_request_cents": 10, "days": 7,
                                    "resources": ["/v1/chat/completions"]})
    say("owner", f"Mandate {m['mandate']} created. Approve it once on PayPal:\n        {m['approve_url']}")
    agent = MandateClient(BASE, m["mandate"], m["secret"], max_cents=10)
    if LIVE:
        say("owner", "Waiting for approval in the browser (log in with a PayPal sandbox buyer account)...")
        while agent.status()["status"] != "active":
            time.sleep(3)
    else:
        urllib.request.urlopen(f"{BASE}/v1/mandates/{m['mandate']}/approved").read()
    say("owner", "Approved. From now on the agent pays on its own, within the limits.")

    for q in ["What is GRAM-402?", "Summarize the TON blockchain in one line.", "One more question, please."]:
        say("agent", f"Asking: {q}")
        try:
            out = agent.chat({"model": "echo", "messages": [{"role": "user", "content": q}]})
        except PaymentError as e:
            say("agent", f"Stopped by the mandate: {e}")
            break
        p = agent.last_payment
        ok = receipts.verify(out["gram402"]["receipt"])
        say("agent", f"Answer: {out['choices'][0]['message']['content']}\n"
                     f"        Paid {p['charged_cents']} cents via PayPal, capture {p['transaction']}, "
                     f"budget left {p['mandate_left_cents']} cents, receipt signature valid: {ok}")

    st = agent.status()
    say("owner", f"Mandate status: spent {st['spent_cents']} of {st['budget_cents']} cents. Revoking it.")
    req = urllib.request.Request(f"{BASE}/v1/mandates/{m['mandate']}", method="DELETE",
                                 headers={"X-Mandate-Secret": m["secret"]})
    urllib.request.urlopen(req).read()
    try:
        agent.chat({"model": "echo", "messages": [{"role": "user", "content": "after revoke"}]})
    except PaymentError as e:
        say("agent", f"After revoke: {e}")
    server.should_exit = True


if __name__ == "__main__":
    main()
