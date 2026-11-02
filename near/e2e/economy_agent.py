"""Agent A in the agent-economy run: buys two trip plans from agent B through its NEAR mandate, then prints B's
profit and loss from the on-chain payments. Author: Aleksandr Shcherbakov (@miinimal)."""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "clients", "python"))
from near_mandate import NearMandateClient  # noqa: E402

CONTRACT, AGENT_A, LOG = os.environ["CONTRACT"], os.environ["AGENT_A"], os.environ["ECON_LOG"]


def log(line):
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def pay(contract, method, args):
    cmd = ["near", "contract", "call-function", "as-transaction", contract, method, "json-args", json.dumps(args),
           "prepaid-gas", "30 Tgas", "attached-deposit", "0 NEAR", "sign-as", AGENT_A, "network-config", "testnet",
           "sign-with-legacy-keychain", "send"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    out = p.stdout + p.stderr
    m = re.search(r"Transaction ID: ([1-9A-HJ-NP-Za-km-z]{32,64})", out)
    if p.returncode != 0 or not m or "succeeded" not in out:
        raise SystemExit("A's payment failed: " + out[-600:])
    log(f"[A] paid B for a plan: {int(args['amount']) / 1e24:.4f} NEAR, tx {m.group(1)}")
    return {"tx_hash": m.group(1), "sender": AGENT_A}


a = NearMandateClient("http://127.0.0.1:8402", 1, pay, CONTRACT, max_yocto=20 * 10**21, wait_s=180, poll_s=3)
revenue = cost = 0
for i in range(2):
    out = a.chat({"model": "agent-b", "messages": [{"role": "user", "content": "Plan my day in Lisbon"}]})
    log(f"[A] got plan {i + 1}: {out['choices'][0]['message']['content'][:160]}")
    revenue += int(a.last_payment["charged_yocto"])
cost = sum(int(x) for x in re.findall(r"bought weather from C: [0-9.]+ NEAR \(yocto=(\d+)\)", open(LOG).read()))
log(f"[B] P&L: revenue {revenue / 1e24:.4f} NEAR from A, data cost {cost / 1e24:.4f} NEAR paid to C, "
    f"gross margin {(revenue - cost) / 1e24:.4f} NEAR (procurement mandate deposit stays B's money)")
assert revenue > cost > 0, "economy run did not produce both sides"
print("ECONOMY OK")
