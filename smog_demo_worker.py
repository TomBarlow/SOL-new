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
    ExecutionBlocked, prepare_limit_order, submit_order, demo_read
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
      SELECT created_at FROM smog_tasks WHERE candidate_key=%s
    """, (setup_id,)).fetchone()
    if not row or (datetime.now(timezone.utc) - row[0]).total_seconds() > 180:
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
        proposal = prepare_limit_order(
            setup_id, plan["direction"], plan["entry"],
            plan["size"], plan["stop"]
        )
        result = submit_order(proposal)
        data = result.get("data") or {}
        order_id = data.get("orderId")
        if not order_id:
            raise ValueError("Successful response omitted order ID")
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
    """Resolve known demo order states without guessing fills or retrying sends."""
    if not enabled():
        return
    with _connect() as conn:
        intents = conn.execute("""
          SELECT id, setup_id, status, exchange_order_id
          FROM smog_execution_intents
          WHERE status IN ('submitted', 'unknown')
          ORDER BY id LIMIT 50
        """).fetchall()
    for intent_id, setup_id, status, exchange_id in intents:
        import hashlib
        client_id = "smog" + hashlib.sha256(setup_id.encode()).hexdigest()[:28]
        params = {"instId": "SOL-USDT"}
        params["orderId" if exchange_id else "clientOrderId"] = exchange_id or client_id
        try:
            data = demo_read("order_detail", params).get("data")
            if not isinstance(data, dict) or not data.get("orderId"):
                continue
            state = data.get("state")
            new_status = "filled" if state == "filled" else (
                "cancelled" if state in ("canceled", "cancelled") else "submitted"
            )
            # One-way reconciliation, no order action.
            with _connect() as conn:
                conn.execute("""
                  UPDATE smog_execution_intents
                  SET status=%s, exchange_order_id=%s, error_code=NULL,
                      updated_at=now()
                  WHERE id=%s AND status IN ('submitted', 'unknown')
                """, (new_status, str(data["orderId"]), intent_id))
        except Exception:
            log.warning("Order detail unavailable for demo intent %s", intent_id)


def run_worker():
    """Event-driven wake with periodic recovery for notifications lost on restart."""
    import time
    while True:
        try:
            with _connect() as conn:
                conn.autocommit = True
                conn.execute("LISTEN smog_demo_intents")
                while True:
                    while process_one():
                        pass
                    reconcile_orders()
                    for _ in conn.notifies(timeout=30, stop_after=1):
                        pass
        except Exception:
            log.exception("Demo worker reconnecting")
            time.sleep(5)
