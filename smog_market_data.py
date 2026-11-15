"""Read-only OHLCV validation and Wilder ADX for the SMOG research engine.

This module does not infer proprietary OG/CHoCH/FVG signals or place orders.
Candles must be CLOSED and supplied in chronological order by a trusted feed.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Sequence


@dataclass(frozen=True)
class Candle:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self):
        if self.start_ms < 0 or any(not isfinite(v) for v in
            (self.open, self.high, self.low, self.close, self.volume)):
            raise ValueError("Invalid candle")
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            raise ValueError("Invalid candle price/volume")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ValueError("Inconsistent OHLC")


def validate_candles(candles: Sequence[Candle], interval_ms: int) -> None:
    """Reject gaps, duplicate bars, out-of-order bars, and unaligned timestamps."""
    if interval_ms <= 0:
        raise ValueError("Invalid interval")
    for i, candle in enumerate(candles):
        if candle.start_ms % interval_ms:
            raise ValueError("Unaligned candle timestamp")
        if i and candle.start_ms - candles[i - 1].start_ms != interval_ms:
            raise ValueError("Missing, duplicate, or out-of-order candle")


def aggregate_5m(candles_1m: Sequence[Candle]) -> list[Candle]:
    """Aggregate only complete, aligned blocks of five consecutive 1m bars."""
    validate_candles(candles_1m, 60_000)
    result = []
    groups: dict[int, list[Candle]] = {}
    for candle in candles_1m:
        bucket = (candle.start_ms // 300_000) * 300_000
        groups.setdefault(bucket, []).append(candle)
    for start, group in sorted(groups.items()):
        if len(group) != 5 or group[0].start_ms != start or group[-1].start_ms != start + 240_000:
            continue
        result.append(Candle(start, group[0].open, max(c.high for c in group),
                             min(c.low for c in group), group[-1].close,
                             sum(c.volume for c in group)))
    return result


def wilder_adx(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    """Wilder-smoothed ADX; first value at candle index 2*period-1.

    ADX at index i uses only bars <= i. No forward-looking values.
    """
    if period < 2:
        raise ValueError("ADX period must be >= 2")
    if len(candles) < 2:
        return [None] * len(candles)
    # ADX is only meaningful on consecutive bars; infer the interval from first pair.
    interval = candles[1].start_ms - candles[0].start_ms
    validate_candles(candles, interval)
    n = len(candles)
    out: list[float | None] = [None] * n
    if n < 2 * period:
        return out
    tr, pdm, mdm = [], [], []
    for prev, cur in zip(candles, candles[1:]):
        up = cur.high - prev.high
        down = prev.low - cur.low
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    smooth_tr = sum(tr[:period])
    smooth_p = sum(pdm[:period])
    smooth_m = sum(mdm[:period])

    def dx(st: float, sp: float, sm: float) -> float:
        if st <= 0:
            return 0.0
        plus, minus = 100.0 * sp / st, 100.0 * sm / st
        return 100.0 * abs(plus - minus) / (plus + minus) if plus + minus else 0.0

    dx_values = [dx(smooth_tr, smooth_p, smooth_m)]
    for j in range(period, len(tr)):
        smooth_tr = smooth_tr - smooth_tr / period + tr[j]
        smooth_p = smooth_p - smooth_p / period + pdm[j]
        smooth_m = smooth_m - smooth_m / period + mdm[j]
        dx_values.append(dx(smooth_tr, smooth_p, smooth_m))
    first_adx = sum(dx_values[:period]) / period
    out[2 * period - 1] = first_adx
    previous = first_adx
    for k, value in enumerate(dx_values[period:], start=2 * period):
        previous = (previous * (period - 1) + value) / period
        out[k] = previous
    return out


def latest_closed_adx(candles: Sequence[Candle], asof_ms: int, interval_ms: int,
                      period: int = 14) -> float | None:
    """Use only bars whose END is <= asof_ms; never use an open candle."""
    eligible = [c for c in candles if c.start_ms + interval_ms <= asof_ms]
    if not eligible:
        return None
    values = wilder_adx(eligible, period)
    return values[-1]


def adx_entry_gate(adx_1m: float | None, adx_5m: float | None) -> bool:
    """Strict SMOG rule: at least one ADX < 25; unknown data fails closed."""
    if adx_1m is None or adx_5m is None:
        return False
    if not (isfinite(adx_1m) and isfinite(adx_5m)):
        return False
    return adx_1m < 25.0 or adx_5m < 25.0
