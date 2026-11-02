"""Standalone verifier for RECEIPTS v1 (MINIMAL AI signed receipts, task receipts and dispute records).
Author: Aleksandr Shcherbakov (@miinimal) - MINIMALCULT. Spec: RECEIPTS.md. License: MIT.

Needs only the `cryptography` package and the issuer's pinned Ed25519 public key. No call to the issuer is required to
check a signature; network calls are optional (fetching the key, checking dispute status).

    python minimalai_receipt.py receipt.json --key <64 hex>
    python minimalai_receipt.py receipt.json --issuer https://api.example --disputes
"""
import base64
import hashlib
import json
import sys
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def canon(obj):
    """Canonical JSON: UTF-8, keys sorted, no whitespace, non-ASCII kept as is."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha256(obj):
    return hashlib.sha256(obj if isinstance(obj, (bytes, bytearray)) else canon(obj)).hexdigest()


def receipt_id(record):
    """Id used by disputes and the oracle: SHA-256 of the canonical record without 'sig'."""
    return sha256({k: v for k, v in record.items() if k != "sig"})


def verify(record, pinned_key_hex):
    """True when the record names the pinned key and its Ed25519 signature over canonical JSON without 'sig' is valid."""
    r = dict(record)
    if r.get("gateway_key") != pinned_key_hex:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pinned_key_hex)).verify(base64.b64decode(r.pop("sig")), canon(r))
    except Exception:  # noqa: BLE001
        return False
    return True


def check_bodies(receipt, request=None, response=None):
    """Answer receipts commit to the exact request and response (response without its 'minimalai' envelope)."""
    if request is not None and sha256(request) != receipt.get("request_sha256"):
        return False
    if response is not None and sha256({k: v for k, v in response.items() if k != "minimalai"}) != receipt.get("response_sha256"):
        return False
    return True


def verify_chain(records, pinned_key_hex):
    """Records oldest first; each signed by the pinned key and linked by prev_sha256 = SHA-256 of the full previous record."""
    prev = records[0].get("prev_sha256") if records else None
    for r in records:
        if not verify(r, pinned_key_hex) or r.get("prev_sha256") != prev:
            return False
        prev = sha256(r)
    return True


def reputation(task_receipts, worker):
    """Recompute the counts of a 'reputation' record from public task receipts (oldest first), without the issuer.
    Returns the same keys the issuer signs, except dispute counts, timestamps and chain fields."""
    mine = [r for r in task_receipts if r.get("worker") == worker]
    paid_by_me = {r.get("worker") for r in task_receipts if r.get("poster") == worker}
    per, unknown = {}, 0
    for r in mine:
        if not r.get("poster"):
            unknown += 1
            continue
        per[r["poster"]] = per.get(r["poster"], 0) + int(r.get("price_nano") or 0)
    total = sum(per.values())
    indep = [r for r in mine if r.get("poster") and r["poster"] not in paid_by_me]
    return {"completed": len(mine), "earned_nano": sum(int(r.get("price_nano") or 0) for r in mine),
            "counterparties": len(per), "capped_completed": min(len(mine), 2 * len(per)),
            "effective_counterparties": round(total * total / sum(v * v for v in per.values()), 3) if total else 0,
            "top_counterparty_share": round(max(per.values()) / total, 4) if total else None,
            "reciprocal_counterparties": sum(1 for p in per if p in paid_by_me), "independent_completed": len(indep),
            "independent_earned_nano": sum(int(r.get("price_nano") or 0) for r in indep), "poster_unknown": unknown}


def merkle_verify(h, path, root):
    """Notary proof (GET /v1/notary/{id}/proof/{hash}): leaf = sha256(0x00||h), node = sha256(0x01||left||right)."""
    cur = hashlib.sha256(b"\x00" + bytes.fromhex(h)).digest()
    for step in path:
        sib = bytes.fromhex(step["hash"])
        cur = hashlib.sha256(b"\x01" + (sib + cur if step["side"] == "L" else cur + sib)).digest()
    return cur.hex() == root


def parse_anchor(comment):
    """Parse a TON anchor comment: 'MINIMALAI receipts <n> <head|-> [disputes <m> <head>] [key <hex>]'."""
    t = comment.split()
    if t[:2] != ["MINIMALAI", "receipts"]:
        raise ValueError("not a MINIMALAI anchor")
    out = {"receipts": int(t[2]), "receipts_head": None if t[3] == "-" else t[3]}
    i = 4
    if i < len(t) and t[i] == "disputes":
        out.update(disputes=int(t[i + 1]), disputes_head=t[i + 2]); i += 3
    if i < len(t) and t[i] == "notary":
        out.update(notary=int(t[i + 1]), notary_head=t[i + 2]); i += 3
    if i < len(t) and t[i] == "key":
        out["key"] = t[i + 1]
    return out


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "minimalai-receipt/1"}), timeout=30) as r:
        return json.load(r)


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Verify a MINIMAL AI RECEIPTS v1 record offline")
    ap.add_argument("record", help="JSON file with the signed record (or a full API response containing minimalai.receipt)")
    ap.add_argument("--key", help="pinned issuer key, 64 hex chars")
    ap.add_argument("--issuer", help="issuer base URL, used to fetch the key from /.well-known/minimalai.json and disputes")
    ap.add_argument("--disputes", action="store_true", help="also show dispute status from the issuer")
    a = ap.parse_args(argv)
    rec = json.load(open(a.record, encoding="utf-8"))
    rec = (rec.get("minimalai") or {}).get("receipt", rec)
    key = a.key or (_get(a.issuer.rstrip("/") + "/.well-known/minimalai.json")["receipt_key_ed25519"] if a.issuer else None)
    if not key:
        ap.error("give --key (pinned) or --issuer")
    ok = verify(rec, key)
    print(json.dumps({"valid_signature": ok, "receipt_id": receipt_id(rec), "kind": rec.get("kind", "answer"), "key": key}, indent=2))
    if a.disputes and a.issuer:
        print(json.dumps(_get(f"{a.issuer.rstrip('/')}/v1/disputes/receipt/{receipt_id(rec)}"), indent=2)[:4000])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
