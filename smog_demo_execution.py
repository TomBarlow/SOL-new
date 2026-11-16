"""SMOG demo futures execution with bounded orders and reconciled lifecycle actions.

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
    "order_detail": "/api/v1/trade/order-detail",
    "tpsl_detail": "/api/v1/trade/order-tpsl-detail",
    "positions_history": "/api/v1/account/positions-history",
    "tpsl_history": "/api/v1/trade/orders-tpsl-history",
}
WRITE_PATHS = {"/api/v1/trade/order", "/api/v1/trade/cancel-order",
               "/api/v1/trade/amend-tpsl", "/api/v1/trade/order-tpsl"}


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
                        margin_mode="cross") -> dict:
    """Build order proposal; no exchange call. Never accept arbitrary symbols."""
    if not setup_id or len(setup_id) > 128:
        raise ValueError("Invalid setup id")
    if direction not in ("long", "short") or margin_mode != "cross":
        raise ValueError("Unsupported direction/margin mode")
    p, q, sl = _positive(price), _positive(size), _positive(stop)
    if direction == "long" and not sl < p:
        raise ValueError("Long stop must be below entry")
    if direction == "short" and not sl > p:
        raise ValueError("Short stop must be above entry")
    # Deterministic idempotency identifier, max 32 chars.
    client_id = "smog" + hashlib.sha256(setup_id.encode()).hexdigest()[:28]
    return {"instId": "SOL-USDT", "marginMode": margin_mode,
            "positionSide": "net", "side": "buy" if direction == "long" else "sell",
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
    for item in normalized_rows(result):
        if str(item.get("code", "0")) != "0":
            raise ValueError("BloFin rejected individual demo order action")
    return result


def normalized_rows(result: dict) -> list[dict]:
    """BloFin order responses are lists, while some TPSL responses are dicts."""
    data = result.get("data")
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list) and all(isinstance(row, dict) for row in data):
        return data
    return []


def accepted_id(result: dict, field: str = "orderId") -> str:
    rows = normalized_rows(result)
    if len(rows) != 1 or str(rows[0].get("code", "0")) != "0":
        raise ValueError("Ambiguous individual BloFin response")
    identifier = str(rows[0].get(field) or "")
    if not identifier.isdigit():
        raise ValueError("Missing valid BloFin identifier")
    return identifier


def client_action_id(setup_id: str, action: str) -> str:
    return "smog" + hashlib.sha256((setup_id + "|" + action).encode()).hexdigest()[:28]


def _exchange_id(value: str) -> str:
    value = str(value or "")
    if not value.isdigit() or len(value) > 64:
        raise ValueError("Invalid exchange identifier")
    return value


def submit_partial_close(setup_id: str, direction: str, size) -> dict:
    """Market reduce-only close of the preapproved demo quantity."""
    q = _positive(size)
    if q % _positive(demo_instrument()["lotSize"]) != 0:
        raise ExecutionBlocked("Partial close violates contract lot increment")
    if direction not in ("long", "short"):
        raise ValueError("Invalid direction")
    return _signed_post("/api/v1/trade/order", {
        "instId": "SOL-USDT", "marginMode": "cross", "positionSide": "net",
        "side": "sell" if direction == "long" else "buy",
        "orderType": "market", "size": str(q), "reduceOnly": "true",
        "clientOrderId": client_action_id(setup_id, "partial_close"),
    })


def submit_emergency_close(setup_id: str, direction: str, size) -> dict:
    """Last-resort demo reduce-only close when exchange protection is absent."""
    q = _positive(size)
    if q % _positive(demo_instrument()["lotSize"]):
        raise ExecutionBlocked("Emergency close violates contract increment")
    if direction not in ("long", "short"):
        raise ValueError("Invalid direction")
    return _signed_post("/api/v1/trade/order", {
        "instId": "SOL-USDT", "marginMode": "cross", "positionSide": "net",
        "side": "sell" if direction == "long" else "buy",
        "orderType": "market", "size": str(q), "reduceOnly": "true",
        "clientOrderId": client_action_id(setup_id, "emergency_exit"),
    })


def amend_protective_stop(tpsl_id: str, price, request_id: str) -> dict:
    """Modify a verified existing standalone protective stop."""
    stop = _positive(price)
    tick = _positive(demo_instrument()["tickSize"])
    if stop % tick:
        raise ExecutionBlocked("Stop violates contract tick increment")
    if not request_id.startswith("smog") or len(request_id) > 32:
        raise ValueError("Invalid action idempotency key")
    return _signed_post("/api/v1/trade/amend-tpsl", {
        "instId": "SOL-USDT", "tpslId": _exchange_id(tpsl_id),
        "requestId": request_id,
        "newSlTriggerPrice": str(stop), "newSlOrderPrice": "-1",
    })


def place_emergency_stop(setup_id: str, direction: str, size, price) -> dict:
    """Protect an existing demo position if its attached SL is absent."""
    q, stop = _positive(size), _positive(price)
    spec = demo_instrument()
    if q % _positive(spec["lotSize"]) or stop % _positive(spec["tickSize"]):
        raise ExecutionBlocked("Emergency stop violates contract increments")
    if direction not in ("long", "short"):
        raise ValueError("Invalid direction")
    return _signed_post("/api/v1/trade/order-tpsl", {
        "instId": "SOL-USDT", "marginMode": "cross", "positionSide": "net",
        "side": "sell" if direction == "long" else "buy", "size": str(q),
        "slTriggerPrice": str(stop), "slOrderPrice": "-1",
        "slTriggerPriceType": "last", "reduceOnly": "true",
        "clientOrderId": client_action_id(setup_id, "emergency_stop"),
    })


def check_contract_and_account(proposal: dict, max_notional_usdt: Decimal = Decimal("100")) -> None:
    """Fail closed on live account mode, open exposure and contract geometry."""
    mode = demo_read("position_mode").get("data") or {}
    margin = demo_read("margin_mode").get("data") or {}
    if mode.get("positionMode") != "net_mode" or margin.get("marginMode") != "cross":
        raise ExecutionBlocked("Unexpected demo account position or margin mode")
    if mode.get("multiPosition") not in (False, "false", None):
        raise ExecutionBlocked("Multi-position mode unsupported")
    for endpoint in ("positions", "orders", "tpsl"):
        rows = demo_read(endpoint, {"instId": "SOL-USDT"}).get("data") or []
        if rows:
            raise ExecutionBlocked(f"Existing demo {endpoint} require reconciliation")
    instrument = demo_instrument()
    if instrument.get("state") != "live" or instrument.get("apiPermission") != "full":
        raise ExecutionBlocked("Instrument is not available for opening orders")
    price = _positive(proposal["price"])
    size = _positive(proposal["size"])
    stop = _positive(proposal["slTriggerPrice"])
    tick = _positive(instrument["tickSize"])
    lot = _positive(instrument["lotSize"])
    minimum = _positive(instrument["minSize"])
    if price % tick or stop % tick or size % lot or size < max(minimum, 2 * lot):
        raise ExecutionBlocked("Entry, stop or contract quantity violates exchange increments")
    if price * size * _positive(instrument["contractValue"]) > max_notional_usdt:
        raise ExecutionBlocked("Demo notional cap exceeded")
    if abs(price - stop) * size * _positive(instrument["contractValue"]) > Decimal("1"):
        raise ExecutionBlocked("Demo stop-loss risk cap exceeded")


def submit_order(proposal: dict) -> dict:
    """Real demo exchange order; intentionally gated off by default."""
    allowed = {"instId", "marginMode", "positionSide", "side", "orderType",
               "price", "size", "slTriggerPrice", "slOrderPrice",
               "slTriggerPriceType", "clientOrderId"}
    if set(proposal) != allowed or proposal["instId"] != "SOL-USDT":
        raise ValueError("Unexpected order fields or instrument")
    if proposal["orderType"] != "limit" or proposal["marginMode"] != "cross" or proposal["positionSide"] != "net":
        raise ValueError("Unsupported order mode")
    direction = "long" if proposal["side"] == "buy" else "short" if proposal["side"] == "sell" else ""
    expected = prepare_limit_order("validation", direction, proposal["price"],
                                   proposal["size"], proposal["slTriggerPrice"])
    for key in ("side", "slOrderPrice", "slTriggerPriceType"):
        if proposal[key] != expected[key]:
            raise ValueError("Invalid order parameters")
    if not str(proposal["clientOrderId"]).startswith("smog"):
        raise ValueError("Invalid client order id")
    check_contract_and_account(proposal)
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
