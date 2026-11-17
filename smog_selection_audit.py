"""One-shot, zero-inference audit of historical candidate association."""
import logging
from collections import Counter

from smog_orchestrator import Event, SUPPORTED, detect_candidates

log = logging.getLogger("smog.selection.audit")


def audit_selection(conn_factory):
    """No mutations, no model inference, no orders, no synthetic signals."""
    with conn_factory() as conn:
        events = conn.execute("""
            SELECT id, event, bar_time, price FROM tv_events
            WHERE received_at > now() - interval '48 hours'
            ORDER BY id LIMIT 5000
        """).fetchall()
        historical = conn.execute("""
            SELECT payload FROM smog_tasks
            WHERE created_at > now() - interval '48 hours'
        """).fetchall()
    observed = [Event(*row) for row in events if row[1] in SUPPORTED]
    revised = detect_candidates(observed)
    reasons = Counter()
    for (candidate,) in historical:
        try:
            bars = candidate["signal_bars"]
            og = int(bars["og"])
            fvg = int(bars["fvg_formed"])
            choch = int(bars["internal_choch"])
        except (KeyError, TypeError, ValueError):
            reasons["LEGACY_WITHOUT_SIGNAL_BARS"] += 1
            continue
        if og > choch:
            reasons["OG_AFTER_CHOCH"] += 1
        if fvg < og:
            reasons["FVG_BEFORE_OG"] += 1
        if fvg > choch:
            reasons["FVG_AFTER_CHOCH"] += 1
        if og <= fvg <= choch:
            reasons["TIME_ORDER_ELIGIBLE"] += 1
    log.info(
        "SMOG_SELECTION_REPLAY window_hours=48 tv_events=%s "
        "prior_queued=%s revised_ordered_candidates=%s "
        "unique_choch_in_replay=%s old_order_counts=%s "
        "model_tokens=0 orders_sent=0",
        len(observed), len(historical), len(revised),
        len({x.choch_id for x in revised}), dict(reasons),
    )
    return {"events": len(observed), "prior_queued": len(historical),
            "revised_ordered_candidates": len(revised),
            "legacy_order_reasons": dict(reasons)}
