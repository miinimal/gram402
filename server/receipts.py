"""Signed receipts (Ed25519) for paid answers.

A receipt binds the request hash, the response hash (excluding the server's own `gram402` block), model, route, time,
optional TEE evidence and, for GRAM-402 payments, {network, transaction, payer, charged_nano}. It is signed with the
server key (`public_key_hex()`); anyone holding the request and the response can verify it.
Author: Aleksandr Shcherbakov (@miinimal).
"""
import base64
import hashlib
import json
import os
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

KEY_PATH = os.environ.get("RECEIPT_KEY_PATH", os.path.join(os.path.dirname(os.environ.get("GRAM402_DB", "./x.db")),
                                                            "receipt_ed25519.key"))
_key = None


def key():
    global _key
    if _key is None:
        if os.path.exists(KEY_PATH):
            _key = Ed25519PrivateKey.from_private_bytes(open(KEY_PATH, "rb").read())
        else:
            _key = Ed25519PrivateKey.generate()
            os.makedirs(os.path.dirname(KEY_PATH) or ".", exist_ok=True)
            with open(KEY_PATH, "wb") as f:
                f.write(_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                           serialization.NoEncryption()))
            os.chmod(KEY_PATH, 0o600)
    return _key


def public_key_hex():
    return key().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha256(obj):
    return hashlib.sha256(canon(obj) if not isinstance(obj, (bytes, bytearray)) else obj).hexdigest()


def make(request_body, response_body, route, attestation, payment=None):
    """payment (GRAM-402): {network, transaction, payer, charged_nano} binds the on-chain payment to this answer."""
    a = attestation or {}
    payload = {
        "v": 1, "issuer": os.environ.get("RECEIPT_ISSUER", "GRAM402"), "ts": int(time.time()), "route": route,
        "model": request_body.get("model"),
        "request_sha256": sha256(request_body),
        "response_sha256": sha256({k: v for k, v in response_body.items() if k not in ("minimalai", "gram402")}),
        "tee": None if route != "cocoon" else {
            "type": a.get("tee"), "image_hash": a.get("image_hash"), "mr_td": a.get("mr_td"),
            "key_bound": a.get("reportdata_binds_tls_key"), "image_allowed_by_root": a.get("image_hash_allowed_by_root"),
            "intel_signature_verified": a.get("quote_signature_verified", False), "checked_at": a.get("checked_at"),
        },
        "gateway_key": public_key_hex(),
    }
    if payment:
        payload["payment"] = {k: payment.get(k) for k in ("network", "transaction", "payer", "charged_nano")}
    sig = key().sign(canon(payload))
    return {**payload, "sig": base64.b64encode(sig).decode()}


def verify(receipt, request_body=None, response_body=None):
    r = dict(receipt)
    sig = base64.b64decode(r.pop("sig"))
    # The signer must be THIS gateway's published key; a receipt carrying any other key is a forgery even if self-consistent.
    if r.get("gateway_key") != public_key_hex():
        return False
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(r["gateway_key"])).verify(sig, canon(r))
    if request_body is not None and sha256(request_body) != r["request_sha256"]:
        return False
    if response_body is not None and sha256({k: v for k, v in response_body.items() if k not in ("minimalai", "gram402")}) != r["response_sha256"]:
        return False
    return True
