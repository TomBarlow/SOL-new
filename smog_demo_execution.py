"""SMOG demo futures execution foundation. Order transmission intentionally disabled.

Validates trade plans and constructs deterministic demo order requests, but
NEVER submits them. Read-only reconciliation is allowed on the demo host.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import base64
import json
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

DEMO = "https://demo-trading-openapi.blofin.com"
READ_PATHS = {
    "positions": "/api/v1/account/positions",
    "orders": "/api/v1/trade/orders-pending",
    "fills": "/api/v1/trade/fills-history",
}
WRITE_PATHS = {"/api/v1/trade/order", "/api/v1/trade/cancel-order"}


class ExecutionBlocked(Exception):
    pass


def _demo_only():
    if os.environ.get("BLOFIN_BASE_URL", "").rstrip("/") != DEMO:
        raise ExecutionBlocked("Demo endpoint required")


def _positive(value):
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid decimal")
    if not n.is_finite() or n <= 0:
        raise ValueError("Positive finite decimal required")
    return n


def prepare_limit_order(setup_id: str, direction: str, price, size, stop,
                        margin_mode="isolated") -> dict:
    """Build order proposal; no exchange call. Never accept arbitrary symbols."""
    if not setup_id or len(setup_id) > 128:
        raise ValueError("Invalid setup id")
    if direction not in ("long", "short") or margin_mode != "isolated":
        raise ValueError("Unsupported direction/margin mode")
    p, q, sl = _positive(price), _positive(size), _positive(stop)
    if direction == "long" and not sl < p:
        raise ValueError("Long stop must be below entry")
    if direction == "short" and not sl > p:
        raise ValueError("Short stop must be above entry")
    # Deterministic idempotency identifier, max 32 chars.
    client_id = "smog" + hashlib.sha256(setup_id.encode()).hexdigest()[:28]
    return {"instId": "SOL-USDT", "marginMode": margin_mode,
            "positionSide": direction, "side": "buy" if direction == "long" else "sell",
            "orderType": "limit", "price": str(p), "size": str(q),
            "slTriggerPrice": str(sl), "slOrderPrice": "-1",
            "slTriggerPriceType": "last", "clientOrderId": client_id}


def submit_order(_proposal):
    """Hard fail-closed: no automated demo or live trading in this phase."""
    raise ExecutionBlocked("Order submission disabled pending explicit activation and tests")


def cancel_order(_order_id):
    raise ExecutionBlocked("Order cancellation disabled pending explicit activation and tests")


def demo_read(endpoint: str, params: dict | None = None) -> dict:
    """Authenticated, read-only account reconciliation, no credentials returned."""
    _demo_only()
    if endpoint not in READ_PATHS:
        raise ExecutionBlocked("Endpoint not on read-only allowlist")
    path = READ_PATHS[endpoint]
    if params:
        path += "?" + urlencode(params)
    timestamp, nonce = str(int(time.time() * 1000)), str(uuid4())
    msg = f"{path}GET{timestamp}{nonce}".encode()
    digest = hmac.new(os.environ["BLOFIN_API_SECRET"].encode(), msg,
                      hashlib.sha256).hexdigest().encode()
    signature = base64.b64encode(digest).decode()
    headers = {"ACCESS-KEY": os.environ["BLOFIN_API_KEY"],
               "ACCESS-SIGN": signature, "ACCESS-TIMESTAMP": timestamp,
               "ACCESS-NONCE": nonce, "ACCESS-PASSPHRASE": os.environ["BLOFIN_PASSPHRASE"]}
    with urlopen(Request(DEMO + path, headers=headers, method="GET"), timeout=8) as response:
        result = json.load(response)
    if str(result.get("code")) != "0":
        raise ValueError("BloFin read-only query rejected")
    return result
