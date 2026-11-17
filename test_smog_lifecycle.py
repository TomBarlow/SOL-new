"""Regression coverage for BloFin API shapes and demo trade mechanics."""
import unittest
from decimal import Decimal
from unittest.mock import patch
from datetime import datetime, timezone
from unittest.mock import MagicMock

from smog_demo_execution import accepted_id, client_action_id, ExecutionBlocked
from smog_lifecycle import _fill_satisfies_target, _price_at_four_r, _safe_tpsl
from smog_lifecycle import manage_positions


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


class ManagementTriggerTests(unittest.TestCase):
    def _actions(self, direction="long", price="101", bos=False,
                 partial_filled=False, be_moved=False):
        entry = Decimal("100")
        initial_stop = Decimal("99" if direction == "long" else "101")
        position = {
            "intent_id": 123,
            "setup_id": "test-demo-only",
            "direction": direction,
            "entry_price": entry,
            "initial_stop": initial_stop,
            "current_stop": initial_stop,
            "initial_size": Decimal("1"),
            "partial_filled": partial_filled,
            "be_moved": be_moved,
            "bos_at": datetime.now(timezone.utc) if bos else None,
            "bos_bar_ms": 1_794_900_000_000 if bos else None,
            "opened_at": datetime.now(timezone.utc),
        }
        live = {
            "positions": "1" if direction == "long" else "-1",
            "markPrice": price,
            "positionId": "demo-position",
        }
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchall.return_value = [(123,)]
        with (
            patch("smog_lifecycle._load_position", return_value=position),
            patch("smog_lifecycle._position", return_value=live),
            patch("smog_lifecycle._safe_tpsl", return_value={"tpslId": "demo-stop"}),
            patch("smog_lifecycle._reconcile_actions"),
            patch("smog_lifecycle._bos_after_entry", return_value=None),
            patch("smog_lifecycle._book_fills"),
            patch("smog_lifecycle._new_trailing_fvg", return_value=None),
            patch("smog_lifecycle.demo_instrument", return_value={
                "tickSize": "0.01", "lotSize": "0.01",
            }),
            patch("smog_lifecycle._transmit") as transmit,
        ):
            manage_positions(lambda: connection)
        return [call.args[2] for call in transmit.call_args_list]

    def test_bos_moves_stop_without_reducing_position(self):
        self.assertEqual(self._actions(price="101", bos=True), ["stop_breakeven"])

    def test_four_r_takes_half_without_bos(self):
        self.assertEqual(self._actions(price="104"), ["partial_close"])
        self.assertEqual(self._actions(direction="short", price="96"), ["partial_close"])

    def test_both_triggers_move_stop_first_then_take_half(self):
        self.assertEqual(self._actions(price="104", bos=True), ["stop_breakeven"])
        self.assertEqual(self._actions(price="104", bos=True, be_moved=True),
                         ["partial_close"])

    def test_short_bos_moves_stop_without_reducing_position(self):
        self.assertEqual(self._actions(direction="short", price="99", bos=True),
                         ["stop_breakeven"])

    def test_breakeven_amendment_waits_for_valid_market_side(self):
        self.assertEqual(self._actions(price="99.95", bos=True), [])
        self.assertEqual(self._actions(direction="short", price="100.05", bos=True), [])

    def test_existing_partial_can_still_move_to_breakeven_on_bos(self):
        self.assertEqual(self._actions(price="101", bos=True, partial_filled=True),
                         ["stop_breakeven"])

    def test_bos_with_existing_breakeven_does_not_take_profit_before_four_r(self):
        self.assertEqual(self._actions(price="103", bos=True, be_moved=True), [])


if __name__ == "__main__":
    unittest.main()
