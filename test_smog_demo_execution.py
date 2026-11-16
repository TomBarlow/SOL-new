import os
import unittest
from unittest.mock import patch
from smog_demo_execution import (
    prepare_limit_order, submit_order, cancel_order, demo_read, ExecutionBlocked
)


class DemoExecutionTests(unittest.TestCase):
    def test_long_and_short_plans(self):
        long = prepare_limit_order("setup-1", "long", "150", "1", "148")
        self.assertEqual(long["side"], "buy")
        self.assertEqual(long["instId"], "SOL-USDT")
        self.assertEqual(long["slTriggerPrice"], "148")
        self.assertEqual(long["clientOrderId"],
                         prepare_limit_order("setup-1", "long", "150", "1", "148")["clientOrderId"])
        short = prepare_limit_order("setup-2", "short", "150", "1", "152")
        self.assertEqual(short["side"], "sell")

    def test_invalid_stops_and_size(self):
        for args in [("long", 150, 1, 151), ("short", 150, 1, 149),
                     ("long", 150, -1, 149), ("long", "nan", 1, 149)]:
            with self.assertRaises(ValueError):
                prepare_limit_order("x", *args)

    def test_writes_always_blocked(self):
        with self.assertRaises(ExecutionBlocked):
            submit_order({})
        with self.assertRaises(ExecutionBlocked):
            cancel_order("123")

    def test_non_demo_host_blocked(self):
        with patch.dict(os.environ, {"BLOFIN_BASE_URL": "https://openapi.blofin.com"}):
            with self.assertRaises(ExecutionBlocked):
                demo_read("positions")

    def test_unknown_endpoint_blocked(self):
        with patch.dict(os.environ, {"BLOFIN_BASE_URL": "https://demo-trading-openapi.blofin.com"}):
            with self.assertRaises(ExecutionBlocked):
                demo_read("anything")


if __name__ == "__main__":
    unittest.main()
