"""Tests for the per-address limits. Author: Aleksandr Shcherbakov (@miinimal)."""
import importlib
import os

os.environ["INVOICES_PER_IP_MINUTE"] = "3"
os.environ["PAYMENTS_PER_IP_MINUTE"] = "2"
import throttle  # noqa: E402

importlib.reload(throttle)
t = 1000.0
assert all(throttle.allow("1.2.3.4", t) for _ in range(3)) and not throttle.allow("1.2.3.4", t)
assert throttle.allow("1.2.3.4", t + 61), "window slides"
# One IPv6 /64 is one bucket; another /64 is not.
assert all(throttle.allow(f"2001:db8:1:2::{i}", t) for i in range(1, 4))
assert not throttle.allow("2001:db8:1:2:ffff::9", t)
assert throttle.allow("2001:db8:1:3::1", t)
assert throttle.key_of("::ffff:10.0.0.1") == "10.0.0.1"
# Payment attempts have their own budget.
assert throttle.allow("5.5.5.5", t, kind="pay") and throttle.allow("5.5.5.5", t, kind="pay")
assert not throttle.allow("5.5.5.5", t, kind="pay") and throttle.allow("5.5.5.5", t)
# X-Forwarded-For is ignored unless trusted; when trusted, the last hop (added by our proxy) counts.
h = {"x-forwarded-for": "6.6.6.6, 7.7.7.7"}
assert throttle.client_ip("10.0.0.2", h) == "10.0.0.2"
throttle.TRUST_XFF = True
assert throttle.client_ip("10.0.0.2", h) == "7.7.7.7" and throttle.client_ip("10.0.0.2", {}) == "10.0.0.2"
throttle.TRUST_XFF = False
# The table never grows past its bound.
throttle.MAX_KEYS = 100
for i in range(500):
    throttle.allow(f"9.9.{i // 256}.{i % 256}", t)
assert len(throttle._hits["invoice"]) <= 100
print("throttle: all tests passed")
