# gram402 (Python clients)

HTTP 402 payments for AI agents. Stdlib-only clients; the MCP server needs `pip install "gram402[mcp]"`.

```python
from paypal_mandate import MandateClient        # pay from a PayPal spending mandate approved once by the owner
from near_mandate import NearMandateClient      # pay through a NEAR mandate contract (limits enforced on chain)
from gram402 import Gram402Client               # pay with GRAM on TON
from gram402_merchant import Merchant, paywall  # sell your API for GRAM
```

MCP server for Claude and other agents: `gram402-mandate-mcp` with `GRAM402_URL`, `MANDATE_ID`, `MANDATE_SECRET`.

Protocol, server and NEAR contract: https://github.com/miinimal/gram402 (MIT).
