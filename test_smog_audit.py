"""Research-only FVG/Fibonacci geometry and shadow touch regression tests."""
import unittest
from unittest.mock import patch, MagicMock

from smog_audit import _fib_midpoint_context, _shadow, settle_observations
from smog_market_data import Candle


def candle(minute, low, high):
    middle = (low + high) / 2
    return Candle(minute * 60_000, middle, high, low, middle, 100)


class AuditGeometryTests(unittest.TestCase):
    def setUp(self):
        self.candles = [candle(i, 100 + i * 0.1, 101 + i * 0.1)
                        for i in range(80)]

    def candidate(self, direction="long", midpoint=102):
        return {
            "direction": direction,
            "signal_bars": {"og": 5 * 60_000,
                            "internal_choch": 20 * 60_000},
            "fvg": {"status": "VERIFIED", "formation_bar_ms": 10 * 60_000,
                    "lower": midpoint - 0.1, "upper": midpoint + 0.1,
                    "identity": "reference-gap"},
        }

    def test_long_midpoint_beyond_fib_half(self):
        result = _fib_midpoint_context(self.candidate(midpoint=101), self.candles)
        self.assertEqual(result["status"], "PROXY_OBSERVED")
        self.assertTrue(result["at_or_beyond_half"])
        self.assertGreater(result["fvg_midpoint_retracement"], 0.5)

    def test_short_midpoint_beyond_fib_half(self):
        result = _fib_midpoint_context(self.candidate("short", midpoint=103), self.candles)
        self.assertEqual(result["status"], "PROXY_OBSERVED")
        self.assertTrue(result["at_or_beyond_half"])

    def test_reversed_signal_order_is_unverifiable(self):
        candidate = self.candidate()
        candidate["fvg"]["formation_bar_ms"] = 21 * 60_000
        self.assertEqual(_fib_midpoint_context(candidate, self.candles)["status"],
                         "UNVERIFIABLE")

    def test_missing_candle_does_not_substitute_later_data(self):
        without_choch = [c for c in self.candles if c.start_ms != 20 * 60_000]
        self.assertEqual(_fib_midpoint_context(self.candidate(), without_choch)["reason"],
                         "INCOMPLETE_IMPULSE")

    def test_gap_outside_swing_is_not_claimed_as_fib_confirmation(self):
        result = _fib_midpoint_context(self.candidate(midpoint=110), self.candles)
        self.assertEqual(result["reason"], "MIDPOINT_OUTSIDE_SWING")

    def test_shadow_touch_requires_actual_price_interval_intersection(self):
        candidate = self.candidate()
        candidate["asof_bar_time_ms"] = 20 * 60_000
        # A candle entirely above the gap is not a touch, regardless of direction.
        future = [candle(i, 105, 106) for i in range(20, 80)]
        history = self.candles[:20]
        with patch("smog_audit.fetch_closed", return_value=history + future):
            result = _shadow(candidate)
        self.assertEqual(result["quality"], "OBSERVED")
        self.assertFalse(result["fvg_touched"])

    def test_expiration_logs_missing_hermes_claims_once(self):
        conn = MagicMock()
        cm = MagicMock()
        cm.__enter__.return_value = conn
        conn.execute.return_value.fetchall.side_effect = [
            [(123, 0), (124, 1)], [], [],
        ]
        with patch("smog_audit.log") as logger:
            settle_observations(lambda: cm)
        logger.warning.assert_called_once()
        arguments = logger.warning.call_args.args
        self.assertIn("SMOG_HERMES_QUEUE_STALL", arguments[0])
        self.assertEqual(arguments[1:], (2, 1, 1, 124))

    def test_no_expiration_produces_no_consumer_warning(self):
        conn = MagicMock()
        cm = MagicMock()
        cm.__enter__.return_value = conn
        conn.execute.return_value.fetchall.side_effect = [[], [], []]
        with patch("smog_audit.log") as logger:
            settle_observations(lambda: cm)
        logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()

