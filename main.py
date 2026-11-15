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
        log.info('stored id=%s event=%s bar_time=%s', inserted[0], payload.event, bar_time_ms)
        return JSONResponse({'status': 'stored', 'id': inserted[0]}, status_code=200)
    log.info('duplicate event=%s bar_time=%s', payload.event, bar_time_ms)
    return {'status': 'duplicate'}
