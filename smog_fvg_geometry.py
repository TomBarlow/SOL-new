"""Reconstruct LuxAlgo FVG boundaries and identity from closed SOL 1m candles.

Source: Fair Value Gap [LuxAlgo] Pine v4.0, threshold=0.02%, Auto=false,
same-chart timeframe. Only the four formation/mitigation alerts are authoritative
signals. We reconstruct individual boundaries because their webhook messages
contain only {{close}} and the indicator's dynamic plot series are disabled.
"""
from hashlib import sha256
from smog_adx_filter import fetch_closed

THRESHOLD_FRACTION = 0.02 / 100
INTERVAL_MS = 60_000


def reconstruct(candles, formed_bar_ms: int, asof_bar_ms: int,
                direction: str) -> dict:
    if direction not in ("long", "short") or formed_bar_ms > asof_bar_ms:
        raise ValueError("Invalid FVG direction/timing")
    by_time = {c.start_ms: i for i, c in enumerate(candles)}
    index = by_time.get(formed_bar_ms)
    if index is None or index < 2:
        raise ValueError("Missing formation candle or lookback")
    current, middle, two_back = (candles[index],
                                 candles[index - 1], candles[index - 2])
    if (middle.start_ms != formed_bar_ms - INTERVAL_MS
            or two_back.start_ms != formed_bar_ms - 2 * INTERVAL_MS):
        raise ValueError("Formation candles are not contiguous")

    if direction == "long":
        upper, lower = current.low, two_back.high
        valid = (upper > lower and middle.close > lower
                 and (upper - lower) / lower > THRESHOLD_FRACTION)
    else:
        upper, lower = two_back.low, current.high
        valid = (lower < upper and middle.close < upper
                 and (upper - lower) / lower > THRESHOLD_FRACTION)
    if not valid:
        raise ValueError("Candle geometry does not reproduce LuxAlgo FVG")

    if asof_bar_ms not in by_time:
        raise ValueError("Missing as-of closed candle")
    if asof_bar_ms < formed_bar_ms:
        raise ValueError("Lookahead not allowed")

    after = candles[index + 1:by_time[asof_bar_ms] + 1]
    mitigated = any(c.close < lower for c in after) if direction == "long" else (
        any(c.close > upper for c in after))
    touched = any(c.low <= upper for c in after) if direction == "long" else (
        any(c.high >= lower for c in after))
    identity = sha256(f"SOL-USDT|1m|{direction}|{formed_bar_ms}".encode()).hexdigest()
    return {
        "status": "VERIFIED",
        "identity": identity,
        "direction": direction,
        "formation_bar_ms": formed_bar_ms,
        "asof_bar_ms": asof_bar_ms,
        "upper": round(upper, 8),
        "lower": round(lower, 8),
        "midpoint": round((upper + lower) / 2, 8),
        "mitigated": mitigated,
        "touched": touched,
        "mitigation_rule": "close below lower (bull) / above upper (bear)",
        "source": "blofin_closed_ohlc_reconstruction_of_luxalgo_v4",
        "threshold_percent": 0.02,
    }


def inspect_fvg(formed_bar_ms: int, asof_bar_ms: int, direction: str) -> dict:
    """Fail closed if the exact alert's FVG cannot be independently reproduced."""
    try:
        candles = fetch_closed("1m", asof_bar_ms + INTERVAL_MS)
        return reconstruct(candles, formed_bar_ms, asof_bar_ms, direction)
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        # Preserve the specific geometry/data failure for diagnostics. The
        # exception strings here describe read-only candle validation only.
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return {"status": "UNVERIFIED", "reason": reason[:160],
                "formation_bar_ms": formed_bar_ms, "asof_bar_ms": asof_bar_ms}

