"""Non-authoritative, closed-candle CHoCH/FVG structure tests."""
import unittest
from unittest.mock import patch
from smog_market_data import Candle
from smog_displacement import assess_displacement, inspect_candidate


def bar(t, o, h, l, close):
    return Candle(t * 60_000, o, h, l, close, 10)


LONG = [
    bar(0, 100, 101, 99, 100), bar(1, 101, 102, 100, 101),
    bar(2, 103, 104, 101, 103), bar(3, 102, 103, 100, 101),
    bar(4, 99, 102, 98, 99), bar(5, 100, 101, 98.5, 100),
    bar(6, 102, 103, 100, 102), bar(7, 104, 105, 103.5, 104.5),
]
SHORT = [
    bar(0, 100, 101, 99, 100), bar(1, 99, 100, 98, 99),
    bar(2, 97, 99, 96, 97), bar(3, 97, 100, 97, 99),
    bar(4, 101, 103, 98, 102), bar(5, 101, 102.5, 100, 101),
    bar(6, 99, 100.5, 98, 99), bar(7, 95, 96.5, 94, 95),
]


def candidate(direction="long", fvg_minute=7):
    return {
        "candidate_key": "test",
        "direction": direction,
        "signal_bars": {"og": 0, "fvg_formed": fvg_minute * 60_000,
                        "internal_choch": 7 * 60_000},
        "asof_bar_time_ms": 7 * 60_000,
    }


class StructureTests(unittest.TestCase):
    def test_bullish_confirmed_break_and_same_displacement_fvg(self):
        with patch("smog_displacement.fetch_closed", return_value=LONG) as fetch:
            geom, structure = inspect_candidate(candidate())
        self.assertEqual(geom["status"], "VERIFIED")
        self.assertEqual(structure["status"], "PROXY_SUPPORTS_ASSOCIATION")
        self.assertEqual(structure["break_pivot_bar_ms"], 2 * 60_000)
        self.assertEqual(structure["proxy_impulse_start_bar_ms"], 4 * 60_000)
        self.assertFalse(structure["rule_4_confirmed"])
        self.assertFalse(structure["authoritative"])
        fetch.assert_called_once_with("1m", 8 * 60_000, limit=720)

    def test_bearish_confirmed_break_and_same_displacement_fvg(self):
        with patch("smog_displacement.fetch_closed", return_value=SHORT):
            geom, structure = inspect_candidate(candidate("short"))
        self.assertEqual(geom["status"], "VERIFIED")
        self.assertEqual(structure["status"], "PROXY_SUPPORTS_ASSOCIATION")
        self.assertEqual(structure["break_pivot_bar_ms"], 2 * 60_000)
        self.assertFalse(structure["authoritative"])

    def test_fvg_earlier_than_reconstructed_impulse_is_unverified(self):
        result = assess_displacement(candidate(fvg_minute=3), LONG,
                                     {"status": "VERIFIED", "midpoint": 101.2})
        self.assertEqual(result["reason"], "FVG_BEFORE_RECONSTRUCTED_IMPULSE")
        self.assertFalse(result["execution_allowed"])

    def test_close_without_actual_pivot_break_not_confirmed(self):
        rows = list(LONG)
        rows[-1] = bar(7, 103.6, 105, 103.5, 103.8)
        result = assess_displacement(candidate(), rows,
                                     {"status": "VERIFIED", "midpoint": 102.2})
        self.assertEqual(result["reason"], "PIVOT_BREAK_NOT_REPRODUCED")

    def test_future_candles_cannot_change_structure_snapshot(self):
        geom = {"status": "VERIFIED", "midpoint": 102.2}
        original = assess_displacement(candidate(), LONG, geom)
        later = bar(8, 200, 250, 150, 210)
        self.assertEqual(assess_displacement(candidate(), LONG + [later], geom),
                         original)

    def test_missing_closed_choch_bar_fails_closed(self):
        result = assess_displacement(candidate(), LONG[:-1],
                                     {"status": "VERIFIED", "midpoint": 102.2})
        self.assertEqual(result["reason"], "MISSING_CHOCH_CLOSED_BAR")

    def test_missing_intermediate_candle_fails_closed(self):
        rows = [c for c in LONG if c.start_ms != 3 * 60_000]
        result = assess_displacement(candidate(), rows,
                                     {"status": "VERIFIED", "midpoint": 102.2})
        self.assertEqual(result["reason"], "NONCONTIGUOUS_HISTORY")

    def test_unavailable_ohlc_never_turns_into_verified(self):
        with patch("smog_displacement.fetch_closed", side_effect=ValueError("Missing candle")):
            geometry, context = inspect_candidate(candidate())
        self.assertEqual(geometry["status"], "UNVERIFIED")
        self.assertEqual(context["status"], "UNVERIFIABLE")

    def test_post_choch_fvg_cannot_be_associated(self):
        result = assess_displacement(candidate(fvg_minute=8), LONG,
                                     {"status": "VERIFIED", "midpoint": 102.2})
        self.assertEqual(result["reason"], "OUTSIDE_ORDERED_OG_CHOCH_LEG")


if __name__ == "__main__":
    unittest.main()
