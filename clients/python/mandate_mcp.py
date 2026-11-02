"""MCP server: lets any MCP-capable AI agent (Claude, ChatGPT, Cursor...) spend from a PayPal mandate on GRAM-402
endpoints, strictly within the limits its owner approved.

    GRAM402_URL=https://gateway.example MANDATE_ID=m_... MANDATE_SECRET=... MANDATE_MAX_CENTS=10 \
        python3 mandate_mcp.py

Tools: `mandate_status` (budget, spent, left, expiry), `paid_chat` (ask a paid AI endpoint, pay per request),
`paid_call` (any GRAM-402 endpoint path on the same server). The secret stays in the environment; the model never
sees it. Requires: pip install "mcp>=1.2,<2". Author: Aleksandr Shcherbakov (@miinimal).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp.server.fastmcp import FastMCP  # noqa: E402

from paypal_mandate import MandateClient, PaymentError  # noqa: E402

mcp = FastMCP("gram402-mandate")


def _client():
    return MandateClient(os.environ["GRAM402_URL"], os.environ["MANDATE_ID"], os.environ["MANDATE_SECRET"],
                         max_cents=int(os.environ.get("MANDATE_MAX_CENTS", "25")))


@mcp.tool()
def mandate_status() -> str:
    """Show the PayPal spending mandate: status, budget, spent and remaining cents, per-request cap, expiry."""
    return json.dumps(_client().status())


@mcp.tool()
def paid_chat(question: str, model: str = "default") -> str:
    """Ask the paid AI endpoint. Pays one request from the PayPal mandate (within its limits) and returns the answer
    with the PayPal capture id and the remaining budget. Fails with a clear reason when a limit would be exceeded."""
    cli = _client()
    try:
        out = cli.chat({"model": model, "messages": [{"role": "user", "content": question}]})
    except PaymentError as e:
        return json.dumps({"paid": False, "reason": str(e)})
    return json.dumps({"paid": True, "answer": out["choices"][0]["message"]["content"], "payment": cli.last_payment,
                       "receipt": out["gram402"]["receipt"]})


@mcp.tool()
def paid_call(path: str, body_json: str = "{}") -> str:
    """POST to any GRAM-402 endpoint path on the same server (e.g. /v1/chat/completions), paying from the mandate."""
    cli = _client()
    try:
        status, out = cli.call(path, json.loads(body_json))
    except PaymentError as e:
        return json.dumps({"paid": False, "reason": str(e)})
    return json.dumps({"status": status, "payment": cli.last_payment, "response": out})


def main():
    mcp.run()


if __name__ == "__main__":
    main()
