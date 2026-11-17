"""Zero-inference SMOG R4 sensitivity research. Never authorises an order.

TradingView's CHoCH event lacks its proprietary displacement swing anchors.
All reconstructed anchors here are explicitly PROXIES, not rule confirmation.
Only closed, contiguous 1m candles up to the CHoCH bar may be used.
"""
import json
import logging
import os
import time

import psycopg
from psycopg.types.json import Jsonb

from smog_adx_filter import fetch_closed

log = logging.getLogger("smog.r4.research")
ONE_MINUTE_MS = 60_000
THRESHOLDS = {"25": 0.25, "38_2": 0.382, "50": 0.5, "61_8": 0.618}
METHODS = ("og_to_choch", "last_5m", "last_10m", "last_20m", "confirmed_pivot")


def init_r4_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smog_r4_replays (
            setup_id TEXT PRIMARY KEY REFERENCES smog_tasks(candidate_key),
            evidence JSONB NOT NULL,
            assessed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def _score(window, direction, midpoint, name):
    if not window:
        return {"status": "UNVERIFIABLE", "reason": "NO_CANDLES", "method": name}
    low = min(c.low for c in window)
    high = max(c.high for c in window)
    if high <= low or not low <= midpoint <= high:
        return {"status": "UNVERIFIABLE", "reason": "MIDPOINT_OUTSIDE_RANGE",
                "method": name}
    depth = ((high - midpoint) / (high - low) if direction == "long"
             else (midpoint - low) / (high - low))
    return {
        "status": "PROXY_OBSERVED", "method": name,
        "start_ms": window[0].start_ms,
        "end_ms": window[-1].start_ms,
        "swing_low": round(low, 8), "swing_high": round(high, 8),
        "fvg_midpoint": round(midpoint, 8),
        "retracement": round(depth, 6),
        "thresholds": {key: depth >= value for key, value in THRESHOLDS.items()},
        "not_authoritative_swing": True,
    }


def _confirmed_pivot_index(window, direction, pivot_width=2):
    """Most recent symmetric pivot whose right-hand candles are CHoCH-or-earlier."""
    if len(window) < 2 * pivot_width + 1:
        return None
    for j in range(len(window) - 1 - pivot_width, pivot_width - 1, -1):
        neighbors = window[j - pivot_width:j] + window[j + 1:j + pivot_width + 1]
        val = window[j].low if direction == "long" else window[j].high
        if (direction == "long" and all(val < c.low for c in neighbors)
                or direction == "short" and all(val > c.high for c in neighbors)):
            return j
    return None


def assess_r4(candidate: dict, candles: list) -> dict:
    """Return anchor/threshold sensitivity, NEVER executable eligibility."""
    output = {"status": "UNVERIFIABLE", "research_only": True,
              "execution_allowed": False, "anchor_authoritative": False,
              "methodology": "closed_1m_proxy_anchor_sensitivity_v1"}
    bars = candidate.get("signal_bars") or {}
    fvg = candidate.get("fvg") or {}
    direction = candidate.get("direction")
    try:
        og_ms, choch_ms = int(bars["og"]), int(bars["internal_choch"])
        formed_ms = int(fvg["formation_bar_ms"])
        lower, upper = float(fvg["lower"]), float(fvg["upper"])
    except (TypeError, ValueError, KeyError):
        return output | {"reason": "MISSING_SIGNAL_OR_GAP_GEOMETRY"}
    if (direction not in ("long", "short") or fvg.get("status") != "VERIFIED"
            or not (0 < lower < upper) or
            not (0 <= og_ms <= formed_ms <= choch_ms) or
            any(ms % ONE_MINUTE_MS for ms in (og_ms, formed_ms, choch_ms))):
        return output | {"reason": "UNVERIFIED_OR_NONDISPLACEMENT_SIGNAL_ORDER"}
    ordered = [c for c in candles if og_ms <= c.start_ms <= choch_ms]
    expected = (choch_ms - og_ms) // ONE_MINUTE_MS + 1
    if (len(ordered) != expected or not ordered or
            ordered[0].start_ms != og_ms or
            ordered[-1].start_ms != choch_ms or
            any(ordered[i + 1].start_ms - ordered[i].start_ms != ONE_MINUTE_MS
                for i in range(len(ordered) - 1))):
        return output | {"reason": "INCOMPLETE_CLOSED_DISPLACEMENT_WINDOW"}
    midpoint = (upper + lower) / 2
    scores = {"og_to_choch": _score(ordered, direction, midpoint, "og_to_choch")}
    for minutes in (5, 10, 20):
        scores[f"last_{minutes}m"] = _score(
            ordered[-minutes:], direction, midpoint, f"last_{minutes}m")
    pivot_idx = _confirmed_pivot_index(ordered, direction)
    scores["confirmed_pivot"] = (_score(
        ordered[pivot_idx:], direction, midpoint, "confirmed_pivot")
        if pivot_idx is not None else
        {"status": "UNVERIFIABLE", "reason": "NO_CONFIRMED_PIVOT",
         "method": "confirmed_pivot"})
    readable = [x for x in scores.values() if x["status"] == "PROXY_OBSERVED"]
    consensus = {}
    for key in THRESHOLDS:
        passing = sum(bool(x["thresholds"][key]) for x in readable)
        consensus[key] = {"observed_methods": len(readable),
                          "proxy_pass_methods": passing,
                          "proxy_fail_methods": len(readable) - passing,
                          "disagreement": 0 < passing < len(readable)}
    return output | {
        "status": "PROXY_OBSERVED" if readable else "UNVERIFIABLE",
        "reason": None if readable else "NO_VALID_RANGE_PROXY",
        "signal_bars": {"og": og_ms, "internal_choch": choch_ms,
                        "fvg_formed": formed_ms},
        "fvg_midpoint": round(midpoint, 8),
        "proxy_results": scores, "sensitivity": consensus,
        "rule_4_confirmed": False,
        "requires_proprietary_choch_leg_evidence": True,
    }


def _log_research_counts(conn_factory, completed):
    # Operational evidence is aggregate only, with no setup IDs or API secrets.
    # Counts intentionally distinguish observed false from unverifiable.
    with conn_factory() as conn:
        totals = conn.execute("""
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE evidence->>'status'='PROXY_OBSERVED'),
                   COUNT(*) FILTER (WHERE
                     evidence#>>'{proxy_results,og_to_choch,thresholds,50}' = 'true'),
                   COUNT(*) FILTER (WHERE
                     evidence#>>'{proxy_results,og_to_choch,thresholds,50}' = 'false'),
                   COUNT(*) FILTER (WHERE
                     evidence#>>'{proxy_results,confirmed_pivot,thresholds,50}' = 'true'),
                   COUNT(*) FILTER (WHERE
                     evidence#>>'{proxy_results,confirmed_pivot,thresholds,50}' = 'false'),
                   COUNT(*) FILTER (WHERE
                     evidence#>>'{sensitivity,50,disagreement}' = 'true')
            FROM smog_r4_replays
        """).fetchone()
    log.info("SMOG_R4_BACKFILL processed=%s total=%s observed=%s "
             "og_proxy_50_pass=%s og_proxy_50_fail=%s "
             "pivot_proxy_50_pass=%s pivot_proxy_50_fail=%s "
             "anchor_disagreement_50=%s inference_tokens=0",
             completed, *totals)


def backfill_once(conn_factory, limit=12):
    """At most limit previously unprocessed observations; read-only market data."""
    with conn_factory() as conn:
        tasks = conn.execute("""
            SELECT t.candidate_key, t.payload
            FROM smog_tasks AS t
            LEFT JOIN smog_r4_replays AS r ON r.setup_id=t.candidate_key
            WHERE r.setup_id IS NULL
              AND t.created_at < now() - interval '2 minutes'
              AND t.status IN ('decided','expired')
            ORDER BY t.id DESC LIMIT %s
        """, (limit,)).fetchall()
    if not tasks:
        _log_research_counts(conn_factory, 0)
        return 0
    completed = 0
    for key, payload in tasks:
        try:
            choch_ms = int((payload.get("signal_bars") or {})["internal_choch"])
            # The CHoCH bar is known only once its minute has closed.
            candles = fetch_closed("1m", choch_ms + ONE_MINUTE_MS, limit=200)
            evidence = assess_r4(payload, candles)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            evidence = {"status": "UNVERIFIABLE", "research_only": True,
                        "execution_allowed": False, "anchor_authoritative": False,
                        "reason": type(exc).__name__, "retry_needed": True}
        with conn_factory() as conn:
            conn.execute("""
                INSERT INTO smog_r4_replays(setup_id, evidence)
                VALUES (%s,%s)
                ON CONFLICT (setup_id) DO NOTHING
            """, (key, Jsonb(evidence)))
        completed += 1
    _log_research_counts(conn_factory, completed)
    return completed


def run_r4_backfill(conn_factory):
    """Runs separately from trading worker; never delays candidate/order processing."""
    while True:
        try:
            count = backfill_once(conn_factory)
            time.sleep(5 if count else 120)
        except Exception:
            log.exception("R4 research batch failed; independent execution unaffected")
            time.sleep(60)
