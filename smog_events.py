"""Event-driven Hermes bridge feed. PostgreSQL LISTEN/NOTIFY, no scheduled polling.

A listener subscribes BEFORE replaying the durable queue to avoid missed events.
Replay deliberately includes ready tasks regardless of Last-Event-ID: that header
is advisory because an undecided task must survive a bridge restart.
"""
import hmac
import json
import os

import psycopg
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/smog", tags=["smog"])
CHANNEL = "smog_candidates"


def _auth(authorization):
    expected = os.environ.get("SMOG_API_TOKEN")
    if not expected:
        raise HTTPException(503, "SMOG API not configured")
    supplied = (authorization or "").removeprefix("Bearer ")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "Unauthorized")


def _ready(conn):
    return conn.execute("""
        SELECT id, candidate_key FROM smog_tasks
        WHERE status = 'pending'
           OR (status = 'leased' AND lease_until < now())
        ORDER BY id
    """).fetchall()


def _frame(task_id, key):
    # Never include market data or secrets in the stream.
    data = json.dumps({"setup_id": key}, separators=(",", ":"))
    return f"id: {task_id}\nevent: candidate\ndata: {data}\n\n"


def _stream(last_event_id):
    # LISTEN must be committed before querying: no race between snapshot/replay
    # and new notifications. A single DB connection is held per bridge stream.
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, connect_timeout=5) as conn:
        conn.execute(f"LISTEN {CHANNEL}")
        for task_id, key in _ready(conn):
            yield _frame(task_id, key)
        yield ": connected\n\n"
        # psycopg notifies() waits on the DB socket; timeout sends SSE keepalives.
        # NOTIFY payload is an ID only; always re-read eligibility from the DB.
        while True:
            notified_ids = [n.payload for n in conn.notifies(timeout=20, stop_after=1)]
            if notified_ids:
                for notification_id in notified_ids:
                    if not notification_id.isdecimal():
                        continue
                    row = conn.execute("""
                        SELECT id, candidate_key FROM smog_tasks
                        WHERE id=%s AND status='pending'
                    """, (int(notification_id),)).fetchone()
                    if row:
                        yield _frame(*row)
            else:
                yield ": heartbeat\n\n"


@router.get("/events")
def events(authorization: str | None = Header(default=None),
           last_event_id: str | None = Header(default=None)):
    _auth(authorization)
    # Last-Event-ID accepted for standards-compatible reconnects; replay of all
    # still-ready tasks is safer than filtering by monotonically increasing ID.
    if last_event_id is not None and (not last_event_id.isdecimal() or len(last_event_id) > 20):
        raise HTTPException(400, "Invalid Last-Event-ID")
    return StreamingResponse(_stream(last_event_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform",
                                      "X-Accel-Buffering": "no"})
