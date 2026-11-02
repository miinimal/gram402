"""Simulated PayPal REST API (OAuth, Vault v3, Orders v2) for offline tests and the offline demo.
Behaves like the sandbox for the calls paypal402 makes; never touches the network."""
import json

import httpx

STATE = {"orders": 0, "fail_next": False, "calls": []}


def handler(req: httpx.Request):
    path = req.url.path
    STATE["calls"].append((req.method, path))
    if path == "/v1/oauth2/token":
        assert req.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    assert req.headers.get("authorization") == "Bearer tok"
    if path == "/v3/vault/setup-tokens":
        b = json.loads(req.content)
        assert b["payment_source"]["paypal"]["usage_type"] == "MERCHANT"
        return httpx.Response(201, json={"id": "ST1", "status": "PAYER_ACTION_REQUIRED",
                                         "links": [{"rel": "approve", "href": "https://sandbox.paypal.com/approve?t=ST1"}]})
    if path == "/v3/vault/payment-tokens":
        return httpx.Response(201, json={"id": "VAULT1", "payment_source": {"paypal": {"email_address": "o@example.com"}}})
    if path == "/v2/checkout/orders":
        if STATE["fail_next"]:
            STATE["fail_next"] = False
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"})
        b = json.loads(req.content)
        assert b["payment_source"]["paypal"]["vault_id"] == "VAULT1"
        assert req.headers.get("paypal-request-id", "").startswith("order-")
        STATE["orders"] += 1
        return httpx.Response(201, json={"id": f"O{STATE['orders']}", "status": "COMPLETED", "purchase_units": [
            {"payments": {"captures": [{"id": f"CAP{STATE['orders']}", "status": "COMPLETED"}]}}]})
    return httpx.Response(404, json={"path": path})


def transport():
    return httpx.MockTransport(handler)
