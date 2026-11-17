# SMOG v2: ordered FVG-to-CHoCH candidate association

## What changed

TradingView's webhook signals must be associated by their **bar timestamps**, not merely grouped because the latest same-direction OG, internal CHoCH and FVG happened within 30 minutes. The previous selector could associate an FVG formed after the break (or before OG), and could prefer a shallow gap formed near the break over an earlier candidate from the same move.

The v2 research selector uses:

1. A same-direction **OG at or before** the internal CHoCH's alert bar.
2. An FVG **at or after OG and at or before CHoCH**.
3. The original **30-minute** window, applied to the ordered event bars.
4. The **earliest post-OG FVG** when there are several matching alert events. This is a depth-first heuristic; it does **not** establish that this FVG belongs to the proprietary CHoCH displacement.
5. Existing BloFin 1m closed-bar validation, FVG identity, mitigation, ADX, freshness and execution limits. Nothing is auto-approved on event timing alone.

The additional non-authoritative "choch_displacement_context" inspects the closed CHoCH candle, a locally confirmed swing pivot, its candle-close break, a subsequent opposite wick extreme, and the selected FVG's formation bar. It estimates a possible R4 depth **without bars after the CHoCH**, and labels it "PROXY_SUPPORTS_ASSOCIATION" or "UNVERIFIABLE". It is diagnostic evidence, not a replacement for the proprietary indicator's swing.

## Operational diagnostics

- Railway receiver logs "SMOG_ASSOCIATION_QUEUED" for new v2 queues. This contains selection version, proxy assessment, and FVG timing.
- GET /smog/association-audit (bearer-authenticated) reports association-selection and structure-status distributions, plus recent queue status with short IDs.
- At receiver startup, the one-shot "SMOG_SELECTION_REPLAY" compares the prior 48h of stored OG/CHoCH/FVG alerts against the new ordered matching algorithm and classifies historical missing/invalid signal bars. **No model wakes** and **no order submissions** are caused by this replay.
- The separate /smog/r4-research continues to label its varying swing-anchor calculations as *research proxies*. Comparisons on old tasks remain historical and should not be treated as a validated signal-rule experiment.

## Remaining data limitation

The webhook payload currently contains only the event name, alert bar time and bar-close price. It does **not** carry the proprietary internal CHoCH's actual structural swing high/low, break level, displacement start, or internal indicator state. Consequently, **Railway cannot independently know the exact indicator-defined displacement leg** from these inputs alone.

To make R4 an independently verifiable deterministic order gate, the TradingView indicator (or a compatible instrumented script) must emit the actual swing anchors with each CHoCH event. Do not infer those fields from the mere OG-to-CHoCH range, a first/last 5-minute window, or the candidate's alert-close price; those are not equivalent. Without that upstream change, Hermes still needs genuine candle-based evidence for TAKE, and failed/ambiguous structure must never be asserted to be an authoritative PASS.

## Safety

No change to the 50% canonical R4 requirement, 30m event window, 10m queue TTL, 3m executable freshness, OG authoritative source, demo-only BloFin URL, one net SOL-USDT cross position, 3x maximum leverage, $100 notional cap, $1 initial stop risk, BOS-to-BE management, 4R 50% profit-taking, exchange reconciliation or idempotency.

Hermes's external one-evaluation inference-budget hold is not modified by this Railway change.
