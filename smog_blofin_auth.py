"""One-shot, read-only BloFin demo authentication diagnostic.

No trading methods, no secret logging, no production host fallback.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

DEMO = "https://demo-trading-openapi.blofin.com"
PATH = "/api/v1/asset/balances?accountType=futures"


def check_demo_auth() -> dict:
    base = os.environ.get("BLOFIN_BASE_URL", "").rstrip("/")
    if base != DEMO:
        return {"status": "BLOCKED", "reason": "non_demo_endpoint"}
    names = ("BLOFIN_API_KEY", "BLOFIN_API_SECRET", "BLOFIN_PASSPHRASE")
    if not all(os.environ.get(k) for k in names):
        return {"status": "BLOCKED", "reason": "missing_credentials"}
    timestamp = str(int(time.time() * 1000))
    nonce = str(uuid4())
    message = f"{PATH}GET{timestamp}{nonce}".encode()
    digest = hmac.new(os.environ["BLOFIN_API_SECRET"].encode(),
                      message, hashlib.sha256).hexdigest().encode()
    signature = base64.b64encode(digest).decode()
    headers = {
        "ACCESS-KEY": os.environ["BLOFIN_API_KEY"],
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-NONCE": nonce,
        "ACCESS-PASSPHRASE": os.environ["BLOFIN_PASSPHRASE"],
        "Content-Type": "application/json",
    }
    try:
        with urlopen(Request(DEMO + PATH, headers=headers, method="GET"), timeout=8) as response:
            payload = json.load(response)
        code = str(payload.get("code", "missing"))
        return {"status": "AUTHENTICATED" if code == "0" else "REJECTED",
                "api_code": code, "demo_only": True}
    except HTTPError as exc:
        return {"status": "HTTP_ERROR", "http_status": exc.code, "demo_only": True}
    except (URLError, TimeoutError, ValueError, OSError):
        return {"status": "NETWORK_OR_RESPONSE_ERROR", "demo_only": True}
