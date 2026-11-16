import unittest
from unittest.mock import patch
from smog_adx_filter import evaluate_adx, _metrics
from smog_market_data import Candle


class AdxPrefilterTests(unittest.TestCase):
    def test_gate_strict_thresholds(self):
        with patch("smog_adx_filter.fetch_closed", side_effect=[[], []]), patch(
            "smog_adx_filter._metrics", side_effect=[
                {"adx": 25.0}, {"adx": 24.99}
            ]):
            self.assertEqual(evaluate_adx(600000)["status"], "PASS")
        with patch("smog_adx_filter.fetch_closed", side_effect=[[], []]), patch(
            "smog_adx_filter._metrics", side_effect=[
                {"adx": 25.0}, {"adx": 25.0}
            ]):
            self.assertEqual(evaluate_adx(600000)["status"], "REJECT")

    def test_off_choch_requires_both_below_20(self):
        with patch("smog_adx_filter.fetch_closed", side_effect=[[], []]), patch(
            "smog_adx_filter._metrics", side_effect=[
                {"adx": 19.9}, {"adx": 20.0}
            ]):
            self.assertEqual(evaluate_adx(600000, off_choch=True)["status"], "REJECT")

    def test_unknown_data_not_rejected(self):
        with patch("smog_adx_filter.fetch_closed", side_effect=ValueError("gap")):
            self.assertEqual(evaluate_adx(600000)["status"], "UNVERIFIED")

    def test_slope_and_acceleration(self):
        candles = [Candle(i * 60000, 100, 102 + i * .1, 99, 101, 1)
                   for i in range(60)]
        result = _metrics(candles)
        self.assertIn("slope", result)
        self.assertIn("acceleration", result)
        self.assertIn("trend", result)


if __name__ == "__main__":
    unittest.main()
