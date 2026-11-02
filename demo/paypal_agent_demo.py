"""Demo: an AI agent pays for its own API calls from a PayPal spending mandate its owner approved once.

    python3 demo/paypal_agent_demo.py            # offline: simulated PayPal, no account needed
    PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... python3 demo/paypal_agent_demo.py   # live PayPal sandbox
    DEMO_PHASE=create|run DEMO_STATE=file ...   # split run (CI): create prints the approval link and exits;
                                                # run waits until PayPal reports the approval, then the agent pays

Story: the owner gives the agent a 0.25 USD budget, at most 0.10 USD per request, valid 7 days, for one endpoint
only. The agent asks questions, hits HTTP 402, pays from the mandate on its own, gets answers with signed receipts
bound to PayPal captures, and is stopped by the budget. Then the owner revokes the mandate.
Author: Aleksandr Shcherbakov (@miinimal).
"""
import json
import os
import subprocess
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
REC = os.environ.get("DEMO_RECORD_DIR", "")  # record the owner page (needs playwright + chromium)
REC_SCRIPT = os.path.join(ROOT, "demo", "record_owner.py")


def say(who, text):
    print(f"\n[{who}] {text}", flush=True)


def owner_post(path, body):
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

    phase, state = os.environ.get("DEMO_PHASE", ""), os.environ.get("DEMO_STATE", "")
    if phase == "run":
        with open(state) as f:
            m = json.load(f)
        say("owner", f"Resuming mandate {m['mandate']}")
    elif REC and phase == "create":
        say("owner", "Creating a spending mandate on the owner page: budget 0.25 USD, max 0.10 USD per request, 7 days")
        subprocess.run([sys.executable, REC_SCRIPT, "create", BASE, state, REC], check=True, stdout=subprocess.DEVNULL)
        with open(state) as f:
            m = json.load(f)
        say("owner", f"Mandate {m['mandate']} created. Approve it once on PayPal:\n        {m['approve_url']}")
    else:
        say("owner", "Creating a spending mandate: budget 0.25 USD, max 0.10 USD per request, 7 days, chat endpoint only")
        m = owner_post("/v1/mandates", {"budget_cents": 25, "per_request_cents": 10, "days": 7,
                                        "resources": ["/v1/chat/completions"]})
        say("owner", f"Mandate {m['mandate']} created. Approve it once on PayPal:\n        {m['approve_url']}")
    if phase == "create" and not REC:
        fd = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"mandate": m["mandate"], "secret": m["secret"]}, f)
    if phase == "create":
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::notice title=Approve the PayPal sandbox mandate::{m['approve_url']}", flush=True)
        server.should_exit = True
        return
    agent = MandateClient(BASE, m["mandate"], m["secret"], max_cents=10)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"## PayPal {'sandbox (live API)' if LIVE else 'simulated'} run, mandate `{m['mandate']}`\n\n"
                    "| Request | Cents | PayPal capture | Budget left | Receipt valid |\n|---|---|---|---|---|\n")
    if LIVE:
        say("owner", "Waiting for approval on PayPal (log in with a PayPal sandbox buyer account)...")
        deadline = time.time() + int(os.environ.get("DEMO_APPROVE_WAIT_S", "1200"))
        while agent.status()["status"] != "active":
            if time.time() > deadline:
                raise SystemExit("mandate was not approved in time")
            time.sleep(6)
    else:
        urllib.request.urlopen(f"{BASE}/v1/mandates/{m['mandate']}/approved").read()
    say("owner", "Approved. From now on the agent pays on its own, within the limits.")
    rec, stop = None, (state or tempfile.mktemp()) + ".stop"
    if REC:
        rec = subprocess.Popen([sys.executable, REC_SCRIPT, "watch", BASE, state, REC, stop])
        time.sleep(6)  # the page is open before the first payment

    for q in ["What is GRAM-402?", "Summarize the TON blockchain in one line.", "One more question, please."]:
        if rec:
            time.sleep(5)  # let the recording show each payment arrive
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
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write(f"| {q} | {p['charged_cents']} | `{p['transaction']}` | {p['mandate_left_cents']} | {ok} |\n")

    if rec:
        time.sleep(6)
    st = agent.status()
    say("owner", f"Mandate status: spent {st['spent_cents']} of {st['budget_cents']} cents. Revoking it.")
    req = urllib.request.Request(f"{BASE}/v1/mandates/{m['mandate']}", method="DELETE",
                                 headers={"X-Mandate-Secret": m["secret"]})
    urllib.request.urlopen(req).read()
    try:
        agent.chat({"model": "echo", "messages": [{"role": "user", "content": "after revoke"}]})
    except PaymentError as e:
        say("agent", f"After revoke: {e}")
    if rec:
        open(stop, "w").close()
        rec.wait(timeout=120)
    server.should_exit = True


if __name__ == "__main__":
    main()
