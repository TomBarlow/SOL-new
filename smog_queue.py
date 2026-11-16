"""SMOG Hermes task queue: independent, read-only/dry-run integration router.

Mount with app.include_router(router) only after testing. No exchange execution.
SMOG_API_TOKEN is a separate secret from TradingView WEBHOOK_SECRET.
"""
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


@router.post("/decisions")
def submit_decision(body: Decision, authorization: str | None = Header(default=None)):
    _auth(authorization)
    if body.decision == "TAKE" and not body.trade_plan:
        raise HTTPException(422, "TAKE requires a trade_plan")
    with _db() as conn:
        row = conn.execute("""
            SELECT status, decision FROM smog_tasks
            WHERE candidate_key=%s FOR UPDATE
        """, (body.setup_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Unknown setup")
        status, prior = row
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
    return {"status": "recorded", "execution_allowed": False}
