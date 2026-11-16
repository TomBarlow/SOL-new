"""Durable, demo-only execution of validated SMOG decisions.

A decision creates a blocked intent. Only a fresh, explicitly enabled TAKE
with a complete plan can be promoted. A failed/uncertain submission is never
blindly retried. This module has no connection to a real-money host.
"""
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
import psycopg
from smog_demo_execution import (
    ExecutionBlocked, accepted_id, prepare_limit_order, submit_order, demo_instrument
)
log = logging.getLogger("smog.demo.worker")


def enabled():
    return os.environ.get("SMOG_DEMO_ORDER_ENABLED") == "true"


def _connect():
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5)


def promote_fresh_intent(conn, setup_id: str, plan: dict):
    """Call from the same transaction that inserts the decision and intent."""
    if not enabled():
        return False
    if not isinstance(plan, dict):
        return False
    # The sole accepted decision contract is explicit entry, stop and
    # contracts. No guessed position size, entry price, or leverage.
    if not all(k in plan for k in ("direction", "entry", "stop", "size")):
        return False
    try:
        prepare_limit_order(setup_id, plan["direction"], plan["entry"],
                            plan["size"], plan["stop"])
    except (ValueError, TypeError, KeyError):
        return False
    row = conn.execute("""
      SELECT created_at, payload FROM smog_tasks WHERE candidate_key=%s
    """, (setup_id,)).fetchone()
    if not row or (datetime.now(timezone.utc) - row[0]).total_seconds() > 180:
        return False
    fvg = row[1].get("fvg") or {}
    if (fvg.get("status") != "VERIFIED" or fvg.get("mitigated") is not False
            or fvg.get("direction") != plan["direction"]):
        return False
    conn.execute("""
      UPDATE smog_execution_intents
      SET status='ready', updated_at=now()
      WHERE setup_id=%s AND status='blocked'
    """, (setup_id,))
    conn.execute("SELECT pg_notify('smog_demo_intents', %s)", (setup_id,))
    return True


def process_one():
    """At most one ready intent, with crash-safe at-most-one submission."""
    if not enabled():
        return False
    with _connect() as conn:
        row = conn.execute("""
          SELECT id, setup_id, trade_plan, created_at
          FROM smog_execution_intents WHERE status='ready'
          ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
        """).fetchone()
        if not row:
            return False
        intent_id, setup_id, plan, created_at = row
        if (datetime.now(timezone.utc) - created_at).total_seconds() > 180:
            conn.execute("""
              UPDATE smog_execution_intents SET status='rejected',
                  error_code='STALE_SETUP', updated_at=now()
              WHERE id=%s
            """, (intent_id,))
            return True
        conn.execute("""
          UPDATE smog_execution_intents SET status='submitting',
              updated_at=now() WHERE id=%s
        """, (intent_id,))
    # The committed 'submitting' marker prevents retry after process crash.
    try:
        from smog_fvg_geometry import inspect_fvg
        import time
        with _connect() as conn:
            task = conn.execute("""
                SELECT payload FROM smog_tasks WHERE candidate_key=%s
            """, (setup_id,)).fetchone()
        fvg = (task[0] if task else {}).get("fvg") or {}
        latest_closed_start = ((int(time.time() * 1000) // 60_000) - 1) * 60_000
        current_fvg = inspect_fvg(fvg.get("formation_bar_ms", 0),
                                  latest_closed_start, plan["direction"])
        if (current_fvg.get("status") != "VERIFIED"
                or current_fvg.get("identity") != fvg.get("identity")
                or current_fvg.get("mitigated") is not False):
            raise ExecutionBlocked("FVG mitigated or geometry unavailable at order time")
        from smog_adx_filter import evaluate_adx, fetch_closed
        tick = Decimal(str(demo_instrument()["tickSize"]))
        price = Decimal(str(plan["entry"]))
        lower = Decimal(str(current_fvg["lower"]))
        upper = Decimal(str(current_fvg["upper"]))
        midpoint = (lower + upper) / 2
        if ((plan["direction"] == "long" and not lower <= price <= midpoint)
                or (plan["direction"] == "short" and not midpoint <= price <= upper)):
            raise ExecutionBlocked("Entry must be in the deeper 50% of the identified FVG")
        if price % tick:
            raise ExecutionBlocked("Entry violates exchange price increment")
        closed_ms = (int(time.time() * 1000) // 60_000) * 60_000
        current_adx = evaluate_adx(closed_ms, off_choch=False)
        if current_adx["status"] != "PASS":
            raise ExecutionBlocked("ADX is not verified below threshold at entry")
        candles = fetch_closed("1m", closed_ms)
        by_start = {c.start_ms: c for c in candles}
        reference = by_start.get(fvg["formation_bar_ms"] - 120_000)
        if reference is None:
            raise ExecutionBlocked("FVG reference candle unavailable")
        stop = Decimal(str(plan["stop"]))
        if (plan["direction"] == "long" and stop > Decimal(str(reference.low)) - tick
                or plan["direction"] == "short" and stop < Decimal(str(reference.high)) + tick):
            raise ExecutionBlocked("Stop must sit beyond the FVG reference candle")
        if fvg["formation_bar_ms"] > task[0]["signal_bars"]["internal_choch"]:
            raise ExecutionBlocked("FVG formed after the internal CHoCH")
        proposal = prepare_limit_order(
            setup_id, plan["direction"], price, plan["size"], stop
        )
        result = submit_order(proposal)
        order_id = accepted_id(result)
        with _connect() as conn:
            conn.execute("""
              UPDATE smog_execution_intents
              SET status='submitted', exchange_order_id=%s,
                  updated_at=now()
              WHERE id=%s AND status='submitting'
            """, (str(order_id), intent_id))
        log.info("Demo order submitted for intent %s", intent_id)
    except ExecutionBlocked as exc:
        with _connect() as conn:
            conn.execute("""
              UPDATE smog_execution_intents
              SET status='rejected', error_code='PREFLIGHT_BLOCKED',
                  updated_at=now() WHERE id=%s AND status='submitting'
            """, (intent_id,))
        log.warning("Demo intent %s blocked: %s", intent_id, type(exc).__name__)
    except Exception as exc:
        # Timeouts and ambiguous HTTP responses might have reached BloFin.
        # Reconciliation by deterministic clientOrderId is required first.
        with _connect() as conn:
            conn.execute("""
              UPDATE smog_execution_intents
              SET status='unknown', error_code='RECONCILE_REQUIRED',
                  updated_at=now() WHERE id=%s AND status='submitting'
            """, (intent_id,))
        log.error("Demo intent %s requires reconciliation: %s",
                  intent_id, type(exc).__name__)
    return True


def reconcile_orders():
    """Independent exchange order and live demo position reconciliation."""
    if not enabled():
        return
    from smog_lifecycle import reconcile_entries, manage_positions
    reconcile_entries(_connect)
    manage_positions(_connect)


def run_worker():
    """Event-driven wake with periodic recovery for notifications lost on restart."""
    import time
    last_audit = 0.0
    while True:
        try:
            with _connect() as conn:
                conn.autocommit = True
                conn.execute("LISTEN smog_demo_intents")
                while True:
                    while process_one():
                        pass
                    reconcile_orders()
                    if time.monotonic() - last_audit >= 60:
                        from smog_audit import settle_observations
                        settle_observations(_connect)
                        last_audit = time.monotonic()
                    for _ in conn.notifies(timeout=5, stop_after=1):
                        pass
        except Exception:
            log.exception("Demo worker reconnecting")
            time.sleep(5)
