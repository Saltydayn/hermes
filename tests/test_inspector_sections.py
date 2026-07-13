"""Phase 5.5.1a - collapsible inspector sections: the pure state helpers.

Headless: `_section_key` (title -> config key, non-alnum runs collapse to _) and
`_initial_collapsed` (saved state overrides the first-run defaults). The widget side
(toggle, pack order, config write) lives in the Tk harness, not here. Keys track the
current section set (5.5.2d: SEAM open, BRANDING collapsed).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modules.clip_editor as ce   # noqa: E402


class TestSectionKey(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(ce._section_key("SEAM"), "seam")
        self.assertEqual(ce._section_key("BRANDING"), "branding")
        self.assertEqual(ce._section_key("RENDER OPTIONS"), "render_options")
        self.assertEqual(ce._section_key("LAYOUTS"), "layouts")

    def test_non_alnum_sanitized(self):
        # A run of non-alphanumerics collapses to a single underscore.
        self.assertEqual(ce._section_key("CONTENT BOXES"), "content_boxes")
        self.assertEqual(ce._section_key("A & B"), "a_b")


class TestInitialCollapsed(unittest.TestCase):
    def test_first_run_defaults(self):
        for key in ce._COLLAPSED_DEFAULT:                 # branding/content_boxes/layouts/audio
            self.assertTrue(ce._initial_collapsed(key, {}), key)
        for key in ("seam", "render_options"):            # default-open
            self.assertFalse(ce._initial_collapsed(key, {}), key)

    def test_saved_state_overrides_defaults(self):
        saved = {"branding": False, "seam": True}
        self.assertFalse(ce._initial_collapsed("branding", saved))  # default-collapsed, opened
        self.assertTrue(ce._initial_collapsed("seam", saved))       # default-open, collapsed
        # Untouched keys keep their defaults.
        self.assertTrue(ce._initial_collapsed("audio", saved))
        self.assertFalse(ce._initial_collapsed("render_options", saved))

    def test_round_trip(self):
        # What a toggle writes (collapsed = not open) reads back identically.
        for key in ("content_boxes", "seam"):
            for collapsed in (True, False):
                self.assertEqual(ce._initial_collapsed(key, {key: collapsed}), collapsed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
