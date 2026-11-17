"""Closed-bar reconstruction of LuxAlgo SMC market-structure signals.

Research implementation based on the published Smart Money Concepts [LuxAlgo]
Pine v5 indicator (CC BY-NC-SA 4.0, © LuxAlgo), using the user's 2026-11-17
TradingView inputs. This module executes no orders and makes no network calls.

The TradingView SMC indicator has Fair Value Gaps turned OFF. SMOG's separate
LuxAlgo Fair Value Gap alerts must continue to be validated independently.
The chart's historical warm-up and TradingView/BloFin candle differences can
affect parity, so results are observations, never execution authorisation.
"""

from dataclasses import dataclass

from smog_market_data import validate_candles

MINUTE_MS = 60_000
INTERNAL_LENGTH = 5
SWING_LENGTH = 50


@dataclass
class _Pivot:
    level: float | None = None
    bar_ms: int | None = None
    crossed: bool = False


class _Structure:
    def __init__(self, length: int):
        self.length = length
        self.leg = 0  # Initial LuxAlgo trend/leg state.
        self.bias = 0
        self.high = _Pivot()
        self.low = _Pivot()

    def update(self, candles, index):
        """Confirm the delayed high/low that starts a new opposite leg."""
        if index < self.length:
            return
        pivot = candles[index - self.length]
        following = candles[index - self.length + 1:index + 1]
        new_leg = self.leg
        if pivot.high > max(c.high for c in following):
            new_leg = 0
        elif pivot.low < min(c.low for c in following):
            new_leg = 1
        if new_leg != self.leg:
            selected = self.low if new_leg == 1 else self.high
            selected.level = pivot.low if new_leg == 1 else pivot.high
            selected.bar_ms = pivot.start_ms
            selected.crossed = False
        self.leg = new_leg


def replay_structure(candles, *, swing_length: int = SWING_LENGTH,
                     confluence_filter: bool = False):
    """Reproduce closed-bar internal/swing BOS and CHoCH pivot-cross events.

    The LuxAlgo script confirms a pivot with bars to its right and labels a
    break using a close crossover of the current pivot. Its internal filter
    also excludes pivots equal to the corresponding swing pivot. Inputs are
    defaults verified against the supplied TradingView settings screenshots.
    """
    if swing_length < 10:
        raise ValueError("Swing length must be at least 10")
    if candles:
        validate_candles(candles, MINUTE_MS)
    internal = _Structure(INTERNAL_LENGTH)
    swing = _Structure(swing_length)
    events = []

    for index, candle in enumerate(candles):
        # The Pine ta.crossover/ta.crossunder calls compare against the prior
        # bar's pivot level, which can differ from the newly confirmed level.
        previous_levels = {
            (kind, side): getattr(structure, side).level
            for kind, structure in (("internal", internal), ("swing", swing))
            for side in ("high", "low")
        }
        swing.update(candles, index)
        internal.update(candles, index)
        if index == 0:
            continue

        previous_close = candles[index - 1].close
        bullish_wick = (candle.high - max(candle.close, candle.open) >
                        min(candle.close, candle.open) - candle.low)
        bearish_wick = (candle.high - max(candle.close, candle.open) <
                        min(candle.close, candle.open) - candle.low)

        for kind, structure in (("internal", internal), ("swing", swing)):
            for side, direction in (("high", "bullish"), ("low", "bearish")):
                pivot = getattr(structure, side)
                prev_level = previous_levels[(kind, side)]
                if pivot.level is None or prev_level is None or pivot.crossed:
                    continue
                if kind == "internal":
                    swing_level = getattr(swing, side).level
                    # A missing swing level behaves as an unknown comparison
                    # in Pine. It must not create a synthetic internal break.
                    if swing_level is None or pivot.level == swing_level:
                        continue
                    if confluence_filter and not (
                        bullish_wick if side == "high" else bearish_wick
                    ):
                        continue
                if side == "high":
                    crossed = previous_close <= prev_level and candle.close > pivot.level
                    tag = "CHoCH" if structure.bias == -1 else "BOS"
                else:
                    crossed = previous_close >= prev_level and candle.close < pivot.level
                    tag = "CHoCH" if structure.bias == 1 else "BOS"
                if not crossed:
                    continue
                opposite = structure.low if side == "high" else structure.high
                events.append({
                    "structure": kind,
                    "direction": direction,
                    "event": tag,
                    "bar_ms": candle.start_ms,
                    "break_level": pivot.level,
                    "break_pivot_ms": pivot.bar_ms,
                    "opposite_pivot_level": opposite.level,
                    "opposite_pivot_ms": opposite.bar_ms,
                    "close": candle.close,
                })
                pivot.crossed = True
                structure.bias = 1 if side == "high" else -1
    return events


def compare_choch(candidate, candles, geometry=None, *,
                  swing_length: int = SWING_LENGTH):
    """Compare an official TradingView internal CHoCH alert with replay.

    A matching break identifies its candidate pivot from the published code.
    This comparison remains research-only until timestamp parity is checked
    on enough observed alerts and candle feeds/settings are aligned.
    """
    evidence = {
        "method": "luxalgo_smc_pine_v5_close_bar_replay",
        "inputs": {"internal_length": INTERNAL_LENGTH,
                   "swing_length": swing_length, "confluence_filter": False},
        "research_only": True,
        "authoritative": False,
        "execution_allowed": False,
    }
    try:
        bars = candidate["signal_bars"]
        target = int(bars["internal_choch"])
        direction = {"long": "bullish", "short": "bearish"}[candidate["direction"]]
        history = [c for c in candles if c.start_ms <= target]
        if (len(history) < 2 * swing_length or
                not history or history[-1].start_ms != target):
            return {**evidence, "status": "UNVERIFIABLE",
                    "reason": "INSUFFICIENT_HISTORY_OR_CLOSED_CHOCH_BAR"}
        events = replay_structure(history, swing_length=swing_length)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        return {**evidence, "status": "UNVERIFIABLE",
                "reason": "INVALID_CANDLES_OR_SIGNAL", "detail": str(exc)[:100]}

    on_bar = [event for event in events if event["bar_ms"] == target and
              event["structure"] == "internal"]
    matching = next((event for event in on_bar if event["direction"] == direction
                     and event["event"] == "CHoCH"), None)
    if matching:
        result = {**evidence, "status": "MATCHED_CHOCH", "signal_bar_ms": target,
                "break_level": matching["break_level"],
                "break_pivot_ms": matching["break_pivot_ms"],
                "opposite_pivot_level": matching["opposite_pivot_level"],
                "opposite_pivot_ms": matching["opposite_pivot_ms"],
                "choch_close": matching["close"]}
        origin_ms = matching["opposite_pivot_ms"]
        if geometry and geometry.get("status") == "VERIFIED" and origin_ms is not None:
            leg = [bar for bar in history if origin_ms <= bar.start_ms <= target]
            if leg:
                leg_low = min(bar.low for bar in leg)
                leg_high = max(bar.high for bar in leg)
                if leg_high > leg_low:
                    midpoint = float(geometry["midpoint"])
                    depth = ((leg_high - midpoint) / (leg_high - leg_low)
                             if direction == "bullish" else
                             (midpoint - leg_low) / (leg_high - leg_low))
                    result["r4_research"] = {
                        "method": "luxalgo_confirmed_opposite_pivot_to_choch_extreme",
                        "origin_ms": origin_ms,
                        "extreme_ms": target,
                        "swing_low": round(leg_low, 8),
                        "swing_high": round(leg_high, 8),
                        "fvg_midpoint": round(midpoint, 8),
                        "midpoint_retracement": round(depth, 6),
                        "midpoint_at_or_beyond_50": depth >= 0.5,
                        "fvg_inside_confirmed_pivot_window": (
                            origin_ms <= int(candidate["signal_bars"]["fvg_formed"]) <= target),
                        "research_only": True,
                        "authoritative": False,
                    }
        return result
    return {**evidence, "status": "NO_MATCH", "signal_bar_ms": target,
            "observed_internal_events_on_bar": [
                {"direction": item["direction"], "event": item["event"]}
                for item in on_bar],
            "research_reason": "CHECK_CANDLE_FEED_AND_INDICATOR_HISTORY"}
