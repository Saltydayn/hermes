"""Phase 5.5.1e - the pure workflow-gap computation on Home.

workflow_gaps(config) reads only registry metadata + the enabled_modules config dict,
so it runs headless against plain dicts (no Tk).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.home import workflow_gaps   # noqa: E402


def _cfg(**enabled):
    return {"enabled_modules": enabled}


class TestWorkflowGaps(unittest.TestCase):
    def test_all_enabled_is_coherent(self):
        cfg = _cfg(importer=True, editor=True, exporter=True)
        self.assertEqual(workflow_gaps(cfg), [])

    def test_editor_without_importer(self):
        cfg = _cfg(importer=False, editor=True, exporter=False)
        gaps = workflow_gaps(cfg)
        self.assertEqual(len(gaps), 1)
        self.assertIn("Clip Editor", gaps[0])
        self.assertIn("Import", gaps[0])
        self.assertIn("enabled to receive clips", gaps[0])

    def test_exporter_without_editor(self):
        cfg = _cfg(importer=True, editor=False, exporter=True)
        gaps = workflow_gaps(cfg)
        self.assertEqual(len(gaps), 1)
        self.assertIn("Export", gaps[0])
        self.assertIn("Clip Editor", gaps[0])

    def test_home_only_is_coherent(self):
        # Home (and About) are always-on and consume nothing.
        self.assertEqual(workflow_gaps(_cfg()), [])

    def test_gap_is_per_consumer_not_per_chain(self):
        # Importer off, editor + exporter on: only the editor lacks an input (the
        # ENABLED editor satisfies the exporter regardless of its own inputs), so
        # exactly one line, naming the editor.
        cfg = _cfg(importer=False, editor=True, exporter=True)
        gaps = workflow_gaps(cfg)
        self.assertEqual(len(gaps), 1)
        self.assertIn("Clip Editor", gaps[0])

    def test_unproduced_dtype_never_warns(self):
        # The exporter also consumes subtitled_clip, which nothing produces until
        # Phase 6 - a coherent selection must not warn about it.
        cfg = _cfg(importer=True, editor=True, exporter=True)
        self.assertEqual(workflow_gaps(cfg), [])

    def test_no_em_dash(self):
        cfg = _cfg(importer=False, editor=True, exporter=True)
        for line in workflow_gaps(cfg):
            self.assertNotIn("—", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
