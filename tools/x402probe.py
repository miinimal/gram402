"""x402 conformance probe: can a buyer agent actually turn this 402 into a payment?

Validates the whole offer of an HTTP 402 answer for x402 v1 (requirements in the JSON body, maxAmountRequired, network
names like "base") and v2 (base64 JSON in the PAYMENT-REQUIRED header, amount, CAIP-2 networks like "eip155:8453",
top-level resource object), plus GRAM-402 (scheme ton-transfer). Every verdict keeps the exact captured response
(status, relevant headers, body) and its SHA-256, so a verdict is evidence, not opinion.

Standalone, MIT. CLI:  python3 x402probe.py https://api.example.com/paid [--method GET|POST|auto]
Library: analyze(capture, url) for a captured answer; await probe(url) to fetch GET and POST and analyze both.
Hosted with a public evidence-backed table: https://ai.minimalcult.org/x402 (POST /v1/x402/check {url}).

Requested by agents on Moltbook (obtranzat's open bounty draft, 2026-11-02): cross-field agreement, header vs body,
accepts[] vs top-level fields, GET vs POST, captured response behind every verdict.
Author: Aleksandr Shcherbakov (@miinimal) - MINIMALCULT.
"""
import base64
import hashlib
import json
import re
import time

import httpx
import ipaddress
import socket
from urllib.parse import urlsplit

EVM = {"base": 8453, "base-sepolia": 84532, "ethereum": 1, "polygon": 137, "polygon-amoy": 80002, "avalanche": 43114,
       "avalanche-fuji": 43113, "arbitrum": 42161, "optimism": 10, "sei": 1329, "sei-testnet": 1328, "iotex": 4689}
CHAIN_NAME = {v: k for k, v in EVM.items()}
USDC = {8453: "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 84532: "0x036cbd53842c5426634e7929541ec2318f3dcf7e"}
SOLANA = {"solana", "solana-devnet", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"}
TON = {"ton-mainnet", "ton-testnet"}
HDRS = ("payment-required", "x-payment-required", "www-authenticate", "content-type", "retry-after")
EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _b64json(v):
    """Base64 (standard or URL-safe, padded or not) JSON; plain JSON is accepted too."""
    raw = (v or "").strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    for dec in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return json.loads(dec(raw + "=" * (-len(raw) % 4)))
        except Exception:  # noqa: BLE001
            continue
    return None


def chain_id(network):
    n = str(network or "")
    if n in EVM:
        return EVM[n]
    m = re.match(r"^eip155:(\d+)$", n)
    return int(m.group(1)) if m else None


def family(network):
    n = str(network or "")
    if chain_id(n):
        return "evm"
    if n in SOLANA or n.startswith("solana:"):
        return "solana"
    if n in TON:
        return "ton"
    return None


TON_RAW = re.compile(r"^-?\d+:[0-9a-fA-F]{64}$")
TON_FRIENDLY = re.compile(r"^[A-Za-z0-9_+/-]{48}$")


def _ton_addr(a):
    return bool(TON_RAW.match(a) or TON_FRIENDLY.match(a))


def check_accept(i, a, version, url):
    out = []

    def f(cid, sev, ok, detail):
        out.append({"id": f"accepts[{i}].{cid}", "severity": sev, "pass": bool(ok), "detail": detail})
    if not isinstance(a, dict):
        f("object", "error", False, "entry is not an object")
        return out
    scheme, net = a.get("scheme"), a.get("network")
    f("scheme", "error", bool(scheme), f"scheme={scheme!r}")
    fam = family(net)
    f("network", "error" if not net else "warn", fam is not None, f"network={net!r}" + ("" if fam else " (not a known network)"))
    if version == 2 and net and chain_id(net) and not str(net).startswith("eip155:"):
        f("network_format", "error", False, "v2 expects CAIP-2 networks such as eip155:8453")
    if version == 1 and str(net or "").startswith("eip155:"):
        f("network_format", "warn", False, "v1 clients expect network names such as 'base'")
    amt_key = "amount" if version == 2 else "maxAmountRequired"
    amt = a.get(amt_key)
    if amt is None:
        other = "maxAmountRequired" if version == 2 else "amount"
        f("amount", "error", False, f"{amt_key} missing" + (f" (found {other}: field of the other version)" if a.get(other) is not None else ""))
    else:
        f("amount", "error", isinstance(amt, str) and amt.isdigit() and int(amt) > 0,
          f"{amt_key}={amt!r} must be a positive integer string in atomic units")
    pay = a.get("payTo")
    if fam == "evm":
        f("payTo", "error", isinstance(pay, str) and bool(EVM_ADDR.match(pay)), f"payTo={pay!r} must be a 0x address")
    elif fam == "solana":
        f("payTo", "error", isinstance(pay, str) and bool(B58.match(pay)), f"payTo={pay!r} must be a base58 address")
    elif fam == "ton":
        f("payTo", "error", isinstance(pay, str) and _ton_addr(pay), f"payTo={pay!r} must be a TON address")
    else:
        f("payTo", "error", bool(pay), f"payTo={pay!r}")
    asset = a.get("asset")
    if fam == "evm" and scheme == "exact":
        okfmt = isinstance(asset, str) and bool(EVM_ADDR.match(asset))
        f("asset", "error", okfmt, f"asset={asset!r} must be the token contract address")
        cid = chain_id(net)
        if okfmt and cid in USDC:
            f("asset_known", "warn", asset.lower() == USDC[cid], f"asset is {'USDC' if asset.lower() == USDC[cid] else 'not the USDC contract'} on chain {cid}")
        ex = a.get("extra") or {}
        f("extra.eip712", "error", bool(ex.get("name")) and bool(ex.get("version")),
          f"extra.name={ex.get('name')!r} extra.version={ex.get('version')!r} (EIP-712 domain; without them no signature verifies)")
    elif fam == "solana":
        f("asset", "error", isinstance(asset, str) and bool(B58.match(asset or "")), f"asset={asset!r} must be a mint address")
    elif fam != "ton":
        f("asset", "warn", bool(asset), f"asset={asset!r}")
    mt = a.get("maxTimeoutSeconds")
    f("maxTimeoutSeconds", "warn", isinstance(mt, int) and mt > 0, f"maxTimeoutSeconds={mt!r}")
    if version == 1:
        res = a.get("resource")
        f("resource", "warn", bool(res) and (str(res) == url or str(res).rstrip("/") == url.split("?")[0].rstrip("/")
                                              or not str(res).startswith("http")), f"resource={res!r} vs requested {url!r}")
    if scheme == "ton-transfer":
        ex = a.get("extra") or {}
        f("extra.invoice", "error", bool(ex.get("invoice")) and bool(ex.get("comment")), "ton-transfer needs extra.invoice and extra.comment")
    return out


def offers(obj):
    if not isinstance(obj, dict):
        return None
    acc = obj.get("accepts")
    return acc if isinstance(acc, list) else None


def norm(a):
    """An offer reduced to what a buyer pays: v1 and v2 spellings of the same offer compare equal."""
    if not isinstance(a, dict):
        return None
    net = a.get("network")
    cid = chain_id(net)
    amt = a.get("amount") if a.get("amount") is not None else a.get("maxAmountRequired")
    return (a.get("scheme"), f"evm:{cid}" if cid else str(net), str(amt), str(a.get("payTo") or "").lower(),
            str(a.get("asset") or "").lower())


def analyze(cap, url):
    """cap: {status, headers{lowercase}, body(str)}. Returns findings and a verdict."""
    fs = []

    def f(cid, sev, ok, detail):
        fs.append({"id": cid, "severity": sev, "pass": bool(ok), "detail": detail})
    f("status", "error", cap["status"] == 402, f"HTTP {cap['status']}")
    hdr_raw = cap["headers"].get("payment-required") or cap["headers"].get("x-payment-required")
    hdr = _b64json(hdr_raw) if hdr_raw else None
    try:
        body = json.loads(cap["body"]) if cap["body"] else None
    except ValueError:
        body = None
    if hdr_raw:
        f("header.decodes", "error", hdr is not None, "PAYMENT-REQUIRED header must be base64 JSON")
    req = hdr if offers(hdr) is not None else (body if offers(body) is not None else None)
    where = "header" if req is hdr and hdr is not None else ("body" if req is not None else None)
    f("requirements", "error", req is not None, f"machine-readable requirements found in {where}" if where else
      "no accepts[] in the PAYMENT-REQUIRED header or the JSON body")
    if req is None:
        return verdict(fs, None, where)
    version = req.get("x402Version")
    f("x402Version", "error", version in (1, 2), f"x402Version={version!r}")
    version = version if version in (1, 2) else (2 if where == "header" else 1)
    if version == 2:
        f("v2.header", "warn", where == "header", "v2 carries requirements in the PAYMENT-REQUIRED header")
        r = req.get("resource")
        f("v2.resource", "warn", isinstance(r, dict) and bool(r.get("url")), "v2 has a top-level resource object with url")
    acc = offers(req)
    f("accepts.nonempty", "error", len(acc) > 0, f"{len(acc)} payment option(s)")
    for i, a in enumerate(acc[:10]):
        fs.extend(check_accept(i, a, version, url))
    # cross-field agreement
    if hdr is not None and offers(hdr) is not None and offers(body) is not None:
        hs, bs = {norm(a) for a in offers(hdr)}, {norm(a) for a in offers(body)}
        same = hs == bs
        f("header_vs_body", "error", same, "header and body describe the same offers (compared by scheme, network, "
          "amount, payTo, asset)" if same else f"header and body offer different payments: header {sorted(map(str, hs - bs))[:2]} "
          f"vs body {sorted(map(str, bs - hs))[:2]}")
    for k in ("network", "payTo", "asset", "scheme"):
        if isinstance(req.get(k), str):
            agree = any(isinstance(a, dict) and a.get(k) == req[k] for a in acc)
            f(f"toplevel.{k}", "error", agree, f"top-level {k}={req[k]!r} " + ("matches an accepts entry" if agree else
                                                                          "matches no accepts entry"))
    return verdict(fs, version, where)


def verdict(fs, version, where):
    errors = [x for x in fs if not x["pass"] and x["severity"] == "error"]
    warns = [x for x in fs if not x["pass"] and x["severity"] == "warn"]
    return {"payable": not errors, "x402Version": version, "requirements_in": where, "errors": len(errors),
            "warnings": len(warns), "findings": fs}


def capture(r):
    body = r.content[:16384].decode("utf-8", "replace")
    headers = {k.lower(): v[:65536] for k, v in r.headers.items() if k.lower() in HDRS}   # v2 offers can be long
    cap = {"status": r.status_code, "headers": headers, "body": body}
    cap["sha256"] = hashlib.sha256(json.dumps(cap, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return cap


async def probe(url, method="auto", client=None):
    """Fetch the unpaid answer(s) safely and analyze them. method auto: GET and POST."""
    err = await check_url_async(url)
    if err:
        return {"error": err}
    own = client is None
    client = client or httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False,
                                         headers={"User-Agent": "minimalai-x402-probe/1 (+https://ai.minimalcult.org/x402)"})
    out = {}
    try:
        for m in (("GET", "POST") if method == "auto" else (method,)):
            try:
                r = await (client.get(url) if m == "GET" else client.post(url, json={}))
                cap = capture(r)
                out[m] = {"capture": cap, "result": analyze(cap, url)}
            except httpx.HTTPError as e:
                out[m] = {"error": "request failed: " + type(e).__name__}
    finally:
        if own:
            await client.aclose()
    ok = {m: v["result"]["payable"] for m, v in out.items() if "result" in v}
    summary = {"url": url, "checked_at": int(time.time()), "methods": out,
               "payable_methods": [m for m, p in ok.items() if p]}
    bad_402 = [m for m, v in out.items() if "result" in v and not v["result"]["payable"] and v["capture"]["status"] == 402]
    if len(ok) == 2 and ok["GET"] != ok["POST"] and bad_402:
        summary["method_mismatch"] = ("one method returns a valid challenge and the other does not: a catalogue that "
                                      "declares the other method sends buyers to an unpayable answer")
    summary["payable"] = bool(summary["payable_methods"])
    caps = [v["capture"] for v in out.values() if "capture" in v]
    summary["state"] = ("payable" if summary["payable"] else
                        "unavailable" if any(cp["status"] == 503 and "retry-after" in cp["headers"] for cp in caps)
                        and not any(cp["status"] == 402 for cp in caps) else "not payable")
    return summary


def check_url(url):
    """Only public https hosts on 443 (the probe must not be pointed at private networks)."""
    p = urlsplit(url)
    try:
        port = p.port
    except ValueError:
        return "invalid port"
    if p.scheme != "https" or not p.hostname or p.username or p.password or port not in (None, 443):
        return "url must be https://host/... on port 443"
    try:
        infos = socket.getaddrinfo(p.hostname, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return "host does not resolve"
    if any(not ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos):
        return "host must resolve to public addresses only"
    return None


async def check_url_async(url):
    import asyncio
    try:
        return await asyncio.wait_for(asyncio.to_thread(check_url, url), 5)
    except asyncio.TimeoutError:
        return "host did not resolve in time"


if __name__ == "__main__":
    import argparse
    import asyncio
    ap = argparse.ArgumentParser(description="x402 conformance probe")
    ap.add_argument("url")
    ap.add_argument("--method", default="auto", choices=["auto", "GET", "POST"])
    a = ap.parse_args()
    print(json.dumps(asyncio.run(probe(a.url, a.method)), indent=2, ensure_ascii=False))
