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
    baseline = Counter()
    longer_history_changed = Counter()
    reasons = Counter()
    mismatches = Counter()
    mismatch_examples = []
    for event, bar_ms in rows:
        direction = "long" if event == "internal_bullish_choch" else "short"
        candidate = {"direction": direction,
                     "signal_bars": {"internal_choch": int(bar_ms)}}
        try:
            # BloFin permits up to 1440 candles/request. Replay 12h to
            # determine whether the old 200-bar chart warm-up was losing the
            # indicator's earlier pivot state, with one historical read.
            candles = fetcher("1m", int(bar_ms) + MINUTE_MS, limit=720)
            outcome = compare_choch(candidate, candles)
            short = compare_choch(candidate, candles[-200:])
            baseline[short["status"]] += 1
            if outcome["status"] != short["status"]:
                longer_history_changed[(short["status"], outcome["status"])] += 1
            results[outcome["status"]] += 1
            if outcome["status"] == "UNVERIFIABLE":
                reasons[outcome.get("reason", "UNKNOWN")] += 1
            elif outcome["status"] == "NO_MATCH":
                seen = outcome.get("observed_internal_events_on_bar") or []
                if any(item["direction"] == ("bullish" if direction == "long" else "bearish")
                       and item["event"] == "BOS" for item in seen):
                    reason = "BOS_INSTEAD_OF_CHOCH"
                elif seen:
                    reason = "OPPOSITE_INTERNAL_EVENT"
                else:
                    reason = "NO_INTERNAL_CROSS_ON_ALERT_BAR"
                mismatches[reason] += 1
                if len(mismatch_examples) < 8:
                    mismatch_examples.append({"bar_ms": int(bar_ms),
                                              "direction": direction, "reason": reason})
        except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
            results["UNVERIFIABLE"] += 1
            reasons[type(exc).__name__] += 1
        if delay_seconds:
            time.sleep(delay_seconds)
    return {"reviewed": len(rows), "outcomes": dict(results),
            "baseline_200": dict(baseline),
            "history_impact": {f"{old}_TO_{new}": count
                               for (old, new), count in longer_history_changed.items()},
            "mismatch_reasons": dict(mismatches),
            "mismatch_examples": mismatch_examples,
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
             "tv_choch=%s results=%s baseline_200=%s history_impact=%s "
             "reasons=%s mismatch_types=%s "
             "mismatch_examples=%s "
             "model_tokens=0 orders_sent=0",
             report["reviewed"], report["outcomes"],
             report["baseline_200"], report["history_impact"],
             report["unverifiable_reasons"], report["mismatch_reasons"],
             report["mismatch_examples"])
    return report
