"""Standalone test of minimalai_registry.check: records signed with a local Ed25519 key, served by a fake fetch.
Covers pass, policy refusals, unknown agent, wrong pinned key, tampering, replay, multi-agent wallets, key fetch."""
import base64, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import minimalai_registry as M
from minimalai_receipt import canon

SK = Ed25519PrivateKey.generate()
KEY = SK.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
NOW = int(time.time())


def signed(agents, ts=NOW):
    rec = {"v": 1, "kind": "registry_entry", "issuer": "MINIMALAI", "ts": ts, "query": "x", "agents": agents,
           "wallet": None, "passport": None, "chain_head": "h", "gateway_key": KEY}
    return {**rec, "sig": base64.b64encode(SK.sign(canon(rec))).decode()}


def agent(name, level, ind, upheld=0):
    return {"worker": "a" * 32, "name": name, "level": level, "independent_completed": ind, "disputes_upheld": upheld,
            "effective_counterparties": 2.273, "contributions": 0, "founder_no": None}


DB = {"scout": signed([agent("scout", "active", 3)]), "rookie": signed([agent("rookie", "new", 0)]),
      "cheat": signed([agent("cheat", "active", 2, upheld=1)]),
      "wallet": signed([agent("w1", "new", 0), agent("w2", "trusted", 12)])}


def fetch(url):
    if url.endswith("/.well-known/minimalai.json"):
        return 200, {"receipt_key_ed25519": KEY}
    name = url.rsplit("/", 1)[1]
    return (200, DB[name]) if name in DB else (404, {"detail": "not in the registry"})


assert M.check("scout", key=KEY, fetch=fetch)["ok"]
assert "below active" in M.check("rookie", key=KEY, fetch=fetch)["reason"]
assert M.check("rookie", key=KEY, fetch=fetch, min_level="new", min_independent=0)["ok"]
assert "upheld" in M.check("cheat", key=KEY, fetch=fetch)["reason"]
w = M.check("wallet", key=KEY, fetch=fetch)
assert w["ok"] and w["agent"]["name"] == "w2"                       # the strongest agent behind a wallet
assert M.check("nobody", key=KEY, fetch=fetch)["reason"] == "no public track record"
assert "signature" in M.check("scout", key="00" * 32, fetch=fetch)["reason"]


def tampered(url):
    st, rec = fetch(url)
    return st, {**rec, "agents": [{**rec["agents"][0], "independent_completed": 99}]} if st == 200 and "lookup" in url else rec


assert "signature" in M.check("rookie", key=KEY, fetch=tampered)["reason"]
assert "replay" in M.check("scout", key=KEY, fetch=fetch, now=NOW + 3600)["reason"]
assert M.check("scout", key=None, fetch=fetch)["key"] == KEY
print("minimalai_registry OK")
