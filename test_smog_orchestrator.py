"""CHoCH-centred candidate selection regression tests."""
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
        events = [e(1, "og_long", 0), e(2, "bullish_fvg_formed", 1),
                  e(3, "internal_bullish_choch", 2)]
        result = detect_candidates(events)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].direction, "long")
        payload = result[0].to_payload()
        self.assertFalse(payload["execution_allowed"])
        self.assertFalse(payload["association_selection"]["candle_membership_verified"])
        self.assertEqual(payload["signal_bars"], {
            "og": 0, "internal_choch": 2 * 60_000,
            "fvg_formed": 1 * 60_000,
        })

    def test_wake_with_fvg_before_choch(self):
        result = detect_candidates([
            e(1, "og_long", 0), e(2, "bullish_fvg_formed", 1),
            e(3, "internal_bullish_choch", 2),
        ])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].fvg_bar_time_ms, 60_000)

    def test_pre_og_fvg_not_misidentified_as_displacement(self):
        self.assertEqual(detect_candidates([
            e(1, "bullish_fvg_formed", 0), e(2, "og_long", 1),
            e(3, "internal_bullish_choch", 2),
        ]), [])

    def test_fvg_after_choch_not_associated_even_if_latest(self):
        self.assertEqual(detect_candidates([
            e(1, "og_short", 0), e(2, "internal_bearish_choch", 2),
            e(3, "bearish_fvg_formed", 3)
        ]), [])

    def test_og_after_choch_cannot_be_borrowed(self):
        self.assertEqual(detect_candidates([
            e(1, "internal_bullish_choch", 0),
            e(2, "og_long", 1), e(3, "bullish_fvg_formed", 2)
        ]), [])

    def test_earliest_post_og_fvg_selected_not_latest_top_gap(self):
        result = detect_candidates([
            e(1, "og_long", 0), e(2, "bullish_fvg_formed", 1),
            e(3, "bullish_fvg_formed", 2), e(4, "internal_bullish_choch", 3)
        ])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].fvg_id, 2)

    def test_same_bar_fvg_and_choch_allowed(self):
        result = detect_candidates([
            e(1, "og_short", 0), e(2, "bearish_fvg_formed", 2),
            e(3, "internal_bearish_choch", 2),
        ])
        self.assertEqual(len(result), 1)

    def test_late_og_delivery_uses_bar_time_not_arrival_time(self):
        result = detect_candidates([
            e(1, "bearish_fvg_formed", 2),
            e(2, "internal_bearish_choch", 3),
            e(3, "og_short", 0),
        ])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].triggered_by_id, 3)

    def test_mitigation_of_another_gap_does_not_globally_suppress(self):
        self.assertEqual(len(detect_candidates([
            e(1, "og_long", 0),
            e(2, "bullish_fvg_formed", 1),
            e(3, "bullish_fvg_mitigated", 2),
            e(4, "internal_bullish_choch", 3)
        ])), 1)

    def test_no_mixed_direction(self):
        self.assertEqual(detect_candidates([
            e(1, "og_long", 0), e(2, "internal_bearish_choch", 1),
            e(3, "bullish_fvg_formed", 2)
        ]), [])

    def test_no_wake_for_stale_events(self):
        self.assertEqual(detect_candidates([
            e(1, "og_long", 0), e(2, "bullish_fvg_formed", 1),
            e(3, "internal_bullish_choch", 35)
        ]), [])

    def test_duplicate_delivery_does_not_wake_twice(self):
        events = [e(1, "og_long", 0), e(2, "bullish_fvg_formed", 1),
                  e(3, "internal_bullish_choch", 2)]
        self.assertEqual(len(detect_candidates(events + events)), 1)


if __name__ == "__main__":
    unittest.main()
