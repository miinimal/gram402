"""Per-address limits for unauthenticated work: unpaid 402s (each writes an invoice row) and payment attempts
(each may cost a PayPal or RPC call). In-memory sliding window; enough for one process.
Author: Aleksandr Shcherbakov (@miinimal)."""
import ipaddress
import os
import time
from collections import OrderedDict, deque

LIMIT = int(os.environ.get("INVOICES_PER_IP_MINUTE", "60"))
PAY_LIMIT = int(os.environ.get("PAYMENTS_PER_IP_MINUTE", "120"))
# Only behind a proxy you run: take the client address from the last X-Forwarded-For hop it appended.
TRUST_XFF = os.environ.get("TRUST_X_FORWARDED_FOR", "") == "1"
MAX_KEYS = 50_000
_hits = {}


def reset():
    _hits.clear()


def key_of(ip):
    """IPv6 clients usually control a whole /64, so they share one bucket."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if a.version == 4:
        return str(a)
    if a.ipv4_mapped is not None:
        return str(a.ipv4_mapped)
    return str(ipaddress.ip_network(f"{a}/64", strict=False))


def client_ip(peer, headers):
    """peer: the socket address; headers: a mapping with lower-case names."""
    if TRUST_XFF:
        hops = [h.strip() for h in (headers.get("x-forwarded-for") or "").split(",") if h.strip()]
        if hops:
            return hops[-1]
    return peer


def allow(ip, now=None, kind="invoice"):
    limit = LIMIT if kind == "invoice" else PAY_LIMIT
    if not ip or limit <= 0:
        return True
    now = now or time.monotonic()
    table = _hits.setdefault(kind, OrderedDict())
    k = key_of(ip)
    q = table.pop(k, None) or deque()
    table[k] = q  # most recently used last
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    while len(table) > MAX_KEYS:  # hard bound: drop the least recently used addresses
        table.popitem(last=False)
    return True
