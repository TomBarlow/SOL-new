"""Research-only replication of the open-source LuxAlgo SMC structure rules."""

import unittest

from smog_luxalgo_structure import compare_choch, replay_structure
from smog_market_data import Candle


def chart():
    """Deterministic repeated swings with confirmed internal transitions."""
    rows = []
    for minute in range(160):
        phase = minute % 24
        price = (100 + (phase if phase < 12 else 24 - phase) * 0.35
                 + ((minute // 24) % 3 - 1) * 0.35)
        rows.append(Candle(minute * 60_000, price - 0.04, price + 0.15,
                           price - 0.15, price + 0.04, 10))
    return rows


def candidate(direction, minute):
    return {"direction": direction,
            "signal_bars": {"internal_choch": minute * 60_000}}


class LuxAlgoTests(unittest.TestCase):
    def test_both_changes_of_character_have_reconstructed_break_levels(self):
        rows = chart()
        bullish = compare_choch(candidate("long", 132), rows)
        bearish = compare_choch(candidate("short", 144), rows)
        for result, direction in ((bullish, "long"), (bearish, "short")):
            self.assertEqual(result["status"], "MATCHED_CHOCH", direction)
            self.assertIsNotNone(result["break_pivot_ms"])
            self.assertIsNotNone(result["opposite_pivot_ms"])
            self.assertFalse(result["authoritative"])
            self.assertFalse(result["execution_allowed"])
        self.assertGreater(bullish["break_level"], bearish["break_level"])

    def test_internal_bos_precedes_first_change_of_character(self):
        events = [e for e in replay_structure(chart())
                  if e["structure"] == "internal"]
        self.assertEqual(events[0]["event"], "BOS")
        self.assertEqual(events[0]["direction"], "bearish")
        self.assertEqual(events[1]["event"], "CHoCH")
        self.assertEqual(events[1]["direction"], "bullish")

    def test_a_wrong_direction_is_observable_without_authorizing_entry(self):
        result = compare_choch(candidate("short", 132), chart())
        self.assertEqual(result["status"], "NO_MATCH")
        self.assertEqual(result["observed_internal_events_on_bar"],
                         [{"direction": "bullish", "event": "CHoCH"}])
        self.assertFalse(result["execution_allowed"])

    def test_no_future_lookahead(self):
        rows = chart()
        result = compare_choch(candidate("long", 132), rows[:133])
        self.assertEqual(result,
                         compare_choch(candidate("long", 132), rows))

    def test_confirmed_pivots_support_read_only_fibonacci_observation(self):
        setup = candidate("long", 132)
        setup["signal_bars"]["fvg_formed"] = 129 * 60_000
        result = compare_choch(setup, chart(),
                               {"status": "VERIFIED", "midpoint": 102.5})
        context = result["r4_research"]
        self.assertGreater(context["swing_high"], context["swing_low"])
        self.assertTrue(context["fvg_inside_confirmed_pivot_window"])
        self.assertFalse(context["authoritative"])
        self.assertFalse(result["execution_allowed"])

    def test_insufficient_warmup_remains_unverified(self):
        result = compare_choch(candidate("short", 72), chart()[:73])
        self.assertEqual(result["status"], "UNVERIFIABLE")

    def test_missing_candle_data_is_unverifiable(self):
        rows = [r for r in chart() if r.start_ms != 50 * 60_000]
        result = compare_choch(candidate("long", 132), rows)
        self.assertEqual(result["status"], "UNVERIFIABLE")


if __name__ == "__main__":
    unittest.main()
