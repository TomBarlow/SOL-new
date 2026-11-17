# SMOG demo audit log

## 2026-11-17 06:00 UTC

### Production observations

- Railway deployment `7433a5c8-f42b-4797-8b7d-4f14631310d0` was healthy on commit `c3efbcb3cda28df89548633e1253161acfe58ae2`.
- BloFin preflight verified the demo host, one-way net mode, cross margin, 3x leverage, zero live SOL-USDT positions, zero open orders and zero fills at startup.
- No worker exception, reconciliation failure, unprotected-position event or stale-order failure appeared in the inspected 00:00–06:12 UTC runtime window.
- Several candidates initially lacked the just-closed evaluation candle. The durable retry recovered exact FVG geometry within seconds. This is an expected data-arrival race and did not bypass fail-closed execution validation.
- No `SMOG_AUDIT_BATCH` was emitted in the window. There were no exchange-verified closed demo trades or realized P&L to report from the inspected logs.

### Reproducible defects fixed

- Entry reconciliation now advances both `initial_size` and `open_size` when a later exchange read reports a larger cumulative partial fill. This prevents management from retaining the first observed partial quantity after a cancel/fill race.
- Emergency exits and partial closes are confirmed only when exchange `filledSize` reaches the deterministic requested target. An underfilled `partially_filled` response can no longer be recorded as a completed emergency exit.

### Validation and strategy decision

- Full isolated unit suite passed after the changes.
- Risk, notional, leverage, position-mode, FVG ordering, ADX and deterministic idempotency rules were unchanged.
- No strategy parameter was changed. The available runtime evidence did not include a completed rolling 50-resolved-setup batch or sufficient actual closed-trade outcomes for an out-of-sample strategy comparison.

## 2026-11-17 12:00 UTC

### Runtime and review pipeline

- The 06:19 deployment `a957c7c3-e67a-45c7-bb27-3bd74f8343fa` on `67d800c` remained SUCCESS.
- From 06:19 to 12:02 UTC, 233 TradingView webhook requests returned HTTP 200. Runtime contained 41 prefilter diagnostics and 17 recovered candidate enqueue logs, with no worker exceptions or `SMOG_AUDIT_BATCH` in the retrieved window.
- Four authenticated `/smog/events` streams were opened, yet HTTP logs showed zero `/smog/next` claims and zero `/smog/decisions` submissions. This indicates that the external Hermes decision-consumer path is stalled or disconnected. The bridge is external to this demo execution service and requires separate diagnosis. No discretionary fallback has been introduced.
- Authenticated `/smog/stats` and `/smog/learning-data` are unavailable to this scheduled audit, so exact queue backlog, resolved setup count and historical closed-trade P&L remain unverified.

### Research-only analysis enhancements

- Added a transparent, deterministic OG-to-CHoCH closed-candle swing-range proxy. Each eligible shadow observation records FVG midpoint, swing extremes, 0.5 Fibonacci level and whether the FVG midpoint lies at or beyond the halfway retracement. The proxy is explicitly marked non-authoritative because proprietary swing anchors cannot be reconstructed from alert closes alone.
- Corrected shadow FVG-touch recognition to require a candle's price range to intersect the actual gap. A candle entirely above or below the FVG cannot count as touching it.
- The 50-resolved-setup batch now reports the number of eligible proxy observations and midpoint-beyond-half cases, without converting those observations into realised trade performance or changing strategy rules.

### Community chart intake

- Three user-supplied annotated SOL examples were examined on 17 November. The first showed a bullish CHoCH/BOS continuation and the third a bearish breakdown/retest. The second showed a 1-minute bullish reversal against bearish higher-timeframe SOL and weak BTC context, with an apparent entry beyond the displayed 0.5 retracement.
- Test hypotheses are FVG-midpoint versus displacement halfway retracement, trend alignment, and whether the existing partial-close/breakeven/FVG-trailing rules retain favourable movement. The screenshots contain selected retrospective price paths, so they provide neither verified exchange fills nor a representative outcome sample.
