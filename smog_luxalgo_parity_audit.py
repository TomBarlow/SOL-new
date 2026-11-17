"""One-shot closed-candle LuxAlgo SMC parity audit with zero inference/orders."""

import logging
import time
from collections import Counter

from smog_adx_filter import fetch_closed
from smog_luxalgo_structure import compare_choch, MINUTE_MS

log = logging.getLogger("smog.luxalgo.parity")


def evaluate_choch_rows(rows, fetcher=fetch_closed, delay_seconds=0.15):
    """Assess recorded alerts using candles closed at each actual signal bar."""
    results = Counter()
    reasons = Counter()
    for event, bar_ms in rows:
        direction = "long" if event == "internal_bullish_choch" else "short"
        candidate = {"direction": direction,
                     "signal_bars": {"internal_choch": int(bar_ms)}}
        try:
            candles = fetcher("1m", int(bar_ms) + MINUTE_MS)
            outcome = compare_choch(candidate, candles)
            results[outcome["status"]] += 1
            if outcome["status"] == "UNVERIFIABLE":
                reasons[outcome.get("reason", "UNKNOWN")] += 1
        except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
            results["UNVERIFIABLE"] += 1
            reasons[type(exc).__name__] += 1
        if delay_seconds:
            time.sleep(delay_seconds)
    return {"reviewed": len(rows), "outcomes": dict(results),
            "unverifiable_reasons": dict(reasons)}


def audit_luxalgo_parity(conn_factory):
    """Bounded historical audit. No database writes or model/exchange calls."""
    with conn_factory() as conn:
        rows = conn.execute("""
            SELECT DISTINCT event, bar_time FROM tv_events
            WHERE received_at > now() - interval '48 hours'
              AND event IN ('internal_bullish_choch', 'internal_bearish_choch')
            ORDER BY bar_time DESC
            LIMIT 100
        """).fetchall()
    report = evaluate_choch_rows(rows)
    log.info("SMOG_LUXALGO_PARITY window_hours=48 sample_limit=100 "
             "tv_choch=%s results=%s reasons=%s "
             "model_tokens=0 orders_sent=0",
             report["reviewed"], report["outcomes"],
             report["unverifiable_reasons"])
    return report
