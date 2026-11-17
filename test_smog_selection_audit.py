"""Zero-inference historical signal-order and association endpoint tests."""
import os
import unittest
from unittest.mock import MagicMock, patch

from smog_queue import association_audit
from smog_selection_audit import audit_selection


class SelectionAuditTests(unittest.TestCase):
    def test_historical_replay_classifies_invalid_legacy_pairings(self):
        conn = MagicMock()
        cm = MagicMock()
        cm.__enter__.return_value = conn
        conn.execute.return_value.fetchall.side_effect = [
            [(1, "og_long", 0, 70.0),
             (2, "bullish_fvg_formed", 60_000, 70.1),
             (3, "internal_bullish_choch", 120_000, 70.2)],
            [({"signal_bars": {"og": 0, "fvg_formed": 60_000,
                              "internal_choch": 120_000}},),
             ({"signal_bars": {"og": 0, "fvg_formed": 180_000,
                              "internal_choch": 120_000}},),
             ({"old_format": True},)]
        ]
        with patch("smog_selection_audit.log"):
            result = audit_selection(lambda: cm)
        self.assertEqual(result["revised_ordered_candidates"], 1)
        self.assertEqual(result["prior_queued"], 3)
        self.assertEqual(result["legacy_order_reasons"]["TIME_ORDER_ELIGIBLE"], 1)
        self.assertEqual(result["legacy_order_reasons"]["FVG_AFTER_CHOCH"], 1)
        self.assertEqual(result["legacy_order_reasons"]["LEGACY_WITHOUT_SIGNAL_BARS"], 1)
        self.assertTrue(all(
            call.args[0].strip().startswith("SELECT")
            for call in conn.execute.call_args_list
        ))

    def test_association_audit_requires_auth_and_reports_proxies(self):
        conn = MagicMock()
        cm = MagicMock()
        cm.__enter__.return_value = conn
        conn.execute.return_value.fetchall.side_effect = [
            [("2", "PROXY_SUPPORTS_ASSOCIATION", 4), ("legacy", "NOT_ASSESSED", 10)],
            [("f" * 12, "earliest_post_og_fvg_before_choch",
              "PROXY_SUPPORTS_ASSOCIATION", None, "pending")]
        ]
        with patch.dict(os.environ, {"SMOG_API_TOKEN": "secret"}), patch(
            "smog_queue._db", return_value=cm):
            result = association_audit(authorization="Bearer secret")
        self.assertEqual(result["count_by_selection_and_structure"][0]["count"], 4)
        self.assertFalse(result["execution_rules_changed"])
        self.assertTrue(result["structure_is_research_only"])
        self.assertEqual(result["recent"][0]["queue_status"], "pending")


if __name__ == "__main__":
    unittest.main()
