"""AI shopper demo: an LLM agent buys the data it needs from paid APIs, paying from a PayPal mandate.

    python3 demo/ai_shopper.py                                   # simulated PayPal, rule-based planner
    LLM_URL=https://api.openai.com/v1 LLM_KEY=... LLM_MODEL=gpt-4o-mini python3 demo/ai_shopper.py   # real LLM
    + PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET for the live PayPal sandbox

A small marketplace sells three APIs through `paypal_paywall` (prices in cents). The owner gives the agent a mandate:
30 cents in total, at most 15 per call. The agent gets a goal, sees the catalog with prices, decides what to buy
with tool calls (any OpenAI-compatible endpoint), pays each call from the mandate, and answers. Expensive calls are
refused by the server, not by the model. Author: Aleksandr Shcherbakov (@miinimal).
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "server"), os.path.join(ROOT, "clients", "python")]
PORT = int(os.environ.get("DEMO_PORT", "8403"))
LIVE = bool(os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"))
os.environ.setdefault("PUBLIC_URL", f"http://localhost:{PORT}")
if not LIVE:
    os.environ.update(PAYPAL_CLIENT_ID="simulated", PAYPAL_CLIENT_SECRET="simulated")

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import paypal402  # noqa: E402
from paypal_mandate import MandateClient, PaymentError  # noqa: E402
from paypal_paywall import paypal_paywall  # noqa: E402

if not LIVE:
    import fake_paypal
    paypal402._transport = fake_paypal.transport()

CATALOG = {"/v1/weather": (5, "tomorrow's forecast for a city: temperature, wind, rain chance"),
           "/v1/weather/pro": (20, "hour-by-hour forecast with UV index and sea temperature"),
           "/v1/events": (8, "events happening in a city tomorrow")}
shop = FastAPI()


@shop.post("/v1/weather")
def weather(b: dict):
    return {"city": b.get("city"), "temp_c": 24, "wind_kmh": 12, "rain_chance": 0.1}


@shop.post("/v1/weather/pro")
def weather_pro(b: dict):
    return {"city": b.get("city"), "hours": [{"h": 12, "temp_c": 25, "uv": 7, "sea_c": 19}]}


@shop.post("/v1/events")
def events(b: dict):
    return {"city": b.get("city"), "events": ["sunset concert at the riverside, 19:00"]}


BASE = f"http://localhost:{PORT}"
TOOLS = [{"type": "function", "function": {
    "name": "buy", "description": "Call a paid API from the catalog; it is paid from your PayPal mandate.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string", "enum": list(CATALOG)},
                                                    "city": {"type": "string"}}, "required": ["path", "city"]}}}]


def llm(messages):
    req = urllib.request.Request(os.environ["LLM_URL"].rstrip("/") + "/chat/completions", method="POST",
                                 data=json.dumps({"model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
                                                  "messages": messages, "tools": TOOLS}).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "X-GitHub-Api-Version": "2022-11-28",
                                          "Authorization": "Bearer " + os.environ.get("LLM_KEY", "")})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raise SystemExit(f"LLM HTTP {e.code}: {e.read()[:400]!r}")
    try:
        msg = json.loads(raw)["choices"][0]["message"]
    except (ValueError, KeyError, IndexError):
        raise SystemExit(f"LLM bad response (HTTP {status}): {raw[:400]!r}")
    return {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")}


def rule_planner(messages):
    """Stand-in for an LLM when no LLM_URL is set: buys basic weather, tries the pro forecast, then events."""
    done = [m for m in messages if m["role"] == "tool"]
    plan = ["/v1/weather", "/v1/weather/pro", "/v1/events"]
    if len(done) < len(plan):
        p = plan[len(done)]
        return {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{len(done)}", "type": "function",
                "function": {"name": "buy", "arguments": json.dumps({"path": p, "city": "Lisbon"})}}]}
    return {"role": "assistant", "content": "Go to the beach after 11:00: 24°C, light wind, 10% rain. "
                                            "Finish at the sunset concert by the river at 19:00."}


def run(goal, agent, think):
    msgs = [{"role": "system", "content": "You are a frugal assistant. Buy only the data you need. Catalog (cents): "
             + json.dumps({p: {"price": c, "what": d} for p, (c, d) in CATALOG.items()})},
            {"role": "user", "content": goal}]
    for _ in range(8):
        msg = think(msgs)
        msgs.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content")
        for c in calls:
            a = json.loads(c["function"]["arguments"])
            print(f"[agent] buying {a['path']} for {a['city']} ({CATALOG[a['path']][0]} cents)", flush=True)
            try:
                st, out = agent.call(a["path"], {"city": a["city"]})
                p = agent.last_payment or {}
                res = out if st == 200 else {"refused": out.get("error")}
                print(f"        -> {'paid, capture ' + str(p.get('transaction')) if st == 200 else 'refused: ' + str(out.get('error'))}",
                      flush=True)
            except PaymentError as e:
                res = {"refused": str(e)}
                print(f"        -> refused by the client: {e}", flush=True)
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(res)})
    return "stopped after 8 steps"


def main():
    app = paypal_paywall(shop, {p: c for p, (c, _) in CATALOG.items()}, tempfile.mktemp(suffix=".db"))
    server = uvicorn.Server(uvicorn.Config(app, port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    think = llm if os.environ.get("LLM_URL") else rule_planner
    print(f"[setup] PayPal {'LIVE SANDBOX' if LIVE else 'SIMULATED'}; brain: "
          f"{os.environ.get('LLM_MODEL', 'LLM') if think is llm else 'rule-based stand-in (set LLM_URL for a real LLM)'}")
    req = urllib.request.Request(BASE + "/v1/mandates", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"budget_cents": 30, "per_request_cents": 15, "days": 1}).encode())
    m = json.loads(urllib.request.urlopen(req).read())
    print(f"[owner] mandate {m['mandate']}: 30 cents total, max 15 per call. Approve: {m['approve_url']}")
    agent = MandateClient(BASE, m["mandate"], m["secret"], max_cents=100)  # the server holds the real limit
    if LIVE:
        while agent.status()["status"] != "active":
            time.sleep(3)
    else:
        urllib.request.urlopen(f"{BASE}/v1/mandates/{m['mandate']}/approved").read()
    answer = run("Plan my beach day in Lisbon tomorrow.", agent, think)
    st = agent.status()
    print(f"\n[agent] {answer}\n[owner] spent {st['spent_cents']} of {st['budget_cents']} cents")
    server.should_exit = True
    return st


if __name__ == "__main__":
    main()
