"""Recover SMOG candidates when closed exchange candles arrive after alerts.

The first assessment is persisted in smog_adx_audit by the webhook. This
worker retries only recent, inconclusive assessments. Postgres is the durable
source of pending work, so a Railway restart cannot strand a fresh candidate.
"""
import logging
import os
import time

import psycopg
from psycopg.types.json import Jsonb

from smog_adx_filter import evaluate_adx
from smog_fvg_geometry import inspect_fvg
from smog_queue import enqueue_candidate

log = logging.getLogger("smog.prefilter.retry")
RETRY_WINDOW_SECONDS = 120
RETRY_INTERVAL_SECONDS = 8
ONE_MINUTE_MS = 60_000


def check_pending():
    """Retry unresolved evidence, keeping the original candidate identity."""
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as conn:
        rows = conn.execute("""
            SELECT a.candidate_key, a.candidate, a.assessment, e.bar_time
            FROM smog_adx_audit AS a
            JOIN tv_events AS e
              ON e.id = (a.candidate->'event_ids'->>'fvg_formed')::bigint
            WHERE a.assessed_at > now() - (%s * interval '1 second')
              AND (a.assessment->>'status' = 'UNVERIFIED'
                OR a.assessment->'fvg_geometry'->>'status' = 'UNVERIFIED')
            ORDER BY a.assessed_at
            LIMIT 30
        """, (RETRY_WINDOW_SECONDS,)).fetchall()

    # Work outside the read transaction because each assessment can require
    # two public candle requests, with a bounded five-second timeout each.
    for key, candidate, previous, formed_bar_ms in rows:
        if previous["status"] == "REJECT":
            continue
        previous_fvg = previous.get("fvg_geometry") or {}
        if previous_fvg.get("mitigated") is True:
            continue

        asof_ms = candidate["asof_bar_time_ms"]
        latest_closed_ms = ((int(time.time() * 1000) // ONE_MINUTE_MS)
                            - 1) * ONE_MINUTE_MS
        geometry = inspect_fvg(formed_bar_ms,
                               max(asof_ms, latest_closed_ms),
                               candidate["direction"])
        adx = (evaluate_adx(asof_ms)
               if previous["status"] == "UNVERIFIED" else previous)
        assessment = {**adx, "fvg_geometry": geometry,
                      "recovery": "retried_after_candle_close"}

        if (adx["status"] == "PASS" and geometry["status"] == "VERIFIED"
                and geometry["mitigated"] is False):
            enriched = {**candidate, "adx_prefilter": adx, "fvg": geometry}
            # Queue before marking the audit resolved. A crash after enqueue
            # is safe because candidate_key is unique in smog_tasks.
            enqueue_candidate(enriched)
            log.info("Recovered SMOG candidate=%s FVG=%s", key,
                     geometry["identity"][:12])
        if (adx["status"] != "UNVERIFIED"
                and geometry["status"] != "UNVERIFIED"):
            log.info("Resolved SMOG prefilter candidate=%s adx=%s mitigated=%s",
                     key, adx["status"], geometry.get("mitigated"))

        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as conn:
            conn.execute("""
                UPDATE smog_adx_audit SET assessment=%s
                WHERE candidate_key=%s
            """, (Jsonb(assessment), key))


def run_prefilter_retries():
    """Bounded, inexpensive database recovery loop. No agent calls while idle."""
    while True:
        try:
            check_pending()
        except Exception:
            log.exception("SMOG prefilter retry failed; will try again")
        time.sleep(RETRY_INTERVAL_SECONDS)
