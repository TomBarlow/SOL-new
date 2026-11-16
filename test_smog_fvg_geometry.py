import unittest

from smog_market_data import Candle
from smog_fvg_geometry import reconstruct


def c(t, o, h, l, close):
    return Candle(t * 60_000, o, h, l, close, 10)


class FvgIdentityTests(unittest.TestCase):
    def test_bull_gap_tracks_its_own_mitigation(self):
        candles = [
            c(0, 99.5, 100, 99, 99.5),
            c(1, 99.9, 100.4, 99.8, 100.2),
            c(2, 100.2, 101.5, 100.1, 101.1),
            c(3, 100.2, 100.3, 100.02, 100.04),
            c(4, 100.0, 100.0, 99.7, 99.9),
        ]
        formed = reconstruct(candles, 2 * 60_000, 2 * 60_000, "long")
        partial = reconstruct(candles, 2 * 60_000, 3 * 60_000, "long")
        mitigated = reconstruct(candles, 2 * 60_000, 4 * 60_000, "long")
        self.assertEqual((formed["upper"], formed["lower"]), (100.1, 100))
        self.assertEqual(formed["midpoint"], 100.05)
        self.assertFalse(formed["mitigated"])
        self.assertTrue(partial["touched"])
        self.assertFalse(partial["mitigated"])
        self.assertTrue(mitigated["mitigated"])
        self.assertEqual(formed["identity"], mitigated["identity"])

    def test_bear_gap_and_full_mitigation(self):
        candles = [
            c(0, 100.5, 101, 100, 100.5),
            c(1, 100.3, 100.4, 99.5, 99.8),
            c(2, 99.8, 99.9, 99, 99.2),
            c(3, 99.3, 99.95, 99.2, 99.95),
            c(4, 99.95, 100.1, 99.9, 100.01),
        ]
        formed = reconstruct(candles, 2 * 60_000, 2 * 60_000, "short")
        touched = reconstruct(candles, 2 * 60_000, 3 * 60_000, "short")
        mitigated = reconstruct(candles, 2 * 60_000, 4 * 60_000, "short")
        self.assertEqual((formed["upper"], formed["lower"]), (100, 99.9))
        self.assertTrue(touched["touched"])
        self.assertFalse(touched["mitigated"])
        self.assertTrue(mitigated["mitigated"])

    def test_non_gap_and_missing_history_fail_closed(self):
        candles = [
            c(0, 100, 101, 99, 100),
            c(1, 100, 101, 99, 100),
            c(2, 100, 101, 99, 100),
        ]
        with self.assertRaises(ValueError):
            reconstruct(candles, 2 * 60_000, 2 * 60_000, "long")
        with self.assertRaises(ValueError):
            reconstruct(candles, 2 * 60_000, 3 * 60_000, "short")


if __name__ == "__main__":
    unittest.main()
