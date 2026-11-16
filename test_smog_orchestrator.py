import unittest
from smog_orchestrator import Event, detect_candidates


def e(i, kind, minute):
    return Event(i, kind, minute * 60_000, 70.0)


class WakeUpTests(unittest.TestCase):
    def test_no_wake_for_partial_setup(self):
        self.assertEqual(detect_candidates([
            e(1, "og_long", 0), e(2, "internal_bullish_choch", 2)
        ]), [])

    def test_wake_on_complete_long(self):
        result = detect_candidates([
            e(1, "og_long", 0), e(2, "internal_bullish_choch", 2),
            e(3, "bullish_fvg_formed", 3)
        ])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].direction, "long")
        self.assertFalse(result[0].to_payload()["execution_allowed"])

    def test_fvg_can_precede_choch(self):
        result = detect_candidates([
            e(1, "bullish_fvg_formed", 0), e(2, "og_long", 1),
            e(3, "internal_bullish_choch", 2)
        ])
        self.assertEqual(len(result), 1)

    def test_mitigation_of_another_gap_does_not_globally_suppress(self):
        self.assertEqual(len(detect_candidates([
            e(1, "bullish_fvg_formed", 0), e(2, "bullish_fvg_mitigated", 1),
            e(3, "og_long", 2), e(4, "internal_bullish_choch", 3)
        ])), 1)

    def test_no_mixed_direction(self):
        self.assertEqual(detect_candidates([
            e(1, "og_long", 0), e(2, "internal_bearish_choch", 1),
            e(3, "bullish_fvg_formed", 2)
        ]), [])

    def test_no_wake_for_stale_events(self):
        self.assertEqual(detect_candidates([
            e(1, "og_long", 0), e(2, "internal_bullish_choch", 1),
            e(3, "bullish_fvg_formed", 35)
        ]), [])

    def test_duplicate_delivery_does_not_wake_twice(self):
        events = [e(1, "og_long", 0), e(2, "internal_bullish_choch", 1),
                  e(3, "bullish_fvg_formed", 2)]
        self.assertEqual(len(detect_candidates(events + events)), 1)


if __name__ == "__main__":
    unittest.main()
