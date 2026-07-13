"""Phase 5.5.5 - feedback/bug-report relay (pure payload builder only, no network).

post_feedback() itself makes a real HTTP call, so it is not exercised here beyond its
blank-url fast path (no network, deterministic). The payload builder is pure and is the
part worth locking down: it is what the Apps Script side parses.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.ui_helpers import FEEDBACK_TOKEN, feedback_payload, post_feedback   # noqa: E402


class TestFeedbackPayload(unittest.TestCase):
    def test_builds_expected_fields(self):
        payload = feedback_payload("bug", "the render crashed", "https://example.com", "v0.9.3")
        self.assertEqual(payload, {
            "kind": "bug",
            "message": "the render crashed",
            "link": "https://example.com",
            "app_version": "v0.9.3",
            "feedback_token": FEEDBACK_TOKEN,
        })

    def test_blank_link_becomes_empty_string(self):
        payload = feedback_payload("feedback", "love it", "", "v0.9.3")
        self.assertEqual(payload["link"], "")
        payload_none = feedback_payload("feedback", "love it", None, "v0.9.3")
        self.assertEqual(payload_none["link"], "")


class TestPostFeedbackBlankUrl(unittest.TestCase):
    def test_blank_url_fails_fast_without_network(self):
        ok, detail = post_feedback("", "bug", "message", "", "v0.9.3")
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_whitespace_url_fails_fast(self):
        ok, _detail = post_feedback("   ", "bug", "message", "", "v0.9.3")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
