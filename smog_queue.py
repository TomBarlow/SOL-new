"""Authenticated Hermes decisions, durable demo execution queue, and outcomes."""
import hashlib
import hmac
import os
from datetime import datetime, timezone, timedelta
from typing import Literal

import psycopg
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from psycopg.types.json import Jsonb

router = APIRouter(prefix="/smog", tags=["smog"])
LEASE_SECONDS = 180


def _auth(authorization: str | None):
    token = os.environ.get("SMOG_API_TOKEN")
    if not token:
        raise HTTPException(503, "SMOG API not configured")
    supplied = (authorization or "").removeprefix("Bearer ")
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(401, "Unauthorized")


def _db():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not configured")
    return psycopg.connect(url, connect_timeout=5)


def init_queue():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS smog_tasks (
                id BIGSERIAL PRIMARY KEY,
                candidate_key TEXT UNIQUE NOT NULL,
                payload JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                lease_until TIMESTAMPTZ,
                attempts INTEGER NOT NULL DEFAULT 0,
                decision JSONB,
                decided_at TIMESTAMPTZ,
                CHECK (status IN ('pending','leased','decided','expired'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS smog_execution_intents (
                id BIGSERIAL PRIMARY KEY,
                setup_id TEXT UNIQUE NOT NULL REFERENCES smog_tasks(candidate_key),
                trade_plan JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'blocked',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                exchange_order_id TEXT,
                error_code TEXT,
                CHECK (status IN ('blocked','ready','submitting','submitted',
                                  'rejected','cancelled','filled','unknown'))
            )
        """)
        from smog_lifecycle import init_schema
        init_schema(conn)
        from smog_audit import init_audit
        init_audit(conn)


def enqueue_candidate(candidate: dict) -> bool:
    """Caller supplies orchestrator's unverified candidate; dedupe in Postgres."""
    if candidate.get("execution_allowed") is not False:
        raise ValueError("Only non-executable candidates may be queued")
    key = candidate["candidate_key"]
    with _db() as conn:
        row = conn.execute("""
            INSERT INTO smog_tasks (candidate_key, payload)
            VALUES (%s, %s) ON CONFLICT (candidate_key) DO NOTHING
            RETURNING id
        """, (key, Jsonb(candidate))).fetchone()
        if row is not None:
            # Transactional notification: delivered only if candidate insert commits.
            conn.execute("SELECT pg_notify(%s, %s)", ("smog_candidates", str(row[0])))
    return row is not None


@router.get("/monitor")
def monitor():
    """Stable, non-sensitive queue wake-up signal for Hermes --monitor-url.

    No credentials or candidate details are exposed. Response is byte-identical
    while the ready queue is unchanged. Monitoring does not claim a task.
    """
    with _db() as conn:
        row = conn.execute("""
            SELECT COUNT(*), COALESCE(MAX(id), 0)
            FROM smog_tasks
            WHERE status = 'pending'
               OR (status = 'leased' AND lease_until < now())
        """).fetchone()
    count, latest_id = row
    return Response(
        content=f"ready:{count}:latest:{latest_id}" if count else "idle",
        media_type="text/plain",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/next")
def next_task(authorization: str | None = Header(default=None)):
    _auth(authorization)
    with _db() as conn:
        # Row lock prevents concurrent workers claiming the same candidate.
        row = conn.execute("""
            SELECT id, candidate_key, payload FROM smog_tasks
            WHERE status = 'pending'
               OR (status = 'leased' AND lease_until < now())
            ORDER BY created_at, id
            FOR UPDATE SKIP LOCKED LIMIT 1
        """).fetchone()
        if not row:
            return Response(status_code=204)
        task_id, key, payload = row
        conn.execute("""
            UPDATE smog_tasks SET status='leased',
            lease_until=now() + interval '180 seconds', attempts=attempts+1
            WHERE id=%s
        """, (task_id,))
    return {"setup_id": key, "lease_seconds": LEASE_SECONDS,
            "candidate": payload, "execution_allowed": False}


class Decision(BaseModel):
    model_config = ConfigDict(extra="allow")
    setup_id: str = Field(min_length=1, max_length=128)
    decision: Literal["TAKE", "WAIT", "SKIP"]
    narrative: str = Field(min_length=1, max_length=10000)
    model: Literal["z-ai/glm-5.3"]
    trade_plan: dict | None = None
    rule_eval: dict | None = None


@router.post("/decisions")
def submit_decision(body: Decision, authorization: str | None = Header(default=None)):
    _auth(authorization)
    if body.decision == "TAKE" and not body.trade_plan:
        raise HTTPException(422, "TAKE requires a trade_plan")
    with _db() as conn:
        row = conn.execute("""
            SELECT status, decision, payload FROM smog_tasks
            WHERE candidate_key=%s FOR UPDATE
        """, (body.setup_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Unknown setup")
        status, prior, candidate = row
        if body.decision == "TAKE":
            # A same-direction OG, CHoCH and FVG within the wake-up window
            # cannot establish that the gap belongs to the CHoCH displacement.
            # Hermes must explicitly attest to the actual alert candles and
            # its candle-based structural assessment before demo execution.
            alignment = (body.rule_eval or {}).get("fvg_choch_displacement")
            signal_bars = candidate.get("signal_bars") or {}
            if (not isinstance(alignment, dict)
                    or alignment.get("status") != "PASS"
                    or alignment.get("choch_bar_ms") != signal_bars.get("internal_choch")
                    or alignment.get("fvg_bar_ms") != signal_bars.get("fvg_formed")
                    or not isinstance(alignment.get("evidence"), str)
                    or not alignment["evidence"].strip()):
                raise HTTPException(422, "TAKE requires evidenced FVG/CHoCH displacement alignment")
        incoming = body.model_dump(mode="json")
        if status == "decided":
            if prior == incoming:
                return {"status": "duplicate", "execution_allowed": False}
            raise HTTPException(409, "Conflicting decision")
        if status != "leased":
            raise HTTPException(409, "Task not leased")
        conn.execute("""
            UPDATE smog_tasks SET status='decided', decision=%s,
                decided_at=now(), lease_until=NULL
            WHERE candidate_key=%s
        """, (Jsonb(incoming), body.setup_id))
        if body.decision == "TAKE":
            # Durable, deduplicated execution intent. Never call an exchange
            # inside the decision transaction. Explicitly blocked until
            # execution validation and independent worker activation.
            conn.execute("""
                INSERT INTO smog_execution_intents(setup_id, trade_plan, status)
                VALUES (%s, %s, 'blocked')
                ON CONFLICT (setup_id) DO NOTHING
            """, (body.setup_id, Jsonb(body.trade_plan)))
            from smog_demo_worker import promote_fresh_intent
            promote_fresh_intent(conn, body.setup_id, body.trade_plan)
    return {"status": "recorded", "execution_allowed": False}


@router.get("/execution-intents")
def execution_intents(authorization: str | None = Header(default=None)):
    """Read-only operational status; never expose plans or account credentials."""
    _auth(authorization)
    from smog_demo_worker import enabled
    with _db() as conn:
        rows = conn.execute("""
            SELECT status, count(*) FROM smog_execution_intents GROUP BY status
        """).fetchall()
    return {"execution_enabled": enabled(),
            "demo_execution_enabled": enabled(),
            "intents_by_status": dict(rows)}



@router.get("/positions")
def positions(authorization: str | None = Header(default=None)):
    """Authenticated read-only BloFin demo position snapshot."""
    _auth(authorization)
    from smog_demo_execution import demo_read
    try:
        response = demo_read("positions", {"instId": "SOL-USDT"})
        rows = response.get("data") or []
        return {"source": "blofin_demo", "positions": [
            {k: item.get(k) for k in ("instId", "positionSide", "positions",
                                      "averagePrice", "markPrice", "upl",
                                      "marginMode", "leverage", "positionId")}
            for item in rows
        ]}
    except Exception:
        raise HTTPException(503, "Demo position query unavailable")

@router.get("/outcomes")
def outcomes(authorization: str | None = Header(default=None)):
    """Verified exchange fills and realized net P&L, grouped by trade."""
    _auth(authorization)
    with _db() as conn:
        conn.row_factory = psycopg.rows.dict_row
        trades = conn.execute("""
            SELECT p.setup_id, p.direction, p.initial_size, p.open_size,
                   p.entry_price, p.initial_stop, p.current_stop,
                   p.opened_at, p.closed_at, p.bos_at,
                   p.partial_filled, p.be_moved, p.pnl_net,
                   count(f.trade_id) AS exchange_fills
            FROM smog_managed_positions p
            LEFT JOIN smog_trade_fills f ON f.intent_id = p.intent_id
            GROUP BY p.intent_id
            ORDER BY p.opened_at DESC LIMIT 100
        """).fetchall()
        counts = conn.execute("""
            SELECT status, count(*) FROM smog_execution_intents
            GROUP BY status
        """).fetchall()
    return {"source": "blofin_demo_exchange_reconciled",
            "counts": dict((row["status"], row["count"]) for row in counts),
            "trades": [dict(row) for row in trades],
            "pnl_available": any(row["pnl_net"] is not None for row in trades)}


@router.get("/audits")
def audits(authorization: str | None = Header(default=None)):
    """Read-only completed 50-setup audit batches."""
    _auth(authorization)
    with _db() as conn:
        conn.row_factory = psycopg.rows.dict_row
        batches = conn.execute("""
            SELECT id, through_task_id, resolved_count, metrics, created_at
            FROM smog_audit_batches ORDER BY id DESC LIMIT 20
        """).fetchall()
    return {"source": "closed_observation_windows", "batches": [dict(row) for row in batches]}


@router.get("/learning-data")
def learning_data(authorization: str | None = Header(default=None)):
    """Structured, independently identifiable actual and shadow outcomes."""
    _auth(authorization)
    with _db() as conn:
        conn.row_factory = psycopg.rows.dict_row
        rows = conn.execute("""
            SELECT t.id, t.candidate_key, t.decision->>'decision' AS decision,
                   t.created_at, t.decided_at, i.status AS execution_status,
                   p.pnl_net, s.outcome AS shadow_outcome
            FROM smog_tasks t
            LEFT JOIN smog_execution_intents i ON i.setup_id=t.candidate_key
            LEFT JOIN smog_managed_positions p ON p.setup_id=t.candidate_key
            LEFT JOIN smog_shadow_outcomes s ON s.setup_id=t.candidate_key
            WHERE t.status IN ('decided','expired')
            ORDER BY t.id DESC LIMIT 250
        """).fetchall()
    return {"observations": [dict(row) for row in rows]}


@router.get("/stats")
def stats(authorization: str | None = Header(default=None)):
    _auth(authorization)
    from smog_demo_worker import enabled
    with _db() as conn:
        decisions = conn.execute("""
            SELECT decision->>'decision', count(*) FROM smog_tasks
            WHERE status='decided' GROUP BY 1
        """).fetchall()
        intents = conn.execute("""
            SELECT status, count(*) FROM smog_execution_intents GROUP BY 1
        """).fetchall()
        realized = conn.execute("""
            SELECT count(*), COALESCE(sum(pnl_net),0)
            FROM smog_managed_positions
            WHERE closed_at IS NOT NULL AND pnl_net IS NOT NULL
        """).fetchone()
        open_positions = conn.execute("""
            SELECT count(*) FROM smog_managed_positions
            WHERE closed_at IS NULL
        """).fetchone()[0]
        audits = conn.execute("""
            SELECT count(*) FROM smog_audit_batches
        """).fetchone()[0]
    return {"decisions": dict(decisions), "execution": dict(intents),
            "managed_open_positions": open_positions,
            "closed_positions_with_realized_pnl": realized[0],
            "realized_pnl_usdt_ex_funding": str(realized[1]),
            "completed_50_setup_audits": audits,
            "demo_execution_enabled": enabled(),
            "pnl_available": realized[0] > 0}
