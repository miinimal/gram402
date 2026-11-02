"""Per-address limit for unauthenticated invoice creation (every unpaid 402 writes an invoice row).
In-memory sliding window; enough for one process. Author: Aleksandr Shcherbakov (@miinimal)."""
import os
import time
from collections import defaultdict, deque

LIMIT = int(os.environ.get("INVOICES_PER_IP_MINUTE", "60"))
_hits = defaultdict(deque)


def allow(ip, now=None):
    if not ip or LIMIT <= 0:
        return True
    now = now or time.monotonic()
    q = _hits[ip]
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= LIMIT:
        return False
    q.append(now)
    if len(_hits) > 50_000:  # bound memory under address churn
        for k in [k for k, v in _hits.items() if not v or v[-1] < now - 60][:25_000]:
            _hits.pop(k, None)
    return True
