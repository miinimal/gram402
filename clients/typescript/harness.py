"""Reference server (mocked chain) on 127.0.0.1:18402 for the TypeScript test; POST /_test/pay adds a transfer."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "server"))
import test_app as T  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import Request  # noqa: E402


@T.A.app.post("/_test/pay")
async def _pay(request: Request):
    b = await request.json()
    T.CHAIN.append(T.tx("ts%d" % len(T.CHAIN), b["comment"], int(b["amount"])))
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(T.A.app, host="127.0.0.1", port=18402, log_level="warning")
