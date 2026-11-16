import os
import unittest
from unittest.mock import patch, MagicMock
from fastapi import HTTPException
from pydantic import ValidationError

from smog_queue import _auth, Decision, enqueue_candidate, monitor


class QueueContractTests(unittest.TestCase):
    def test_no_secret_configured_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                _auth("Bearer any")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_wrong_bearer_rejected(self):
        with patch.dict(os.environ, {"SMOG_API_TOKEN": "test-secret"}):
            with self.assertRaises(HTTPException) as ctx:
                _auth("Bearer wrong")
            _auth("Bearer test-secret")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_no_executable_candidate_accepted(self):
        with self.assertRaises(ValueError):
            enqueue_candidate({"candidate_key": "test", "execution_allowed": True})

    def test_monitor_stable_idle_and_ready_responses(self):
        conn = MagicMock()
        cm = MagicMock()
        cm.__enter__.return_value = conn
        with patch("smog_queue._db", return_value=cm):
            conn.execute.return_value.fetchone.return_value = (0, 0)
            idle = monitor()
            self.assertEqual(idle.body, b"idle")
            self.assertEqual(monitor().body, idle.body)
            conn.execute.return_value.fetchone.return_value = (2, 17)
            ready = monitor()
            self.assertEqual(ready.body, b"ready:2:latest:17")
            self.assertEqual(ready.headers["cache-control"], "no-store")

    def test_decision_schema_rejects_unexpected_model_and_action(self):
        base = {"setup_id": "test", "decision": "SKIP",
                "narrative": "Not enough context", "model": "z-ai/glm-5.3"}
        Decision(**base)
        with self.assertRaises(ValidationError):
            Decision(**(base | {"model": "different-model"}))
        with self.assertRaises(ValidationError):
            Decision(**(base | {"decision": "BUY"}))


if __name__ == "__main__":
    unittest.main()
