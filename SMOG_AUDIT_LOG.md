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

## 2026-11-17 community examples, later intake

### OG-to-CHoCH timing example (user screenshot IMG_1022)

- User identified an approximately 40-minute interval between OG alerts and a potentially earlier reversal entry while 5-minute ADX was declining.
- Railway signals for the pictured sequence recorded `og_long` around 13:55 UTC, bullish FVG formation around 13:56–13:58, and `internal_bullish_choch` at approximately 14:12. The 17-minute lag is observable. At 14:12 the ADX prefilter passed and FVG geometry was recovered, but no downstream Hermes task claim or trade was verified in the inspected 13:25–14:25 HTTP logs.
- Research hypothesis: compare an OG-led, pre-CHoCH early-reversal observation with the existing confirmed CHoCH/FVG sequence, using only closed data available at each proposed decision time. Keep early signals in shadow research until independently tested. Falling ADX alone does not establish a reversal or justify demo execution.

### Two stop-triggered external exits (shemil, originally posted 12 November 2026)

- The user initially described two stopped-out trades in screenshot `IMG_1023`. Follow-up screenshot `IMG_1024` supplies the trader's own message dated 12 November 2026, 18:08: the **second entry's stop-loss was hit at breakeven**, followed by price moving toward the target. Correct the earlier classification of two losses. First trade: reported stopped out, its monetary outcome remains unverified. Second trade: **reported gross breakeven stop**, with fees and any partial realised profit unverified. These are external reports, not exchange-reconciled SMOG trades.
- The accompanying chart contains two long-position drawings, FVG/demand regions and a rising support line. Actual fills, stop amendments, partial exits and the stop trigger timing cannot be established from the drawing.
- Important comparison with SMOG lifecycle at the time of this intake: after a new BOS or reaching 4R, the demo manager attempts a half-position partial exit, then moves the remaining stop to breakeven once that partial is confirmed. Following a BOS, this may move the runner stop relatively early. The trader's anecdote suggests testing whether a prompt breakeven move reduces participation in subsequent continuation, while recording any profits already locked through partial exits.
- Research analyses should distinguish initial-stop losses, gross breakeven exits, profitable partial plus breakeven runner, and exits after FVG trailing. Compare actual realised R with post-exit MFE/MAE and structural invalidation, using only evidence available when the stop was moved. Evaluate variations in shadow or isolated historical tests before changing the live demo policy.
- No outcome from this external screenshot enters SMOG demo fill counts, realised P&L or win-rate statistics. No entry or stop-management rule was changed from the anecdote.

## 2026-11-17 user-directed BOS and 4R management change

- User selected distinct management triggers for the demo-only strategy. A confirmed post-entry internal BOS requests a protective stop amendment to the entry-price breakeven level, with no partial close at BOS. A favourable move reaching 4R requests a 50% partial exit, regardless of whether BOS has already appeared.
- The BOS-triggered amendment is deferred until the current mark is on the valid side of the proposed entry-price stop. The existing protective stop remains active while waiting. Exchange confirmation and durable `stop_breakeven` / `partial_close` action IDs retain existing crash-safe reconciliation and idempotency.
- If BOS and 4R occur together, the breakeven amendment is requested first, followed by the 4R partial on a subsequent management pass. Existing partially filled positions are reconciled under their prior exchange state.
- The change was explicitly requested as demo management policy. The two external community trade illustrations remain hypotheses about premature breakeven stops, not validated optimisation evidence. A breakeven trigger price is gross of execution costs.
- Tested locally with 55 isolated unit tests, including BOS-only, 4R-only, simultaneous triggers, both directions, wrong-side-of-market protection and pre-existing partial exits. No synthetic orders or real exchange writes were used in the tests.
- The one SOL-USDT net cross position, 3x maximum leverage, $100 maximum notional, $1 maximum initial stop risk, demo host and deterministic exchange idempotency are preserved.

## 2026-11-17 17:25 UTC — Hermes consumer disconnect investigation

- From 12:00–17:20 UTC, the active Railway receiver logged 211 successful TradingView webhook deliveries and seven authenticated `/smog/events` SSE connections. HTTP telemetry showed zero `GET /smog/next` task claims and zero `POST /smog/decisions` submissions. Webhook ingestion and FVG recovery were active.
- At 16:50 UTC the next `SMOG_AUDIT_BATCH` (through task ID 100) reported 50 UNDECIDED out of 50 resolved observations, with zero exchange-verified closed trades or realised P&L. These are operational missed decisions, not trading losses.
- The accessible Railway project contains the SMOG receiver, CI, Postgres and a separate SOL service. No Hermes consumer service or Hermes credentials are present on the SMOG receiver. Restoring the external Hermes gateway/bridge remains necessary before discretionary TAKE/WAIT/SKIP decisions can resume. Do not fabricate decisions for expired candidates.
- Added authenticated read-only `/smog/consumer-health` reporting pending/leased, unclaimed older than two minutes, recent expiries without a claim, expired claims without decision, and recent decisions. Existing exchange endpoints and strategy gates remain unchanged.
- Every expiry batch now emits one `SMOG_HERMES_QUEUE_STALL` warning with a breakdown of unclaimed and leased-but-unanswered tasks. This makes missing decisions visible promptly in Railway logs, ahead of the 50-settlement audit.
- Passed 59 isolated tests, including four new consumer-health and expiry-warning checks. No demo or live orders were submitted for testing.


## 2026-11-17 18:10 UTC — post-deployment runtime audit

- Deployment `40de606d-4ba4-499b-bce6-fea8c57d5cb9` on commit `5cda416` remained SUCCESS. Railway CI ran 59 tests successfully. Runtime preflight confirmed the BloFin demo host, zero SOL-USDT positions, zero open orders, zero fills, zero TP/SL records, net/one-way position mode, cross margin and 3x leverage.
- Between deployment at 17:27 UTC and 18:07 UTC, 21 TradingView/event-stream HTTP requests completed successfully. No worker exception, reconciliation error, stale order, unprotected position or exchange ambiguity appeared.
- The new stall diagnostic recorded tasks 106, 107 and 108 expiring without any Hermes claim. A fourth candidate at 17:56 UTC passed the ADX prefilter, recovered exact bearish FVG geometry as verified and unmitigated within four seconds, entered the durable queue as task 109, received zero `/smog/next` claim and expired unclaimed at 18:07 UTC.
- This repeated result localises the operational failure to the external Hermes decision consumer. Signal ingestion, FVG recovery, ADX screening, queue persistence and demo exchange authentication were functioning. No discretionary fallback decision or synthetic order was introduced.
- The latest completed rolling batch through task ID 100 contains 50 UNDECIDED observations, four eligible Fibonacci proxy observations, one midpoint at/beyond the proxy 0.5 level, and zero actual closed demo trades. No win rate, realised R or P&L can be inferred. No strategy refinement was adopted from this batch.
- Protective-stop and BOS/4R management code was inactive because no position was opened. Existing limits remain one SOL-USDT net cross position, 3x maximum leverage, $100 maximum notional and $1 maximum initial stop-risk.


## 2026-11-17 — R4 depth investigation (read-only implementation)

- Hermes reported a zero-inference shadow replay of 30 candidates: 29 flagged by its R4 50% retracement rule, one hypothetical TAKE at -1R, zero exchange-reconciled fills. Its proprietary displacement anchor selection and candle trace were not independently accessible from Railway, so this rejection rate is an **unverified external claim**, not a proven Railway trading-rule failure.
- Railway already reconstructs and transmits identified FVG lower/upper bounds and midpoint; do not rebuild FVG enrichment. The missing piece is a validated CHoCH displacement **swing anchor**, which the TradingView webhook does not transmit. The existing OG-to-CHoCH extrema calculation is only a research proxy, not a trustworthy substitute for the proprietary indicator's structural anchors.
- Added isolated, **zero-LLM**, read-only R4 research that replays all completed Railway candidates against only confirmed BloFin 1m candles known by the CHoCH close. It compares OG-to-CHoCH extrema, trailing 5/10/20-minute windows and the most recent confirmed local pivot, independently for long and short directions, at 25%, 38.2%, 50% and 61.8% depth thresholds.
- An independent background worker backfills old candidates into `smog_r4_replays`; a token-authenticated `GET /smog/r4-research` returns observed passes/fails/unverifiable cases by proxy and threshold, plus disagreements between anchors. No model inference or trade orders are used in this analysis.
- **Safety boundary:** when a proprietary swing cannot be verified, the result remains `PROXY_OBSERVED` or `UNVERIFIABLE`; no backtest threshold is an authoritative TAKE/SKIP gate. Actual R4 trading threshold remains 50%, and market/exchange execution rules, demo-only host, stop and risk caps remain unchanged. The Hermes wake budget hold remains external and is not modified by this Railway deployment.


## 2026-11-17 — post-21:22 UTC signal-to-displacement mapping repair

- User requested a fix to the OG/CHoCH/FVG association problem after a 123-candidate zero-inference R4 replay found 64 proxy-invalid candidate signal orders, 43 legacy missing-anchor exceptions, and only 15 interpretable research proxies. No exchange-verified trade fills were demonstrated.
- The old selector grouped latest same-direction OG, CHoCH and FVG events in an unordered 30-minute window. This allowed FVG-before-OG and FVG-after-CHoCH pairings, and favoured late/shallow FVG events irrespective of the CHoCH displacement.
- Replaced selection with candle-time-ordered OG <= FVG <= internal CHoCH matching (30-minute original window) and a one-candidate-per-fresh-alert depth-first heuristic selecting the earliest post-OG FVG. This is **selection only**, not proof of actual same-displacement membership.
- Added independent candle-based structural diagnostics: prior confirmed pivot, break on the CHoCH closing candle, ensuing opposite impulse extreme, FVG formation position and a reconstructed 50% depth proxy. It uses only closed candles through the CHoCH; all outputs explicitly remain non-authoritative and non-executable.
- Existing webhook prefilter/retry attaches these diagnostics to enriched candidates for external Hermes review, while leaving the official OHLC-verified FVG, mitigation, ADX, risk checks and demo-only order worker unchanged.
- Added authenticated /smog/association-audit and a one-shot read-only 48-hour event replay on deployment logging previous candidate-order failure causes and number of newly ordered candidates, with zero LLM usage and zero replay orders.
- Test suite includes out-of-order delivery, missing bars, forward-look prevention, bullish/bearish breaks, bogus post-CHoCH FVGs, later shallow gaps, and read-only audit properties.
- Fundamental dependency remains: the current TradingView payload exposes only event/time/close, not the proprietary indicator's precise internal CHoCH swing anchors. A true independent authoritative R4 gate requires those upstream levels. **Do not claim this repair validates the trading strategy or automatically produces fills.**
- The outside Hermes runtime's one-evaluation-budget hold is not changed. No 50% R4 threshold, stop, position size, time windows, demo exchange risk limits or unvalidated retry permissions were relaxed.

## 2026-11-17 late evening — LuxAlgo SMC inputs and Pine structure replay

- The user supplied three screenshots of **LuxAlgo Smart Money Concepts** on their TradingView SOL-USDT chart. Internal and swing structure show both bullish and bearish `All`, internal confluence filter **off**, swing-point lookback **50**, and the SMC Fair Value Gaps option **off**. Equal highs/lows use three confirming bars and threshold 0.1. The official SMC is open source, correcting an earlier inaccurate description of inaccessible proprietary swing logic.
- Derived a read-only closed-candle Pine v5 structure replay from the public `Smart Money Concepts (SMC) [LuxAlgo]` script, with CC BY-NC-SA 4.0 / © LuxAlgo attribution, internal 5-bar and swing 50-bar pivots, previous/current close crossover, pivot-cross idempotency, trend BOS-to-CHoCH transitions and the disabled confluence filter. The separate FVG signal stream keeps its existing independently reconstructed LuxAlgo Fair Value Gap v4 geometry.
- Nested `luxalgo_structure` evidence on candidate assessments records matched/no-match/unverifiable CHoCH bar, inferred pivot references and research-only R4 midpoint depth. It reuses the existing single read-only closed OHLC fetch, spends **zero inference tokens**, and has no route to exchange execution or strategy pass/fail. The previous displacement proxy is retained for comparison.
- Added a one-shot, bounded zero-inference parity audit sampling up to 100 genuinely received internal CHoCH alerts in the prior 48 hours. It queries closed BloFin candles only up to each event's time, aggregates matches/mismatches/unknowns and logs `SMOG_LUXALGO_PARITY` on Railway without changing the trade ledger or any strategy rule.
- Local isolated test suite passed **93 tests**, including long/short structures, pivot confirmation, wrong-direction events, no lookahead, insufficient warmup, missing candles, candidate attachment and parity count aggregation. Historical TradingView-versus-BloFin parity must be read from production after deployment. No filled trades or realised P&L are claimed from this work.

## 2026-11-17 23:17 UTC — first historical SMC CHoCH parity result

- The production zero-inference audit compared **86 actual received internal CHoCH alert timestamps** against the independent closed 1m BloFin LuxAlgo-rule replay. **63 matched and 23 did not** (73.3% observed timestamp/direction/tag agreement). Zero candle-fetch errors, zero model tokens and zero orders. This is signal-reconstruction agreement and cannot be interpreted as profitable or filled trades.
- The 23 mismatches need classification before treating reconstructed break pivots as a deterministic R4 order gate. Added reason counts distinguishing a BOS on the alert bar, an opposite internal event and no matching internal cross on the alert bar, with eight bounded timestamp examples for read-only diagnosis.
- First classified production run on a later 85-alert 48h window found **62 matched / 23 unmatched**. Of the mismatches, **10 were BOS where TradingView emitted CHoCH** and **13 had no internal cross on the alert candle**. This pattern suggests truncated indicator state and/or differences in exchange/chart candles.
- Confirmed the official BloFin 1m candle API permits `limit` up to 1440. Added a **read-only 720-bar versus 200-bar paired replay** of the same genuine alert samples, using a single larger candle request per alert, to quantify whether warm-up depth explains mismatches. Production candidate evaluation continues to use its existing candle budget until this is measured.
