"""Regression coverage for BloFin API shapes and demo trade mechanics."""
import unittest
from decimal import Decimal
from unittest.mock import patch

from smog_demo_execution import accepted_id, client_action_id, ExecutionBlocked
from smog_lifecycle import _fill_satisfies_target, _price_at_four_r, _safe_tpsl


class DemoLifecycleTests(unittest.TestCase):
    def test_successful_blofin_order_list_is_accepted(self):
        reply = {"code": "0", "data": [{"code": "0", "orderId": "1234567"}]}
        self.assertEqual(accepted_id(reply), "1234567")

    def test_individual_rejection_prevents_an_order_id(self):
        reply = {"code": "0", "data": [{"code": "123", "orderId": "1234567"}]}
        with self.assertRaises(ValueError):
            accepted_id(reply)

    def test_action_ids_are_stable_and_unique(self):
        first = client_action_id("setup", "partial_close")
        self.assertEqual(first, client_action_id("setup", "partial_close"))
        self.assertNotEqual(first, client_action_id("setup", "stop_breakeven"))
        self.assertEqual(len(first), 32)

    def test_four_r_respects_direction(self):
        base = {"entry_price": Decimal("100"), "initial_stop": Decimal("99"),
                "direction": "long"}
        self.assertFalse(_price_at_four_r(base, Decimal("103.99")))
        self.assertTrue(_price_at_four_r(base, Decimal("104")))
        base["direction"] = "short"
        base["initial_stop"] = Decimal("101")
        self.assertFalse(_price_at_four_r(base, Decimal("96.01")))
        self.assertTrue(_price_at_four_r(base, Decimal("96")))

    def test_stop_safety_requires_live_matching_protection(self):
        live = [{"tpslId": "11", "side": "sell", "positionSide": "net",
                 "slTriggerPrice": "99", "size": "2"}]
        with patch("smog_lifecycle._rows", return_value=live):
            self.assertEqual(_safe_tpsl("long", Decimal("1"), Decimal("99"))["tpslId"], "11")
            self.assertIsNone(_safe_tpsl("short", Decimal("1"), Decimal("101")))
            self.assertIsNone(_safe_tpsl("long", Decimal("3"), Decimal("99")))
            self.assertIsNone(_safe_tpsl("long", Decimal("1"), Decimal("100")))

    def test_partial_exchange_fill_does_not_confirm_full_exit(self):
        detail = {"state": "partially_filled", "filledSize": "0.40"}
        self.assertFalse(_fill_satisfies_target(detail, Decimal("1.00")))
        detail["filledSize"] = "1.00"
        self.assertTrue(_fill_satisfies_target(detail, Decimal("1.00")))

    def test_missing_or_zero_fill_never_confirms_action(self):
        self.assertFalse(_fill_satisfies_target(None, Decimal("1")))
        self.assertFalse(_fill_satisfies_target({"state": "filled"}, Decimal("1")))
        self.assertFalse(_fill_satisfies_target({"filledSize": "1"}, Decimal("0")))


if __name__ == "__main__":
    unittest.main()
