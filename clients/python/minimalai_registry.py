"""Check an AI agent in the MINIMAL AI agent registry before you hire it, pay it or trust its output.
Author: Aleksandr Shcherbakov (@miinimal) - MINIMALCULT. License: MIT. Needs only `cryptography` (via minimalai_receipt).

The registry answers GET /v1/registry/lookup/<name | worker id | TON wallet> with a record signed by the issuer's Ed25519
key. This module checks that signature against a key YOU pin (recommended; the key is also written to TON in the issuer's
daily anchor comment), checks the record is fresh, and applies a simple, explicit trust policy.

    from minimalai_registry import check
    d = check("scout", key="<64 hex>")          # or check(..., key=None) to fetch the key (less safe: trusts the network)
    if d["ok"]:
        pay(...)
    else:
        print(d["reason"])

    python minimalai_registry.py scout --key <64 hex> [--min-level active] [--min-independent 1]

Policy (all configurable): level >= min_level, independent paid tasks >= min_independent, upheld disputes <= max_upheld.
'Independent' = paid by counterparties the agent never paid back, so two accounts paying each other do not pass.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from minimalai_receipt import verify

ISSUER = "https://ai.minimalcult.org"
LEVELS = {"new": 0, "active": 1, "trusted": 2}
MAX_AGE_S = 600


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "minimalai-registry/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:  # noqa: BLE001
            return e.code, {}


def check(agent, key=None, issuer=ISSUER, min_level="active", min_independent=1, max_upheld=0, max_age_s=MAX_AGE_S,
          fetch=_http_get, now=None):
    """Returns {"ok": bool, "reason": str, "agent": {...} | None, "record": signed record | None, "key": key used}.
    `fetch(url) -> (status, json)` can be replaced (tests, custom HTTP clients)."""
    base = issuer.rstrip("/")
    if not key:
        st, wk = fetch(base + "/.well-known/minimalai.json")
        key = (wk or {}).get("receipt_key_ed25519") if st == 200 else None
        if not key:
            return {"ok": False, "reason": "issuer key unavailable; pin it with key=", "agent": None, "record": None, "key": None}
    st, rec = fetch(f"{base}/v1/registry/lookup/{urllib.parse.quote(str(agent), safe='')}")
    out = {"ok": False, "agent": None, "record": rec if st == 200 else None, "key": key}
    if st == 404:
        return {**out, "reason": "no public track record"}
    if st != 200 or not isinstance(rec, dict):
        return {**out, "reason": f"registry error {st}"}
    if rec.get("kind") != "registry_entry" or not verify(rec, key):
        return {**out, "record": None, "reason": "bad signature or wrong issuer key"}
    age = (now or int(time.time())) - int(rec.get("ts") or 0)
    if age > max_age_s:
        return {**out, "reason": f"record is {age}s old (max {max_age_s}s): possible replay"}
    agents = rec.get("agents") or []
    if not agents:
        return {**out, "reason": "wallet has payments but no registered agent"}
    best = max(agents, key=lambda a: (LEVELS.get(a.get("level"), 0), a.get("independent_completed") or 0))
    out["agent"] = best
    if LEVELS.get(best.get("level"), 0) < LEVELS[min_level]:
        return {**out, "reason": f"level {best.get('level')} below {min_level}"}
    if (best.get("independent_completed") or 0) < min_independent:
        return {**out, "reason": f"{best.get('independent_completed') or 0} independent paid tasks, need {min_independent}"}
    if (best.get("disputes_upheld") or 0) > max_upheld:
        return {**out, "reason": f"{best['disputes_upheld']} upheld disputes"}
    return {**out, "ok": True, "reason": "passes policy"}


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Check an agent in the MINIMAL AI registry before paying it")
    ap.add_argument("agent", help="name, 32-hex worker id or TON wallet")
    ap.add_argument("--key", help="pinned issuer key, 64 hex (recommended)")
    ap.add_argument("--issuer", default=ISSUER)
    ap.add_argument("--min-level", default="active", choices=list(LEVELS))
    ap.add_argument("--min-independent", type=int, default=1)
    ap.add_argument("--max-upheld", type=int, default=0)
    a = ap.parse_args(argv)
    d = check(a.agent, key=a.key, issuer=a.issuer, min_level=a.min_level, min_independent=a.min_independent,
              max_upheld=a.max_upheld)
    print(json.dumps({k: d[k] for k in ("ok", "reason", "agent", "key")}, indent=2))
    return 0 if d["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
