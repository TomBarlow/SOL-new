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
    "position_mode": "/api/v1/account/position-mode",
    "margin_mode": "/api/v1/account/margin-mode",
    "tpsl": "/api/v1/trade/orders-tpsl-pending",
    "order_history": "/api/v1/trade/orders-history",
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


def _signed_post(path: str, body: dict) -> dict:
    """Send to a fixed demo endpoint only, with explicit execution enablement."""
    _demo_only()
    if path not in WRITE_PATHS:
        raise ExecutionBlocked("Write endpoint not allowlisted")
    if os.environ.get("SMOG_DEMO_ORDER_ENABLED") != "true":
        raise ExecutionBlocked("Demo order transmission disabled")
    timestamp, nonce = str(int(time.time() * 1000)), str(uuid4())
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
    message = f"{path}POST{timestamp}{nonce}{raw}".encode()
    digest = hmac.new(os.environ["BLOFIN_API_SECRET"].encode(),
                      message, hashlib.sha256).hexdigest().encode()
    headers = {
        "ACCESS-KEY": os.environ["BLOFIN_API_KEY"],
        "ACCESS-SIGN": base64.b64encode(digest).decode(),
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-NONCE": nonce,
        "ACCESS-PASSPHRASE": os.environ["BLOFIN_PASSPHRASE"],
        "Content-Type": "application/json",
    }
    with urlopen(Request(DEMO + path, data=raw.encode(), headers=headers,
                         method="POST"), timeout=8) as response:
        result = json.load(response)
    if str(result.get("code")) != "0":
        raise ValueError("BloFin rejected demo order request")
    return result


def submit_order(proposal: dict) -> dict:
    """Real demo exchange order; intentionally gated off by default."""
    allowed = {"instId", "marginMode", "positionSide", "side", "orderType",
               "price", "size", "slTriggerPrice", "slOrderPrice",
               "slTriggerPriceType", "clientOrderId"}
    if set(proposal) != allowed or proposal["instId"] != "SOL-USDT":
        raise ValueError("Unexpected order fields or instrument")
    if proposal["orderType"] != "limit" or proposal["marginMode"] != "isolated":
        raise ValueError("Unsupported order mode")
    direction = proposal["positionSide"]
    expected = prepare_limit_order("validation", direction, proposal["price"],
                                   proposal["size"], proposal["slTriggerPrice"])
    for key in ("side", "slOrderPrice", "slTriggerPriceType"):
        if proposal[key] != expected[key]:
            raise ValueError("Invalid order parameters")
    if not str(proposal["clientOrderId"]).startswith("smog"):
        raise ValueError("Invalid client order id")
    return _signed_post("/api/v1/trade/order", proposal)


def cancel_order(order_id: str) -> dict:
    """Real demo exchange cancel, gated off by default."""
    if not order_id or not order_id.isdigit() or len(order_id) > 64:
        raise ValueError("Invalid exchange order ID")
    return _signed_post("/api/v1/trade/cancel-order",
                        {"instId": "SOL-USDT", "orderId": order_id})


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


def demo_instrument() -> dict:
    """Read exchange contract specifications; no trading or account mutation."""
    _demo_only()
    with urlopen(Request(DEMO + "/api/v1/market/instruments?instId=SOL-USDT",
                         method="GET"), timeout=8) as response:
        payload = json.load(response)
    if str(payload.get("code")) != "0":
        raise ValueError("Instrument lookup rejected")
    matches = [x for x in payload.get("data", []) if x.get("instId") == "SOL-USDT"]
    if len(matches) != 1:
        raise ValueError("SOL-USDT instrument not uniquely found")
    return matches[0]
