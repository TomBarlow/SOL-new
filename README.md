# SMOG TradingView webhook receiver (Stage 1)

Receive and persist the 10 TradingView alert types for BLOFIN:SOLUSDT.P, 1m. No trade execution, AI calls, or strategy decisions.

## Deploy into an existing Railway project

1. Create a new **Postgres** service in the Railway project.
2. Create a new **empty service** for this receiver. Deploy the contents of this directory via GitHub or `railway up` (after `railway link`, selecting the new service). Do not overwrite an existing application service.
3. In the receiver's **Variables**, set:
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}` (Railway variable reference, select your Postgres service)
   - `WEBHOOK_SECRET=` followed by a newly generated random token, e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`
4. Deploy and generate a public HTTPS domain from the receiver service's **Settings > Networking**.
5. Open `https://YOUR-DOMAIN/health` to confirm `{"status":"ok"}`.
6. Test with the curl command below before changing TradingView alerts.

## Test (substitute values; don't commit your secret)

```bash
curl -i -X POST "https://YOUR-DOMAIN/webhook/tradingview?token=YOUR_SECRET" \
  -H 'Content-Type: application/json' \
  --data '{"event":"og_long","source":"tradingview","symbol":"BLOFIN:SOLUSDT.P","interval":"1","bar_time":"2026-11-15T09:00:00Z","price":145.20}'
```

Expect HTTP 200 and `{"status":"stored", "id": ...}`. Repeating the same command should return `{"status":"duplicate"}`. Invalid token -> 401. Unknown event -> 422.

## TradingView alert message

Paste the following JSON into each alert's **Message** field. Change only `event` for each alert:

```json
{"event":"og_long","source":"tradingview","symbol":"BLOFIN:SOLUSDT.P","interval":"1","bar_time":"{{time}}","price":{{close}}}
```

TradingView's `{{time}}` placeholder expands to an ISO-8601 UTC bar timestamp. The receiver converts it to epoch milliseconds for deduplication. Alert time is the bar's time, not arrival time.

Set the **Webhook URL** to `https://YOUR-DOMAIN/webhook/tradingview?token=YOUR_SECRET` and retain once-per-bar-close. Do not include the secret in the JSON message.

Event names:
- OG Long: `og_long`
- OG Short: `og_short`
- Internal Bullish CHoCH: `internal_bullish_choch`
- Internal Bearish CHoCH: `internal_bearish_choch`
- Internal Bullish BOS: `internal_bullish_bos`
- Internal Bearish BOS: `internal_bearish_bos`
- Bullish FVG formed: `bullish_fvg_formed`
- Bearish FVG formed: `bearish_fvg_formed`
- Bullish FVG mitigated: `bullish_fvg_mitigated`
- Bearish FVG mitigated: `bearish_fvg_mitigated`

## Security and reliability

- Webhook URL contains a secret token: treat as a password; do not paste publicly or include it in screenshots. Railway access logs and TradingView configuration can expose URL query strings. Rotate if exposed.
- This is a starter. For production execution, use IP allowlisting (TradingView publishes source IPs), monitoring, retries/reconciliation, a durable queue, and stronger request authentication where possible.
- TradingView has a 3-second webhook response limit. Postgres must respond quickly; monitor latency and TradingView webhook status.
- TradingView may occasionally miss a delivery; this service cannot reconstruct missing events by itself.
- Do not deploy this service with BloFin live-trading keys.
