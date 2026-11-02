"""RECEIPTS v1 verifier: receipts signed by server/receipts.py verify offline with the pinned key; tampering, a foreign
key, a broken chain link and anchor comments are handled."""
import os, sys, tempfile
here = os.path.dirname(os.path.abspath(__file__))
os.environ["RECEIPT_KEY_PATH"] = os.path.join(tempfile.mkdtemp(), "k.key")
sys.path[:0] = [here, os.path.join(here, "..", "..", "server")]
import receipts as S
import minimalai_receipt as V

req, resp = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, {"choices": [{"message": {"content": "hello"}}]}
rc = S.make(req, resp, "fallback", None)
pk = S.public_key_hex()
assert V.verify(rc, pk)
assert not V.verify(rc, "00" * 32)
assert not V.verify({**rc, "model": "other"}, pk)
assert not V.verify({**rc, "sig": "!!"}, pk)
assert V.check_bodies(rc, request=req, response={**resp, "minimalai": {"x": 1}})
assert not V.check_bodies(rc, response={"choices": []})
assert V.receipt_id(rc) == V.sha256({k: v for k, v in rc.items() if k != "sig"})
a = S.make({**req, "n": 1}, resp, "fallback", None)
b = S.make({**req, "n": 2}, resp, "fallback", None)
# a chain is built by the issuer with prev_sha256; emulate it with the issuer key
k = S.key()
import base64
def signed(p):
    p = {**p, "gateway_key": pk}
    return {**p, "sig": base64.b64encode(k.sign(V.canon(p))).decode()}
r1 = signed({"v": 1, "kind": "dispute_opened", "receipt_id": V.receipt_id(a), "prev_sha256": None})
r2 = signed({"v": 1, "kind": "dispute_resolved", "receipt_id": V.receipt_id(a), "outcome": "upheld", "prev_sha256": V.sha256(r1)})
assert V.verify_chain([r1, r2], pk) and not V.verify_chain([r2, r1], pk)
p = V.parse_anchor(f"MINIMALAI receipts 3 {'a' * 64} disputes 2 {'b' * 64} key {pk}")
assert p == {"receipts": 3, "receipts_head": "a" * 64, "disputes": 2, "disputes_head": "b" * 64, "key": pk}
assert V.parse_anchor("MINIMALAI receipts 0 -")["receipts_head"] is None
recs = [{"worker": "a", "poster": "b", "price_nano": 3}, {"worker": "a", "poster": "c", "price_nano": 1},
        {"worker": "b", "poster": "a", "price_nano": 2}, {"worker": "a", "price_nano": 1}]
rp = V.reputation(recs, "a")
assert rp["counterparties"] == 2 and rp["effective_counterparties"] == round(16 / 10, 3) and rp["top_counterparty_share"] == 0.75
assert rp["reciprocal_counterparties"] == 1 and rp["independent_completed"] == 1 and rp["poster_unknown"] == 1
print("minimalai_receipt OK")
