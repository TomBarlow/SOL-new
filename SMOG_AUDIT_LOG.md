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
