"""Simulated PayPal REST API (OAuth, Vault v3, Orders v2) for offline tests and the offline demo.
Behaves like the sandbox for the calls paypal402 makes; never touches the network."""
import json

import httpx

STATE = {"orders": 0, "fail_next": False, "error_next": False, "calls": [], "by_request_id": {}, "refunds": [],
         "by_order_id": {}, "pending_next": False}


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
    if req.method == "GET" and path.startswith("/v2/checkout/orders/"):
        o = STATE["by_order_id"].get(path.rsplit("/", 1)[1])
        return httpx.Response(200, json=o) if o else httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
    if path.startswith("/v2/payments/captures/") and path.endswith("/refund"):
        STATE["refunds"].append(path.split("/")[4])
        return httpx.Response(201, json={"id": "R1", "status": "COMPLETED"})
    if path == "/v2/checkout/orders":
        rid = req.headers.get("paypal-request-id", "")
        if rid in STATE["by_request_id"]:  # PayPal idempotency: same request id -> same result, no new charge
            return httpx.Response(200, json=STATE["by_request_id"][rid])
        if STATE["error_next"]:  # PayPal 5xx after the capture happened (worst case)
            STATE["error_next"] = False
            STATE["orders"] += 1
            o = {"id": f"O{STATE['orders']}", "status": "COMPLETED", "purchase_units": [
                {"payments": {"captures": [{"id": f"CAP{STATE['orders']}", "status": "COMPLETED"}]}}]}
            STATE["by_request_id"][rid] = STATE["by_order_id"][o["id"]] = o
            return httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"})
        if STATE["fail_next"]:
            STATE["fail_next"] = False
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"})
        b = json.loads(req.content)
        assert b["payment_source"]["paypal"]["vault_id"] == "VAULT1"
        assert req.headers.get("paypal-request-id", "").startswith("order-")
        STATE["orders"] += 1
        pend = STATE["pending_next"]
        STATE["pending_next"] = False
        body = {"id": f"O{STATE['orders']}", "status": "COMPLETED", "purchase_units": [
            {"payments": {"captures": [{"id": f"CAP{STATE['orders']}", "status": "PENDING" if pend else "COMPLETED"}]}}]}
        STATE["by_request_id"][rid] = STATE["by_order_id"][body["id"]] = body
        return httpx.Response(201, json=body)
    return httpx.Response(404, json={"path": path})


def transport():
    return httpx.MockTransport(handler)
