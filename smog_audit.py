"""Durable 50-resolved-setup audit based on actual and shadow observations.

Shadow outcomes report market paths and FVG touches. They never claim a fill,
a realized P&L or an executable hypothetical trading performance.
"""
import logging
from datetime import datetime, timezone, timedelta
from collections import Counter

from psycopg.types.json import Jsonb

from smog_adx_filter import fetch_closed

log = logging.getLogger("smog.audit")
SHADOW_MINUTES = 60
SETTLEMENT_MINUTES = 90


def init_audit(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smog_shadow_outcomes (
            setup_id TEXT PRIMARY KEY REFERENCES smog_tasks(candidate_key),
            outcome JSONB NOT NULL,
            resolved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            audited_at TIMESTAMPTZ
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smog_audit_batches (
            id BIGSERIAL PRIMARY KEY,
            through_task_id BIGINT NOT NULL,
            resolved_count INTEGER NOT NULL,
            metrics JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def _shadow(candidate):
    fvg = candidate.get("fvg") or {}
    direction = candidate.get("direction")
    start_ms = int(candidate.get("asof_bar_time_ms") or 0)
    if start_ms <= 0 or direction not in ("long", "short"):
        return {"quality": "INVALID_CANDIDATE", "shadow_only": True}
    try:
        candles = fetch_closed("1m", start_ms + SHADOW_MINUTES * 60_000,
                               limit=160)
        window = [c for c in candles
                  if start_ms <= c.start_ms < start_ms + SHADOW_MINUTES * 60_000]
        if len(window) < SHADOW_MINUTES - 1:
            raise ValueError("Shadow candle window incomplete")
        result = {"quality": "OBSERVED", "shadow_only": True,
                  "first_bar_ms": window[0].start_ms,
                  "last_bar_ms": window[-1].start_ms,
                  "high": max(c.high for c in window),
                  "low": min(c.low for c in window)}
        if fvg.get("status") == "VERIFIED":
            lower, upper = float(fvg["lower"]), float(fvg["upper"])
            touches = [c.start_ms for c in window if (
                c.low <= upper if direction == "long" else c.high >= lower)]
            result["fvg_touched"] = bool(touches)
            result["first_fvg_touch_ms"] = touches[0] if touches else None
            result["fvg_identity"] = fvg.get("identity")
        return result
    except Exception:
        return {"quality": "DATA_UNAVAILABLE", "shadow_only": True}


def settle_observations(conn_factory):
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=SETTLEMENT_MINUTES)
    with conn_factory() as conn:
        conn.execute("""
            UPDATE smog_tasks SET status='expired', lease_until=NULL
            WHERE status IN ('pending','leased')
              AND created_at < now() - interval '10 minutes'
        """)
        tasks = conn.execute("""
            SELECT t.id, t.candidate_key, t.payload, t.decision,
                   t.status, i.status, p.closed_at
            FROM smog_tasks t
            LEFT JOIN smog_execution_intents i ON i.setup_id=t.candidate_key
            LEFT JOIN smog_managed_positions p ON p.setup_id=t.candidate_key
            LEFT JOIN smog_shadow_outcomes s ON s.setup_id=t.candidate_key
            WHERE s.setup_id IS NULL
              AND t.created_at < %s
              AND t.status IN ('decided','expired')
              AND (i.status IS NULL OR i.status NOT IN
                  ('submitted','unknown','submitting','ready','cancel_pending'))
              AND (p.intent_id IS NULL OR p.closed_at IS NOT NULL)
            ORDER BY t.id LIMIT 30
        """, (cutoff,)).fetchall()
    for task_id, key, payload, decision, task_status, execution_status, closed_at in tasks:
        observed = _shadow(payload)
        observed["decision"] = (decision or {}).get("decision", "UNDECIDED")
        observed["execution_status"] = execution_status
        observed["actual_position_closed"] = closed_at is not None
        with conn_factory() as conn:
            conn.execute("""
                INSERT INTO smog_shadow_outcomes(setup_id, outcome)
                VALUES (%s,%s) ON CONFLICT (setup_id) DO NOTHING
            """, (key, Jsonb(observed)))
    with conn_factory() as conn:
        pending = conn.execute("""
            SELECT s.setup_id, t.id, s.outcome, p.pnl_net
            FROM smog_shadow_outcomes s
            JOIN smog_tasks t ON t.candidate_key=s.setup_id
            LEFT JOIN smog_managed_positions p ON p.setup_id=s.setup_id
            WHERE s.audited_at IS NULL ORDER BY t.id LIMIT 50
        """).fetchall()
        if len(pending) != 50:
            return
        decisions = Counter((row[2] or {}).get("decision", "UNKNOWN") for row in pending)
        qualities = Counter((row[2] or {}).get("quality", "UNKNOWN") for row in pending)
        real = [float(row[3]) for row in pending if row[3] is not None]
        metrics = {
            "source": "50_settled_setup_observations",
            "decisions": dict(decisions),
            "shadow_quality": dict(qualities),
            "actual_closed_trade_count": len(real),
            "actual_realized_pnl_usdt_ex_funding": round(sum(real), 8),
            "actual_profitable_count": sum(p > 0 for p in real),
            "actual_unprofitable_count": sum(p < 0 for p in real),
            "recommendations_only": True,
        }
        conn.execute("""
            INSERT INTO smog_audit_batches
            (through_task_id, resolved_count, metrics)
            VALUES (%s, 50, %s)
        """, (max(row[1] for row in pending), Jsonb(metrics)))
        conn.execute("""
            UPDATE smog_shadow_outcomes SET audited_at=now()
            WHERE setup_id = ANY(%s) AND audited_at IS NULL
        """, ([row[0] for row in pending],))
        log.info("Recorded 50-setup audit batch through task %s",
                 max(row[1] for row in pending))
