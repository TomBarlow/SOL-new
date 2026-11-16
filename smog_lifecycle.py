"""Reconcile and manage a single SOL-USDT BloFin demo position.

All exchange writes have a durable single-use action reservation. Unknown
outcomes are reconciled by client ID or exchange state before any further write.
"""
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from psycopg.types.json import Jsonb

from smog_demo_execution import (
    ExecutionBlocked, accepted_id, amend_protective_stop, cancel_order,
    client_action_id, demo_instrument, demo_read, place_emergency_stop,
    submit_partial_close,
)
from smog_fvg_geometry import inspect_fvg

log = logging.getLogger("smog.lifecycle")
INSTRUMENT = "SOL-USDT"
ENTRY_TTL_SECONDS = 180


def init_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smog_managed_positions (
            intent_id BIGINT PRIMARY KEY REFERENCES smog_execution_intents(id),
            setup_id TEXT UNIQUE NOT NULL,
            direction TEXT NOT NULL,
            initial_size NUMERIC NOT NULL,
            open_size NUMERIC NOT NULL,
            entry_price NUMERIC NOT NULL,
            initial_stop NUMERIC NOT NULL,
            current_stop NUMERIC NOT NULL,
            position_id TEXT,
            opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            bos_at TIMESTAMPTZ,
            bos_bar_ms BIGINT,
            partial_order_id TEXT,
            partial_filled BOOLEAN NOT NULL DEFAULT false,
            be_moved BOOLEAN NOT NULL DEFAULT false,
            trailing_gap_ms BIGINT,
            closed_at TIMESTAMPTZ,
            pnl_net NUMERIC
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smog_management_actions (
            id BIGSERIAL PRIMARY KEY,
            intent_id BIGINT NOT NULL REFERENCES smog_execution_intents(id),
            action_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sending',
            target TEXT,
            exchange_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(intent_id, action_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smog_trade_fills (
            trade_id TEXT PRIMARY KEY,
            intent_id BIGINT NOT NULL REFERENCES smog_execution_intents(id),
            order_id TEXT,
            fill_price NUMERIC,
            fill_size NUMERIC,
            fill_pnl NUMERIC NOT NULL,
            fee NUMERIC NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL
        )
    """)


def _decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Nonfinite exchange value")
    return result


def _rows(endpoint, params=None):
    response = demo_read(endpoint, params or {"instId": INSTRUMENT})
    data = response.get("data")
    if not isinstance(data, list):
        raise ValueError("Unexpected BloFin data shape")
    return data


def _position():
    active = [p for p in _rows("positions")
              if p.get("instId") == INSTRUMENT
              and _decimal(p.get("positions", "0")) != 0]
    if len(active) > 1:
        raise ExecutionBlocked("Multiple active SOL positions: manual investigation required")
    return active[0] if active else None


def _action(conn, intent_id, key, target=None):
    """Reserve once. Never automatically replay a transmitted action."""
    return conn.execute("""
        INSERT INTO smog_management_actions(intent_id, action_key, target)
        VALUES (%s, %s, %s) ON CONFLICT (intent_id, action_key) DO NOTHING
        RETURNING id
    """, (intent_id, key, str(target) if target is not None else None)).fetchone()


def _finish_action(conn, action_id, status, exchange_id=None):
    conn.execute("""
        UPDATE smog_management_actions
        SET status=%s, exchange_id=%s, updated_at=now() WHERE id=%s
    """, (status, exchange_id, action_id))


def _transmit(conn_factory, intent_id, key, send, field="orderId", target=None):
    with conn_factory() as conn:
        claim = _action(conn, intent_id, key, target)
    if claim is None:
        return False
    action_id = claim[0]
    try:
        result = send()
        exchange_id = accepted_id(result, field)
    except Exception:
        with conn_factory() as conn:
            _finish_action(conn, action_id, "unknown")
        log.exception("Demo action %s on intent %s requires reconciliation", key, intent_id)
        return False
    with conn_factory() as conn:
        _finish_action(conn, action_id, "accepted", exchange_id)
    return True


def _order_detail(order_id=None, client_id=None):
    params = {"instId": INSTRUMENT}
    if order_id:
        params["orderId"] = order_id
    else:
        params["clientOrderId"] = client_id
    detail = demo_read("order_detail", params).get("data")
    if isinstance(detail, list) and len(detail) == 1:
        detail = detail[0]
    if not isinstance(detail, dict) or not detail.get("orderId"):
        return None
    return detail


def _current_closed_bar():
    return (int(time.time() * 1000) // 60_000) * 60_000


def _cancel_pending(conn_factory, intent_id, order_id):
    return _transmit(
        conn_factory, intent_id, "entry_cancel",
        lambda: cancel_order(str(order_id)),
    )


def reconcile_entries(conn_factory):
    """Reconcile uncertain submissions and cancel stale or partly filled limits."""
    with conn_factory() as conn:
        intents = conn.execute("""
            SELECT id, setup_id, trade_plan, status, exchange_order_id, created_at
            FROM smog_execution_intents
            WHERE status IN ('submitted','unknown','cancel_pending')
            ORDER BY id LIMIT 50
        """).fetchall()
    for intent_id, setup_id, plan, status, exchange_id, created_at in intents:
        try:
            client_id = client_action_id(setup_id, "entry")
            # Existing entry client IDs predate the lifecycle engine.
            import hashlib
            client_id = "smog" + hashlib.sha256(setup_id.encode()).hexdigest()[:28]
            detail = _order_detail(exchange_id, client_id)
            if detail is None:
                continue
            order_id = str(detail["orderId"])
            state = detail.get("state")
            filled = _decimal(detail.get("filledSize") or "0")
            order_size = _decimal(detail.get("size") or plan["size"])
            if filled < 0 or filled > order_size:
                raise ValueError("Invalid filled quantity")
            if filled > 0:
                price = _decimal(detail.get("averagePrice") or plan["entry"])
                if price <= 0:
                    raise ValueError("Missing filled entry price")
                with conn_factory() as conn:
                    conn.execute("""
                        INSERT INTO smog_managed_positions
                            (intent_id, setup_id, direction, initial_size,
                             open_size, entry_price, initial_stop, current_stop,
                             position_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (intent_id) DO UPDATE
                        SET initial_size = GREATEST(
                            smog_managed_positions.initial_size, EXCLUDED.initial_size)
                    """, (intent_id, setup_id, plan["direction"],
                          filled, filled, price, plan["stop"], plan["stop"],
                          detail.get("positionId")))
            expiry = (datetime.now(timezone.utc) - created_at).total_seconds() >= ENTRY_TTL_SECONDS
            pending = state in ("live", "partially_filled", "partially-filled")
            if pending and (expiry or filled > 0):
                _cancel_pending(conn_factory, intent_id, order_id)
                new_status = "cancel_pending"
            elif state == "filled" or (state in ("canceled","cancelled") and filled > 0):
                new_status = "filled"
            elif state in ("canceled", "cancelled", "expired"):
                new_status = "cancelled"
            else:
                new_status = "submitted"
            with conn_factory() as conn:
                conn.execute("""
                    UPDATE smog_execution_intents
                    SET status=%s, exchange_order_id=%s, error_code=NULL,
                        updated_at=now()
                    WHERE id=%s AND status IN ('submitted','unknown','cancel_pending')
                """, (new_status, order_id, intent_id))
        except Exception:
            log.exception("Entry reconciliation failed for intent %s", intent_id)


def _reconcile_actions(conn_factory, pos):
    intent_id, setup_id = pos["intent_id"], pos["setup_id"]
    with conn_factory() as conn:
        actions = conn.execute("""
            SELECT id, action_key, status, target, exchange_id
            FROM smog_management_actions
            WHERE intent_id=%s AND status IN ('sending','unknown','accepted')
        """, (intent_id,)).fetchall()
    for action_id, key, state, target, order_id in actions:
        try:
            if key == "partial_close":
                detail = _order_detail(order_id, client_action_id(setup_id, key))
                if detail and _decimal(detail.get("filledSize") or "0") >= _decimal(target):
                    with conn_factory() as conn:
                        _finish_action(conn, action_id, "confirmed", str(detail["orderId"]))
                        conn.execute("""
                            UPDATE smog_managed_positions
                            SET partial_order_id=%s, partial_filled=true WHERE intent_id=%s
                        """, (str(detail["orderId"]), intent_id))
            elif key.startswith("stop_") or key == "emergency_stop":
                current_tpsl = _rows("tpsl")
                if key == "emergency_stop":
                    matching = [row for row in current_tpsl
                                if row.get("clientOrderId") == client_action_id(setup_id,key)]
                else:
                    matching = [row for row in current_tpsl
                                if row.get("tpslId") == order_id
                                and _decimal(row.get("slTriggerPrice") or "0") == _decimal(target)]
                if matching:
                    with conn_factory() as conn:
                        _finish_action(conn, action_id, "confirmed",
                                       str(matching[0]["tpslId"]))
                        if key.startswith("stop_"):
                            conn.execute("""
                                UPDATE smog_managed_positions
                                SET current_stop=%s, be_moved=CASE
                                    WHEN %s='stop_breakeven' THEN true ELSE be_moved END
                                WHERE intent_id=%s
                            """, (target, key, intent_id))
            elif key == "entry_cancel":
                detail = _order_detail(order_id)
                if detail and detail.get("state") in ("filled","canceled","cancelled","expired"):
                    with conn_factory() as conn:
                        _finish_action(conn, action_id, "confirmed", order_id)
        except Exception:
            log.exception("Action reconciliation failed: intent %s action %s", intent_id, key)


def _safe_tpsl(direction, position_size, current_stop):
    """Find the sole matching live stop with sufficient coverage."""
    opposite = "sell" if direction == "long" else "buy"
    matches = [row for row in _rows("tpsl")
               if row.get("side") == opposite
               and row.get("positionSide") == "net"
               and _decimal(row.get("slTriggerPrice") or "0") > 0
               and (_decimal(row.get("size") or "0") >= position_size
                    or str(row.get("size")) == "-1")]
    if len(matches) != 1:
        return None
    if direction == "long" and _decimal(matches[0]["slTriggerPrice"]) < current_stop:
        return None
    if direction == "short" and _decimal(matches[0]["slTriggerPrice"]) > current_stop:
        return None
    return matches[0]


def _bos_after_entry(conn_factory, pos):
    event = "internal_bullish_bos" if pos["direction"] == "long" else "internal_bearish_bos"
    with conn_factory() as conn:
        return conn.execute("""
            SELECT bar_time, received_at FROM tv_events
            WHERE event=%s AND received_at >= %s
            ORDER BY received_at LIMIT 1
        """, (event, pos["opened_at"])).fetchone()


def _price_at_four_r(pos, market_price):
    entry = pos["entry_price"]
    risk = abs(entry - pos["initial_stop"])
    if risk <= 0:
        return False
    distance = (market_price - entry) if pos["direction"] == "long" else (entry - market_price)
    return distance >= 4 * risk


def _new_trailing_fvg(conn_factory, pos, tick):
    if pos["bos_bar_ms"] is None:
        return None
    direction = pos["direction"]
    name = "bullish_fvg_formed" if direction == "long" else "bearish_fvg_formed"
    with conn_factory() as conn:
        rows = conn.execute("""
            SELECT bar_time FROM tv_events
            WHERE event=%s AND bar_time > %s
            ORDER BY bar_time DESC LIMIT 10
        """, (name, pos["bos_bar_ms"])).fetchall()
    now = _current_closed_bar()
    for (formed,) in rows:
        if formed >= now or formed == pos["trailing_gap_ms"]:
            continue
        fvg = inspect_fvg(formed, now - 60_000, direction)
        if fvg.get("status") != "VERIFIED" or fvg.get("mitigated") or fvg.get("touched"):
            continue
        # Far boundary of a newly confirmed directional gap with 1 tick padding.
        stop = _decimal(fvg["lower"] if direction == "long" else fvg["upper"])
        stop = stop - tick if direction == "long" else stop + tick
        improves = (stop > pos["current_stop"]) if direction == "long" else (
            stop < pos["current_stop"])
        if improves:
            return formed, stop
    return None


def _load_position(conn_factory, intent_id):
    with conn_factory() as conn:
        conn.row_factory = __import__("psycopg").rows.dict_row
        return conn.execute("""
            SELECT * FROM smog_managed_positions WHERE intent_id=%s
        """, (intent_id,)).fetchone()


def _book_fills(conn_factory, pos, closed=False):
    """Record exchange executions. P&L is reported only after a verified close."""
    try:
        rows = _rows("fills", {"instId": INSTRUMENT, "limit": "100"})
    except Exception:
        return
    opened_ms = int(pos["opened_at"].timestamp() * 1000) - 60_000
    until_ms = int((pos["closed_at"] or datetime.now(timezone.utc)).timestamp() * 1000)
    for row in rows:
        try:
            occurred = int(row.get("ts") or row.get("fillTime") or "0")
            trade_id = str(row.get("tradeId") or "")
            if not trade_id or not opened_ms <= occurred <= until_ms:
                continue
            with conn_factory() as conn:
                conn.execute("""
                    INSERT INTO smog_trade_fills
                    (trade_id, intent_id, order_id, fill_price, fill_size,
                     fill_pnl, fee, occurred_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s))
                    ON CONFLICT (trade_id) DO NOTHING
                """, (trade_id, pos["intent_id"], row.get("orderId"),
                      _decimal(row.get("fillPrice") or "0"),
                      _decimal(row.get("fillSize") or "0"),
                      _decimal(row.get("fillPnl") or "0"),
                      _decimal(row.get("fee") or "0"), occurred / 1000))
        except Exception:
            log.exception("Fill import failed for intent %s", pos["intent_id"])
    if closed:
        with conn_factory() as conn:
            result = conn.execute("""
                SELECT COUNT(*), SUM(fill_pnl + fee)
                FROM smog_trade_fills WHERE intent_id=%s
            """, (pos["intent_id"],)).fetchone()
            if result[0] >= 2:
                conn.execute("""
                    UPDATE smog_managed_positions SET pnl_net=%s
                    WHERE intent_id=%s AND closed_at IS NOT NULL
                """, (result[1], pos["intent_id"]))


def manage_positions(conn_factory):
    with conn_factory() as conn:
        ids = [row[0] for row in conn.execute("""
            SELECT intent_id FROM smog_managed_positions WHERE closed_at IS NULL
            ORDER BY opened_at LIMIT 20
        """).fetchall()]
    for intent_id in ids:
        try:
            pos = _load_position(conn_factory, intent_id)
            live = _position()
            if live is None:
                with conn_factory() as conn:
                    conn.execute("""
                        UPDATE smog_managed_positions SET closed_at=now(), open_size=0
                        WHERE intent_id=%s AND closed_at IS NULL
                    """, (intent_id,))
                _book_fills(conn_factory, _load_position(conn_factory, intent_id), closed=True)
                continue
            direction = "long" if _decimal(live["positions"]) > 0 else "short"
            if direction != pos["direction"]:
                raise ExecutionBlocked("Live position direction does not match managed trade")
            size = abs(_decimal(live["positions"]))
            price = _decimal(live.get("markPrice") or "0")
            if price <= 0:
                raise ValueError("Missing mark price")
            _reconcile_actions(conn_factory, pos)
            pos = _load_position(conn_factory, intent_id)
            with conn_factory() as conn:
                conn.execute("""
                    UPDATE smog_managed_positions SET open_size=%s,
                        position_id=COALESCE(NULLIF(%s,''), position_id)
                    WHERE intent_id=%s
                """, (size, live.get("positionId"), intent_id))
            stop = _safe_tpsl(direction, size, pos["current_stop"])
            if stop is None:
                _transmit(conn_factory, intent_id, "emergency_stop",
                          lambda: place_emergency_stop(
                              pos["setup_id"], direction, size, pos["current_stop"]),
                          field="tpslId", target=pos["current_stop"])
                log.error("Live demo position %s requires verified protective stop", intent_id)
                continue
            if not pos["bos_at"]:
                bos = _bos_after_entry(conn_factory, pos)
                if bos:
                    with conn_factory() as conn:
                        conn.execute("""
                            UPDATE smog_managed_positions
                            SET bos_at=%s, bos_bar_ms=%s
                            WHERE intent_id=%s AND bos_at IS NULL
                        """, (bos[1], bos[0], intent_id))
                    pos = _load_position(conn_factory, intent_id)
            four_r = _price_at_four_r(pos, price)
            if (pos["bos_at"] is not None or four_r) and not pos["partial_filled"]:
                lot = _decimal(demo_instrument()["lotSize"])
                half = ((pos["initial_size"] / 2) / lot).to_integral_value(
                    rounding=ROUND_DOWN) * lot
                if half > 0 and half < size:
                    _transmit(conn_factory, intent_id, "partial_close",
                              lambda: submit_partial_close(pos["setup_id"], direction, half),
                              target=half)
                    continue
                log.error("Partial exit impossible at demo contract lot size, intent=%s", intent_id)
                continue
            if pos["partial_filled"] and not pos["be_moved"]:
                entry = pos["entry_price"]
                tick = _decimal(demo_instrument()["tickSize"])
                entry = (entry / tick).to_integral_value(rounding=ROUND_DOWN) * tick
                if direction == "short" and entry < pos["entry_price"]:
                    entry += tick
                better = entry > pos["current_stop"] if direction == "long" else entry < pos["current_stop"]
                if better:
                    _transmit(conn_factory, intent_id, "stop_breakeven",
                              lambda: amend_protective_stop(
                                  str(stop["tpslId"]), entry,
                                  client_action_id(pos["setup_id"], "stop_breakeven")),
                              field="tpslId", target=entry)
                    continue
            if pos["be_moved"] and pos["bos_bar_ms"] and four_r:
                tick = _decimal(demo_instrument()["tickSize"])
                candidate = _new_trailing_fvg(conn_factory, pos, tick)
                if candidate:
                    formed, target = candidate
                    action_key = "stop_trail_" + str(formed)
                    sent = _transmit(conn_factory, intent_id, action_key,
                                     lambda: amend_protective_stop(
                                         str(stop["tpslId"]), target,
                                         client_action_id(pos["setup_id"], action_key)),
                                     field="tpslId", target=target)
                    if sent:
                        with conn_factory() as conn:
                            conn.execute("""
                                UPDATE smog_managed_positions
                                SET trailing_gap_ms=%s WHERE intent_id=%s
                            """, (formed, intent_id))
            _book_fills(conn_factory, pos)
        except Exception:
            log.exception("Demo position management failed for intent %s", intent_id)
