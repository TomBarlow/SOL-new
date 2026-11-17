"""Bounded, read-only parity report over recorded TradingView CHoCH rows."""

import unittest

from smog_luxalgo_parity_audit import evaluate_choch_rows
from test_smog_luxalgo_structure import chart


class ParityAuditTests(unittest.TestCase):
    def test_counts_matched_misaligned_and_unverifiable_without_orders(self):
        calls = []

        def fetcher(interval, asof):
            calls.append((interval, asof))
            if asof == 8 * 60_000:
                raise ValueError("Candle feed missing")
            return chart()

        rows = [("internal_bullish_choch", 132 * 60_000),
                ("internal_bearish_choch", 132 * 60_000),
                ("internal_bearish_choch", 144 * 60_000),
                ("internal_bullish_choch", 7 * 60_000)]
        report = evaluate_choch_rows(rows, fetcher=fetcher, delay_seconds=0)
        self.assertEqual(report["reviewed"], 4)
        self.assertEqual(report["outcomes"], {
            "MATCHED_CHOCH": 2, "NO_MATCH": 1, "UNVERIFIABLE": 1})
        self.assertEqual(report["unverifiable_reasons"], {"ValueError": 1})
        self.assertEqual(report["mismatch_reasons"],
                         {"OPPOSITE_INTERNAL_EVENT": 1})
        self.assertEqual(calls[-1], ("1m", 8 * 60_000))


if __name__ == "__main__":
    unittest.main()
