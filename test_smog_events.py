import os
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from smog_events import _auth, _frame, events


class EventStreamContractTests(unittest.TestCase):
    def test_event_frame_contains_only_id_and_setup(self):
        frame = _frame(42, "setup-abc")
        self.assertEqual(frame, 'id: 42\nevent: candidate\ndata: {"setup_id":"setup-abc"}\n\n')
        self.assertNotIn("token", frame)

    def test_auth_required(self):
        with patch.dict(os.environ, {"SMOG_API_TOKEN": "test-secret"}):
            with self.assertRaises(HTTPException) as ctx:
                events(authorization="Bearer invalid", last_event_id=None)
            self.assertEqual(ctx.exception.status_code, 401)
            response = events(authorization="Bearer test-secret", last_event_id="42")
            self.assertEqual(response.media_type, "text/event-stream")

    def test_reject_invalid_cursor(self):
        with patch.dict(os.environ, {"SMOG_API_TOKEN": "test-secret"}):
            with self.assertRaises(HTTPException) as ctx:
                events(authorization="Bearer test-secret", last_event_id="abc")
            self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
