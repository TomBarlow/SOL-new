"""Read-only BloFin ADX prequalification for SMOG. Never places orders.

Unknown/insufficient history is UNVERIFIED, not a trading-rule rejection.
Uses closed candles at the signal timestamp, not the current market.
"""
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from smog_market_data import Candle, wilder_adx, validate_candles

BASE = "https://openapi.blofin.com/api/v1/market/candles"
INTERVALS = {"1m": 60_000, "5m": 300_000}


def fetch_closed(bar: str, asof_ms: int, limit: int = 200) -> list[Candle]:
    if bar not in INTERVALS or asof_ms <= 0:
        raise ValueError("Unsupported bar or timestamp")
    interval = INTERVALS[bar]
    # BloFin's 'after' returns older rows, newest first. Query at the
    # start of the next bar, then discard any not closed at asof.
    end = (asof_ms // interval) * interval
    params = urlencode({"instId": "SOL-USDT", "bar": bar,
                        "after": str(end), "limit": str(limit)})
    req = Request(BASE + "?" + params, headers={"User-Agent": "smog-adx/1.0"})
    with urlopen(req, timeout=5) as response:
        body = json.load(response)
    if str(body.get("code")) != "0" or not isinstance(body.get("data"), list):
        raise ValueError("BloFin candle response invalid")
    candles = []
    for row in body["data"]:
        if len(row) < 9 or str(row[8]) != "1":
            continue
        start = int(row[0])
        if start + interval > asof_ms:
            continue
        candles.append(Candle(start, *(float(row[i]) for i in (1, 2, 3, 4, 5))))
    candles.sort(key=lambda c: c.start_ms)
    if len(candles) < 32:
        raise ValueError("Insufficient closed candle history")
    validate_candles(candles, interval)
    # No lookahead and no silent substitution with current candles.
    if asof_ms - (candles[-1].start_ms + interval) >= interval:
        raise ValueError("Missing candle at evaluation timestamp")
    return candles


def _metrics(candles):
    values = [v for v in wilder_adx(candles, 14) if v is not None]
    if len(values) < 3:
        raise ValueError("Insufficient ADX warmup")
    current, prior, older = values[-1], values[-2], values[-3]
    slope = current - prior
    acceleration = slope - (prior - older)
    return {"adx": round(current, 5), "slope": round(slope, 5),
            "acceleration": round(acceleration, 5),
            "trend": "rising" if slope > 0 else "falling" if slope < 0 else "flat",
            "momentum": "accelerating" if acceleration > 0 else
                        "decelerating" if acceleration < 0 else "steady",
            "last_closed_bar_ms": candles[-1].start_ms}


def evaluate_adx(asof_ms: int, off_choch: bool = False) -> dict:
    """Returns PASS/REJECT/UNVERIFIED with auditable metrics.

    Off-CHoCH cannot be inferred from current event payloads: caller must
    explicitly supply the classification, never default a known exception.
    """
    try:
        m1 = _metrics(fetch_closed("1m", asof_ms))
        m5 = _metrics(fetch_closed("5m", asof_ms))
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        return {"status": "UNVERIFIED", "reason": type(exc).__name__,
                "asof_ms": asof_ms, "period": 14}
    passed = (m1["adx"] < 20 and m5["adx"] < 20) if off_choch else (
        m1["adx"] < 25 or m5["adx"] < 25)
    return {"status": "PASS" if passed else "REJECT",
            "reason": "ADX_THRESHOLD_PASS" if passed else "ADX_THRESHOLD_FAIL",
            "asof_ms": asof_ms, "period": 14, "off_choch": off_choch,
            "adx_1m": m1, "adx_5m": m5}
