"""share = auto: the reply and notable-solve messages say the user pre-approved posting; ask (default) says nothing."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend  # noqa: E402
import messages  # noqa: E402


class ShareSetting(unittest.TestCase):
    def test_auto_adds_the_note(self):
        with mock.patch.dict(os.environ, {"BREAKPOINT_SHARE": "auto"}):
            self.assertEqual(backend.share_mode(), "auto")
            self.assertIn(messages.AUTO_SHARE, messages.leave_reply(76, "python -m unittest", "8ddb7ec3"))
            self.assertIn(messages.AUTO_SHARE, messages.notable_solve("python -m pytest", 1, "ed750af3", [],
                                                                      open_question_id=75))

    def test_ask_adds_nothing(self):
        with mock.patch.dict(os.environ, {"BREAKPOINT_SHARE": "ask"}):
            self.assertEqual(backend.share_mode(), "ask")
            self.assertNotIn(messages.AUTO_SHARE, messages.leave_reply(76, "python -m unittest", "8ddb7ec3"))

    def test_unknown_values_count_as_ask(self):
        with mock.patch.dict(os.environ, {"BREAKPOINT_SHARE": "yes please"}):
            self.assertEqual(backend.share_mode(), "ask")


if __name__ == "__main__":
    unittest.main()
