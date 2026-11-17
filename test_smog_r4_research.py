"""R4 research checks: no lookahead, proxy ambiguity, and read-only aggregation."""
import os
import unittest
from unittest.mock import MagicMock, patch

from smog_market_data import Candle
from smog_r4_research import assess_r4, THRESHOLDS, _confirmed_pivot_index
from smog_queue import r4_research


def candles(count=15):
    return [Candle(i * 60_000, 100 + i * 0.2, 101 + i * 0.2,
                   99 + i * 0.2, 100.5 + i * 0.2, 1)
            for i in range(count)]


def candidate(direction="long", midpoint=100.4):
    return {
        "direction": direction,
        "signal_bars": {"og": 2 * 60_000, "fvg_formed": 4 * 60_000,
                        "internal_choch": 12 * 60_000},
        "fvg": {"status": "VERIFIED",
                "formation_bar_ms": 4 * 60_000,
                "lower": midpoint - 0.1, "upper": midpoint + 0.1},
    }


class ResearchOnlyTests(unittest.TestCase):
    def test_deep_retracement_passes_and_returns_all_thresholds(self):
        result = assess_r4(candidate(midpoint=100.4), candles())
        self.assertEqual(result["status"], "PROXY_OBSERVED")
        baseline = result["proxy_results"]["og_to_choch"]
        self.assertTrue(baseline["thresholds"]["61_8"])
        self.assertEqual(set(baseline["thresholds"]), set(THRESHOLDS))
        self.assertFalse(result["execution_allowed"])
        self.assertFalse(result["anchor_authoritative"])
        self.assertFalse(result["rule_4_confirmed"])

    def test_shallow_retracement_does_not_turn_into_trade_rejection(self):
        result = assess_r4(candidate(midpoint=103.0), candles())
        baseline = result["proxy_results"]["og_to_choch"]
        self.assertFalse(baseline["thresholds"]["25"])
        self.assertFalse(baseline["thresholds"]["50"])
        self.assertEqual(result["status"], "PROXY_OBSERVED")
        self.assertFalse(result["rule_4_confirmed"])

    def test_short_direction_depth(self):
        result = assess_r4(candidate("short", midpoint=103.0), candles())
        self.assertTrue(result["proxy_results"]["og_to_choch"]["thresholds"]["50"])

    def test_closed_asof_prevents_future_data_leaking(self):
        original = assess_r4(candidate(midpoint=101.3), candles())
        future = Candle(15 * 60_000, 100, 250, 50, 105, 1)
        augmented = assess_r4(candidate(midpoint=101.3), candles() + [future])
        self.assertEqual(original, augmented)

    def test_missing_closed_bar_not_approximated(self):
        broken = [c for c in candles() if c.start_ms != 6 * 60_000]
        result = assess_r4(candidate(), broken)
        self.assertEqual(result["status"], "UNVERIFIABLE")
        self.assertEqual(result["reason"], "INCOMPLETE_CLOSED_DISPLACEMENT_WINDOW")

    def test_post_choch_gap_does_not_claim_displacement_association(self):
        row = candidate()
        row["fvg"]["formation_bar_ms"] = 13 * 60_000
        row["signal_bars"]["fvg_formed"] = 13 * 60_000
        result = assess_r4(row, candles())
        self.assertEqual(result["status"], "UNVERIFIABLE")
        self.assertEqual(result["reason"], "UNVERIFIED_OR_NONDISPLACEMENT_SIGNAL_ORDER")

    def test_unverified_fvg_fails_research_closed(self):
        row = candidate()
        row["fvg"]["status"] = "UNVERIFIED"
        self.assertEqual(assess_r4(row, candles())["status"], "UNVERIFIABLE")

    def test_pivot_must_be_confirmed_before_choch(self):
        rows = candles()
        # Create a potential pivot at the last bar; there are no confirming right bars.
        rows[-1] = Candle(rows[-1].start_ms, 100, 102, 90, 100, 1)
        sub = rows[:13]
        self.assertNotEqual(_confirmed_pivot_index(sub, "long"), 12)

    def test_summary_read_only_and_auth_scoped(self):
        result = assess_r4(candidate(midpoint=103.0), candles())
        conn = MagicMock()
        cm = MagicMock()
        cm.__enter__.return_value = conn
        conn.execute.return_value.fetchall.return_value = [("a" * 64, result, "SKIP")]
        conn.execute.return_value.fetchone.return_value = (0,)
        with patch.dict(os.environ, {"SMOG_API_TOKEN": "test-secret"}), patch(
            "smog_queue._db", return_value=cm):
            summary = r4_research(authorization="Bearer test-secret")
        self.assertEqual(summary["analysed"], 1)
        self.assertEqual(summary["anchor_threshold_counts"]["og_to_choch"]["50"]["fail"], 1)
        self.assertFalse(summary["execution_rules_changed"])
        self.assertTrue(summary["research_only"])
        self.assertEqual(summary["latest"][0]["original_decision"], "SKIP")


if __name__ == "__main__":
    unittest.main()
