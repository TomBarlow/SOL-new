"""Closed-candle CHoCH/FVG association evidence, never trade authorisation.

The proprietary TradingView CHoCH alert contains no actual pivot/swing anchors.
This module can test whether a plausible internal pivot was broken and whether
the selected FVG formed after the ensuing local swing, but the result remains
a labelled proxy and cannot serve as an authoritative R4 PASS or order gate.
"""
from smog_adx_filter import fetch_closed
from smog_fvg_geometry import reconstruct

MINUTE = 60_000


def _unverified(reason, **details):
    return {"status": "UNVERIFIABLE", "reason": reason,
            "method": "closed_1m_confirmed_pivot_break_proxy_v1",
            "research_only": True, "authoritative": False,
            "execution_allowed": False, **details}


def _pivot(rows, index, kind, width=2):
    if index < width or index + width >= len(rows):
        return False
    candle = rows[index]
    adjacent = rows[index - width:index] + rows[index + 1:index + width + 1]
    if kind == "high":
        return all(candle.high > x.high for x in adjacent)
    return all(candle.low < x.low for x in adjacent)


def assess_displacement(candidate, candles, geometry):
    """Retrospective structure snapshot using bars no later than CHoCH.

    No pivots are inferred from future bars. Signal timestamps are authoritative,
    but the pivot algorithm cannot reproduce a proprietary indicator's internals.
    """
    try:
        bars = candidate["signal_bars"]
        og, formed, choch = (int(bars[name]) for name in
                            ("og", "fvg_formed", "internal_choch"))
        direction = candidate["direction"]
        midpoint = float(geometry["midpoint"])
    except (KeyError, TypeError, ValueError):
        return _unverified("MISSING_SIGNAL_OR_FVG")
    if (direction not in ("long", "short") or geometry.get("status") != "VERIFIED"
            or not 0 <= og <= formed <= choch):
        return _unverified("OUTSIDE_ORDERED_OG_CHOCH_LEG")
    historic = [x for x in candles if x.start_ms <= choch]
    if not historic or historic[-1].start_ms != choch:
        return _unverified("MISSING_CHOCH_CLOSED_BAR")
    if any(b.start_ms - a.start_ms != MINUTE
           for a, b in zip(historic, historic[1:])):
        return _unverified("NONCONTIGUOUS_HISTORY")
    # The last two candles are not eligible pivots: a pivot needs two
    # complete right-hand confirmation candles by the CHoCH close.
    pivot_type = "high" if direction == "long" else "low"
    pivot_candidates = [i for i in range(2, len(historic) - 2)
                        if _pivot(historic, i, pivot_type)]
    if not pivot_candidates:
        return _unverified("NO_CONFIRMED_PRE_BREAK_PIVOT")
    pivot_idx = pivot_candidates[-1]
    level = historic[pivot_idx].high if direction == "long" else historic[pivot_idx].low
    prev_close = historic[-2].close
    close = historic[-1].close
    is_break = (prev_close <= level < close if direction == "long"
                else prev_close >= level > close)
    if not is_break:
        return _unverified(
            "PIVOT_BREAK_NOT_REPRODUCED",
            candidate_break_level=round(level, 8),
            previous_close=round(prev_close, 8), choch_close=round(close, 8))
    # Opposite extreme after the broken pivot is an impulse origin PROXY,
    # not an authoritative LuxAlgo swing. No post-CHoCH data is consulted.
    segment = historic[pivot_idx + 1:]
    if len(segment) < 2:
        return _unverified("INSUFFICIENT_DISPLACEMENT")
    origin = (min(segment, key=lambda x: x.low) if direction == "long"
              else max(segment, key=lambda x: x.high))
    if formed < origin.start_ms:
        return _unverified(
            "FVG_BEFORE_RECONSTRUCTED_IMPULSE",
            pivot_bar_ms=historic[pivot_idx].start_ms,
            proxy_impulse_start_ms=origin.start_ms,
            fvg_formation_ms=formed)
    leg = [c for c in segment if origin.start_ms <= c.start_ms <= choch]
    swing_low = min(c.low for c in leg)
    swing_high = max(c.high for c in leg)
    if not swing_low < swing_high:
        return _unverified("ZERO_IMPULSE_RANGE")
    if not swing_low <= midpoint <= swing_high:
        return _unverified(
            "FVG_MIDPOINT_OUTSIDE_RECONSTRUCTED_SWING",
            swing_low=round(swing_low, 8), swing_high=round(swing_high, 8))
    depth = ((swing_high - midpoint) / (swing_high - swing_low)
             if direction == "long"
             else (midpoint - swing_low) / (swing_high - swing_low))
    return {
        "status": "PROXY_SUPPORTS_ASSOCIATION",
        "method": "closed_1m_confirmed_pivot_break_proxy_v1",
        "research_only": True, "authoritative": False,
        "execution_allowed": False, "rule_4_confirmed": False,
        "break_level": round(level, 8),
        "break_pivot_bar_ms": historic[pivot_idx].start_ms,
        "choch_bar_ms": choch,
        "choch_close": round(close, 8),
        "fvg_bar_ms": formed,
        "proxy_impulse_start_bar_ms": origin.start_ms,
        "proxy_swing_low": round(swing_low, 8),
        "proxy_swing_high": round(swing_high, 8),
        "fvg_midpoint": round(midpoint, 8),
        "proxy_retracement": round(depth, 6),
        "proxy_midpoint_beyond_50": depth >= 0.5,
        "requires_authoritative_choch_swing_anchors": True,
    }


def inspect_candidate(candidate: dict, asof_bar_ms: int | None = None):
    """One read-only OHLC request for verified gap + structural observations."""
    signal_bars = candidate.get("signal_bars") or {}
    direction = candidate.get("direction")
    try:
        formed = int(signal_bars["fvg_formed"])
        asof = int(asof_bar_ms if asof_bar_ms is not None else
                   candidate["asof_bar_time_ms"])
        if direction not in ("long", "short"):
            raise ValueError("Invalid FVG direction")
        candles = fetch_closed("1m", asof + MINUTE)
        geometry = reconstruct(candles, formed, asof, direction)
        structure = assess_displacement(candidate, candles, geometry)
        return geometry, structure
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return ({"status": "UNVERIFIED", "reason": reason[:160],
                 "formation_bar_ms": signal_bars.get("fvg_formed"),
                 "asof_bar_ms": asof_bar_ms or candidate.get("asof_bar_time_ms")},
                _unverified("CANDLE_OR_GEOMETRY_UNAVAILABLE",
                            detail=reason[:120]))
