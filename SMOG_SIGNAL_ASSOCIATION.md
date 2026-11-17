# SMOG v2: ordered FVG-to-CHoCH candidate association

## What changed

TradingView's webhook signals must be associated by their **bar timestamps**, not merely grouped because the latest same-direction OG, internal CHoCH and FVG happened within 30 minutes. The previous selector could associate an FVG formed after the break (or before OG), and could prefer a shallow gap formed near the break over an earlier candidate from the same move.

The v2 research selector uses:

1. A same-direction **OG at or before** the internal CHoCH's alert bar.
2. An FVG **at or after OG and at or before CHoCH**.
3. The original **30-minute** window, applied to the ordered event bars.
4. The **earliest post-OG FVG** when there are several matching alert events. This is a depth-first heuristic; it does **not** establish that this FVG belongs to the relevant CHoCH displacement.
5. Existing BloFin 1m closed-bar validation, FVG identity, mitigation, ADX, freshness and execution limits. Nothing is auto-approved on event timing alone.

The additional non-authoritative "choch_displacement_context" inspects the closed CHoCH candle, a locally confirmed swing pivot, its candle-close break, a subsequent opposite wick extreme, and the selected FVG's formation bar. It estimates a possible R4 depth **without bars after the CHoCH**, and labels it "PROXY_SUPPORTS_ASSOCIATION" or "UNVERIFIABLE".

The user provided LuxAlgo Smart Money Concepts chart inputs on 2026-11-17. SMC is **open source**, so `smog_luxalgo_structure.py` now independently replays its 5-bar internal and 50-bar swing pivot/close-cross rules with the confluence filter disabled. The resulting `choch_displacement_context.luxalgo_structure` records `MATCHED_CHOCH`, `NO_MATCH`, or `UNVERIFIABLE`, the inferred pivot break level and opposite pivot, plus a clearly labelled exploratory R4 depth using that pivot. Replay uses the existing 200 closed BloFin 1m candles, is inference-free, and never approves a trade. The original proxy output is retained for historical comparability.

The user's SMC screenshot explicitly shows **Fair Value Gaps unchecked**. Existing SMOG FVG signals are sourced from the separately configured LuxAlgo Fair Value Gap alert, and its existing 0.02% threshold, midpoint and mitigation reconstruction are retained.

## Operational diagnostics

- Railway receiver logs "SMOG_ASSOCIATION_QUEUED" for new v2 queues. This contains selection version, proxy assessment, new LuxAlgo replay status, and FVG timing.
- GET /smog/association-audit (bearer-authenticated) reports association-selection and structure-status distributions, plus recent queue status with short IDs.
- At receiver startup, the one-shot "SMOG_SELECTION_REPLAY" compares the prior 48h of stored OG/CHoCH/FVG alerts against the new ordered matching algorithm and classifies historical missing/invalid signal bars. **No model wakes** and **no order submissions** are caused by this replay.
- Also at startup, a bounded background `SMOG_LUXALGO_PARITY` audit compares up to 100 genuine internal CHoCH alerts received in the last 48h against the reconstructed SMC rules using only historical closed 1m candles. It reports `MATCHED_CHOCH`, `NO_MATCH`, and `UNVERIFIABLE` totals without writing to the trade ledger, placing orders or waking Hermes.
- The parity audit also compares 200 and 720 bars of **the same historical OHLC**, identifying whether the previous short warm-up is responsible for incorrect CHoCH/BOS classification. This larger fetch is limited to background research until the observations justify adopting it for candidate prequalification.
- The separate /smog/r4-research continues to label its varying swing-anchor calculations as *research proxies*. Comparisons on old tasks remain historical and should not be treated as a validated signal-rule experiment.

## Remaining parity and data limitations

The webhook payload currently contains only the event name, alert bar time and bar-close price. It does **not** carry the internal CHoCH pivot level or the alert-time indicator state. Its public algorithm allows independent reconstruction, provided the script version, warm-up length and actual TradingView candles agree with BloFin's historical candle series. The current 200-candle history may begin after significant prior swings, changing the replayed initial structure bias.

The new `MATCHED_CHOCH` field is **an observational comparison**, with parity still unproven against historical TradingView alerts. The preceding confirmed opposite internal pivot is a useful structural reference, but it need not be the start of the trader's intended impulse/displacement for canonical R4. Before replacing the R4 verification with an automated hard gate, first measure agreement across actual webhook CHoCH events and validate the precise entry leg definition. Do not turn a missing or mismatching reconstructed signal into a fabricated TAKE or SKIP.

## Safety

No change to the 50% canonical R4 requirement, 30m event window, 10m queue TTL, 3m executable freshness, OG authoritative source, demo-only BloFin URL, one net SOL-USDT cross position, 3x maximum leverage, $100 notional cap, $1 initial stop risk, BOS-to-BE management, 4R 50% profit-taking, exchange reconciliation or idempotency.

Hermes's external one-evaluation inference-budget hold is not modified by this Railway change.
