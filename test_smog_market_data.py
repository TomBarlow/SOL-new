import unittest
from smog_market_data import Candle, aggregate_5m, adx_entry_gate, latest_closed_adx, validate_candles, wilder_adx


class MarketDataTests(unittest.TestCase):
    def candles(self, count=80):
        return [Candle(i * 60_000, 100 + i * .1, 101 + i * .1,
                       99 + i * .1, 100.5 + i * .1, 10) for i in range(count)]

    def test_aggregation_complete_buckets_only(self):
        result = aggregate_5m(self.candles(12))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].start_ms, 0)
        self.assertEqual(result[0].volume, 50)

    def test_gaps_rejected(self):
        candles = self.candles(6)
        with self.assertRaises(ValueError):
            validate_candles(candles[:2] + candles[3:], 60_000)

    def test_adx_warmup_and_range(self):
        result = wilder_adx(self.candles())
        self.assertTrue(all(v is None for v in result[:27]))
        self.assertIsNotNone(result[27])
        self.assertTrue(all(0 <= v <= 100 for v in result[27:]))

    def test_no_open_candle_used(self):
        candles = self.candles(80)
        self.assertEqual(latest_closed_adx(candles, 79 * 60_000, 60_000),
                         wilder_adx(candles[:79])[-1])

    def test_strict_adx_gate(self):
        self.assertTrue(adx_entry_gate(24.99, 30))
        self.assertFalse(adx_entry_gate(25, 25))
        self.assertFalse(adx_entry_gate(None, 20))


if __name__ == "__main__":
    unittest.main()
