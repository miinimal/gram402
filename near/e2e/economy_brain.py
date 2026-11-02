"""Agent B's brain for the agent-economy run: an OpenAI-compatible upstream that buys the data it needs.

B sells trip plans (its GRAM-402 server uses this as UPSTREAM_URL). To write a plan it buys a weather report from
merchant C, paying from B's own procurement mandate. After its first sale B opens that mandate itself, funded from
the money it has just earned; afterwards every plan is: revenue from A, cost paid to C, both on chain.
Author: Aleksandr Shcherbakov (@miinimal).
"""
import json
import os
import re
import subprocess
import sys
import time

from fastapi import FastAPI

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "clients", "python"))
from near_mandate import NearMandateClient  # noqa: E402

E = os.environ
CONTRACT, SELLER, PROCURE, DATA = E["CONTRACT"], E["SELLER"], E["PROCURE"], E["DATA"]
DATA_URL, PROC_DEPOSIT, PROC_CAP = E["DATA_URL"], E["PROC_DEPOSIT"], E["PROC_CAP"]
LOG = E.get("ECON_LOG", "economy.log")
state = {"mandate": None, "bought": 0}
app = FastAPI()


def log(line):
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def near_call(signer, method, args, deposit="0 NEAR"):
    cmd = ["near", "contract", "call-function", "as-transaction", CONTRACT, method, "json-args", json.dumps(args),
           "prepaid-gas", "30 Tgas", "attached-deposit", deposit, "sign-as", signer, "network-config", "testnet",
           "sign-with-legacy-keychain", "send"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    out = p.stdout + p.stderr
    m = re.search(r"Transaction ID: ([1-9A-HJ-NP-Za-km-z]{32,64})", out)
    if p.returncode != 0 or not m or "succeeded" not in out:
        raise RuntimeError(f"{method} failed: {out[-600:]}")
    return m.group(1), out


def procurement_mandate():
    if state["mandate"] is None:
        tx, out = near_call(SELLER, "create_mandate", {"agent": PROCURE, "per_payment_cap": PROC_CAP,
                                                        "valid_for_ms": 3_600_000, "merchants": [DATA]},
                            deposit=PROC_DEPOSIT)
        mid = int(re.search(r'"mandate_id":(\d+)', out).group(1))
        state["mandate"] = mid
        log(f"[B] first sale done -> opened procurement mandate {mid} from revenue ({PROC_DEPOSIT}), tx {tx}")
    return state["mandate"]


def pay(contract, method, args):
    tx, _ = near_call(PROCURE, method, args)
    log(f"[B] bought weather from C: {int(args['amount']) / 1e24:.4f} NEAR (yocto={args['amount']}), tx {tx}")
    return {"tx_hash": tx, "sender": PROCURE}


@app.post("/chat/completions")
def plan(body: dict):
    mid = procurement_mandate()
    shop = NearMandateClient(DATA_URL, mid, pay, max_yocto=int(PROC_CAP), poll_s=2)
    weather = shop.chat({"model": "weather", "messages": [{"role": "user", "content": "Lisbon tomorrow"}]})
    w = weather["choices"][0]["message"]["content"]
    state["bought"] += 1
    text = f"Plan: beach after 11:00, sunset walk at 19:00. Based on purchased data -> {w}"
    return {"id": f"plan-{int(time.time())}", "object": "chat.completion", "created": int(time.time()),
            "model": "agent-b", "choices": [{"index": 0, "finish_reason": "stop",
                                             "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
