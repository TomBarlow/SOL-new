"""A trade cannot be promoted from unrelated same-direction alerts."""
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from smog_queue import Decision, submit_decision


class FakeConnection:
    def __init__(self):
        self.writes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params):
        if query.lstrip().startswith("SELECT"):
            return self
        self.writes.append((query, params))
        return self

    def fetchone(self):
        return ("leased", None, {"signal_bars": {
            "internal_choch": 120_000, "fvg_formed": 60_000,
        }})


class StructureGateTests(unittest.TestCase):
    def _decision(self, alignment):
        return Decision(
            setup_id="setup-1", decision="TAKE", narrative="Illustrative setup",
            model="z-ai/glm-5.3",
            trade_plan={"direction": "long", "entry": 71, "stop": 70.7, "size": 1},
            rule_eval={"fvg_choch_displacement": alignment},
        )

    def test_take_requires_matching_alert_candles_and_evidence(self):
        connection = FakeConnection()
        with (patch.dict("os.environ", {"SMOG_API_TOKEN": "test-token"}),
              patch("smog_queue._db", return_value=connection)):
            for alignment in ({},
                              {"status": "PASS", "choch_bar_ms": 60_000,
                               "fvg_bar_ms": 120_000, "evidence": "Wrong candle"},
                              {"status": "PASS", "choch_bar_ms": 120_000,
                               "fvg_bar_ms": 60_000, "evidence": " "}):
                with self.assertRaises(HTTPException) as raised:
                    submit_decision(self._decision(alignment),
                                    authorization="Bearer test-token")
                self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(connection.writes, [])

    def test_take_with_matching_evidence_can_be_recorded(self):
        connection = FakeConnection()
        with (patch.dict("os.environ", {"SMOG_API_TOKEN": "test-token"}),
              patch("smog_queue._db", return_value=connection),
              patch("smog_demo_worker.promote_fresh_intent", return_value=False)):
            response = submit_decision(self._decision({
                "status": "PASS", "choch_bar_ms": 120_000,
                "fvg_bar_ms": 60_000,
                "evidence": "The three-candle gap is on the bullish break displacement",
            }), authorization="Bearer test-token")
        self.assertEqual(response["status"], "recorded")
        self.assertEqual(len(connection.writes), 2)


if __name__ == "__main__":
    unittest.main()
