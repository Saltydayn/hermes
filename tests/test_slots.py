"""Phase 5.5.2e - dynamic slot delete checks (pure, headless).

`slot_delete_check` (layout presets: list of dicts | None) and `snippet_delete_check`
(text snippets: list of strings) both gate the last-slot delete: refuse at the floor,
confirm when the last slot holds content.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.clip_editor import slot_delete_check, MIN_SLOTS       # noqa: E402
from modules.exporter import snippet_delete_check, SNIPPET_MIN     # noqa: E402


class TestLayoutSlotDeleteCheck(unittest.TestCase):
    def test_floor_refused(self):
        self.assertEqual(slot_delete_check([None] * 5), (False, False, None))
        self.assertEqual(MIN_SLOTS, 5)

    def test_extra_empty_slot_no_confirm(self):
        self.assertEqual(slot_delete_check([None] * 6), (True, False, None))

    def test_occupied_last_needs_confirm_with_name(self):
        presets = [None] * 5 + [{"name": "Talky"}]
        self.assertEqual(slot_delete_check(presets), (True, True, "Talky"))

    def test_occupied_last_missing_name_falls_back(self):
        # A real preset always has content; one lacking a "name" key falls back to
        # the slot number. (An empty dict {} is falsy and reads as an empty slot.)
        presets = [None] * 5 + [{"split_ratio": 50}]
        allowed, confirm, name = slot_delete_check(presets)
        self.assertTrue(allowed and confirm)
        self.assertEqual(name, "Slot 6")

    def test_below_floor_list_still_refused(self):
        # A short/corrupt list still counts as the floor (max(MIN, len)).
        self.assertEqual(slot_delete_check([None] * 3), (False, False, None))


class TestSnippetDeleteCheck(unittest.TestCase):
    def test_floor_refused(self):
        self.assertEqual(snippet_delete_check([""] * 5), (False, False, None))
        self.assertEqual(SNIPPET_MIN, 5)

    def test_extra_empty_slot_no_confirm(self):
        self.assertEqual(snippet_delete_check([""] * 6), (True, False, None))

    def test_occupied_last_needs_confirm_with_preview(self):
        slots = [""] * 5 + ["my video title"]
        self.assertEqual(snippet_delete_check(slots), (True, True, "my video title"))

    def test_whitespace_only_last_is_empty(self):
        self.assertEqual(snippet_delete_check([""] * 5 + ["   "]), (True, False, None))

    def test_long_preview_truncated(self):
        long = "x" * 100
        allowed, confirm, preview = snippet_delete_check([""] * 5 + [long])
        self.assertTrue(allowed and confirm)
        self.assertEqual(len(preview), 40)
        self.assertTrue(preview.endswith("…"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
