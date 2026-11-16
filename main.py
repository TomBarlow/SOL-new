import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Literal

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field
from smog_queue import router as smog_router, init_queue, enqueue_candidate
from smog_events import router as smog_events_router
from smog_orchestrator import Event, detect_candidates
from smog_adx_filter import evaluate_adx
from smog_fvg_geometry import inspect_fvg
from smog_blofin_auth import check_demo_auth

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('smog')

EVENTS = {
    'og_long', 'og_short',
    'internal_bullish_choch', 'internal_bearish_choch',
    'internal_bullish_bos', 'internal_bearish_bos',
    'bullish_fvg_formed', 'bearish_fvg_formed',
    'bullish_fvg_mitigated', 'bearish_fvg_mitigated',
}
app = FastAPI(title='SMOG TradingView Webhook Receiver', docs_url=None, redoc_url=None)
app.include_router(smog_router)
app.include_router(smog_events_router)

class Alert(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event: str
    symbol: Literal['BLOFIN:SOLUSDT.P']
    interval: Literal['1']
    bar_time: datetime
    price: float = Field(gt=0)
    source: Literal['tradingview']


def db_url():
    value = os.environ.get('DATABASE_URL', '')
    if not value:
        raise RuntimeError('DATABASE_URL not configured')
    return value


def initialize():
    with psycopg.connect(db_url(), connect_timeout=5) as conn:
        conn.execute('''
          CREATE TABLE IF NOT EXISTS tv_events (
            id BIGSERIAL PRIMARY KEY,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            event TEXT NOT NULL,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            bar_time BIGINT NOT NULL,
            price DOUBLE PRECISION NOT NULL,
            payload JSONB NOT NULL,
            event_key TEXT NOT NULL UNIQUE
          )
        ''')

@app.on_event('startup')
def startup():
    if not os.environ.get('WEBHOOK_SECRET'):
        raise RuntimeError('WEBHOOK_SECRET not configured')
    initialize()
    init_queue()
    # One read-only demo authentication probe at startup. Never log secrets or balances.
    import threading
    def demo_preflight():
        log.info('blofin_demo_auth=%s', check_demo_auth())
        from smog_demo_execution import demo_read, demo_instrument
        for endpoint in ('positions', 'orders', 'fills', 'position_mode', 'margin_mode', 'leverage', 'tpsl'):
            try:
                result = demo_read(endpoint, {'instId': 'SOL-USDT'} if endpoint in ('positions', 'orders', 'fills', 'tpsl') else {'instId': 'SOL-USDT', 'marginMode': 'cross'} if endpoint == 'leverage' else None)
                data = result.get('data')
                if endpoint == 'leverage' and isinstance(data, list) and len(data) == 1:
                    safe = {'status': 'OK', 'leverage': data[0].get('leverage'),
                            'position_side': data[0].get('positionSide')}
                elif isinstance(data, list):
                    safe = {'status': 'OK', 'count': len(data)}
                elif isinstance(data, dict):
                    safe = {'status': 'OK', 'mode': data.get('positionMode') or data.get('marginMode'),
                            'multi_position': data.get('multiPosition')}
                else:
                    safe = {'status': 'UNKNOWN'}
                log.info('blofin_demo_preflight_%s=%s', endpoint, safe)
            except Exception as exc:
                log.warning('blofin_demo_preflight_%s=ERROR:%s', endpoint, type(exc).__name__)
        try:
            inst = demo_instrument()
            safe = {k: inst.get(k) for k in ('instId', 'contractValue', 'minSize', 'lotSize', 'tickSize', 'state', 'apiPermission')}
            log.info('blofin_demo_preflight_instrument=%s', safe)
        except Exception as exc:
            log.warning('blofin_demo_preflight_instrument=ERROR:%s', type(exc).__name__)
    threading.Thread(target=demo_preflight, daemon=True).start()
    from smog_demo_worker import run_worker
    threading.Thread(target=run_worker, name="smog-demo-worker", daemon=True).start()
    with psycopg.connect(db_url(), connect_timeout=5) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS smog_adx_audit (
                candidate_key TEXT PRIMARY KEY,
                candidate JSONB NOT NULL,
                assessment JSONB NOT NULL,
                assessed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    # A closed TradingView bar can precede BloFin's confirmed OHLC response.
    # Recover recently unverified candidates without blocking webhook replies.
    from smog_prefilter_retry import run_prefilter_retries
    threading.Thread(target=run_prefilter_retries,
                     name="smog-prefilter-retry", daemon=True).start()

@app.get('/health')
def health():
    return {'status': 'ok'}

@app.post('/webhook/tradingview')
async def tradingview(request: Request):
    secret = os.environ.get('WEBHOOK_SECRET', '')
    supplied = request.query_params.get('token', '')
    if not secret or not hmac.compare_digest(secret, supplied):
        raise HTTPException(status_code=401, detail='Unauthorized')

    raw = await request.body()
    if len(raw) > 8192:
        raise HTTPException(status_code=413, detail='Payload too large')
    try:
        payload = Alert.model_validate_json(raw)
    except Exception:
        raise HTTPException(status_code=422, detail='Invalid alert payload')
    if payload.bar_time.tzinfo is None:
        raise HTTPException(status_code=422, detail='bar_time must include timezone')
    if payload.event not in EVENTS:
        raise HTTPException(status_code=422, detail='Unknown event')

    # One event of each type per closed candle. Do not use alert arrival time
    # as an identifier: TradingView can retry or deliver duplicates.
    bar_time_ms = int(payload.bar_time.timestamp() * 1000)
    unique = f'{payload.source}|{payload.symbol}|{payload.interval}|{payload.event}|{bar_time_ms}'
    key = hashlib.sha256(unique.encode()).hexdigest()
    try:
        with psycopg.connect(db_url(), connect_timeout=2) as conn:
            result = conn.execute('''
                INSERT INTO tv_events (event, symbol, interval, bar_time, price, payload, event_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_key) DO NOTHING RETURNING id
            ''', (payload.event, payload.symbol, payload.interval,
                  bar_time_ms, payload.price, Jsonb(payload.model_dump(mode="json")), key))
            inserted = result.fetchone()
    except psycopg.Error:
        log.exception('Failed to persist TradingView event')
        raise HTTPException(status_code=503, detail='Database unavailable')
    if inserted:
        # Replay recent persisted alerts. Unique candidate keys make retries safe.
        # Failure to queue does not undo the successfully stored source alert.
        try:
            with psycopg.connect(db_url(), connect_timeout=5) as conn:
                rows = conn.execute('''
                    SELECT id, event, bar_time, price FROM tv_events
                    WHERE bar_time BETWEEN %s AND %s
                    ORDER BY id
                ''', (bar_time_ms - 30 * 60_000, bar_time_ms + 30 * 60_000)).fetchall()
            event_times = {row[0]: row[2] for row in rows}
            events = [Event(*row) for row in rows if row[1] in {
                'og_long', 'og_short', 'internal_bullish_choch',
                'internal_bearish_choch', 'bullish_fvg_formed',
                'bearish_fvg_formed', 'bullish_fvg_mitigated',
                'bearish_fvg_mitigated'}]
            for candidate in detect_candidates(events):
                if candidate.triggered_by_id == inserted[0]:
                    geometry = inspect_fvg(
                        event_times[candidate.fvg_id],
                        candidate.asof_bar_time_ms, candidate.direction
                    )
                    result = evaluate_adx(candidate.asof_bar_time_ms)
                    result["fvg_geometry"] = geometry
                    with psycopg.connect(db_url(), connect_timeout=5) as audit_conn:
                        audit_conn.execute("""
                            INSERT INTO smog_adx_audit(candidate_key, candidate, assessment)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (candidate_key) DO NOTHING
                        """, (candidate.key, Jsonb(candidate.to_payload()), Jsonb(result)))
                    if (result["status"] == "PASS"
                            and geometry["status"] == "VERIFIED"
                            and not geometry["mitigated"]):
                        enriched = candidate.to_payload()
                        enriched["adx_prefilter"] = result
                        enriched["fvg"] = geometry
                        enqueue_candidate(enriched)
                    else:
                        log.info("SMOG prefilter candidate=%s adx=%s fvg=%s reason=%s",
                                 candidate.key, result["status"],
                                 geometry["status"] + ("_MITIGATED" if geometry.get("mitigated") else ""),
                                 geometry.get("reason", "none"))
        except Exception:
            log.exception('SMOG queue processing failed; source alert remains stored')
        log.info('stored id=%s event=%s bar_time=%s', inserted[0], payload.event, bar_time_ms)
        return JSONResponse({'status': 'stored', 'id': inserted[0]}, status_code=200)
    log.info('duplicate event=%s bar_time=%s', payload.event, bar_time_ms)
    return {'status': 'duplicate'}

