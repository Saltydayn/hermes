"""Phase 5.5.1c - bus publish recency + the exporter's recency-based source pick.

Pure and windowless: the bus is a plain object, and ExporterModule._pick_source reads
only self.bus, so it runs against a stub carrier without Tk.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.bus import MessageBus                 # noqa: E402
from modules.exporter import ExporterModule       # noqa: E402


class TestBusRecency(unittest.TestCase):
    def test_seq_strictly_increases(self):
        bus = MessageBus()
        bus.publish("raw_clip", "a.mp4")
        bus.publish("edited_clip", "b.mp4")
        bus.publish("raw_clip", "c.mp4")
        _, s1 = bus.latest_info("edited_clip")
        _, s2 = bus.latest_info("raw_clip")
        self.assertEqual(s1, 2)
        self.assertEqual(s2, 3)   # re-publish restamps with a HIGHER seq

    def test_latest_info_path_matches_latest(self):
        bus = MessageBus()
        bus.publish("raw_clip", "a.mp4")
        path, seq = bus.latest_info("raw_clip")
        self.assertEqual(path, "a.mp4")
        self.assertEqual(path, bus.latest("raw_clip"))
        self.assertGreater(seq, 0)

    def test_unpublished_dtype(self):
        bus = MessageBus()
        self.assertEqual(bus.latest_info("edited_clip"), (None, 0))
        self.assertIsNone(bus.latest("edited_clip"))

    def test_latest_behavior_unchanged(self):
        bus = MessageBus()
        seen = []
        bus.subscribe("raw_clip", seen.append)
        bus.publish("raw_clip", "a.mp4")
        bus.publish("raw_clip", "b.mp4")
        self.assertEqual(bus.latest("raw_clip"), "b.mp4")
        self.assertEqual(seen, ["a.mp4", "b.mp4"])


class TestExporterPick(unittest.TestCase):
    """_pick_source over a bus-only stub (no Tk, no widgets)."""

    def _pick(self, bus):
        stub = types.SimpleNamespace(bus=bus)
        return ExporterModule._pick_source(stub)

    def test_empty_bus(self):
        self.assertEqual(self._pick(MessageBus()), (None, None))

    def test_raw_only(self):
        bus = MessageBus()
        bus.publish("raw_clip", "raw.mp4")
        self.assertEqual(self._pick(bus), ("raw.mp4", "raw_clip"))

    def test_edited_after_raw_wins(self):
        bus = MessageBus()
        bus.publish("raw_clip", "raw.mp4")
        bus.publish("edited_clip", "edit.mp4")
        self.assertEqual(self._pick(bus), ("edit.mp4", "edited_clip"))

    def test_raw_after_edited_wins(self):
        # The stale-render trap that motivated the recency stamp: render clip A,
        # import clip B, never render B - the exporter must offer B's raw import,
        # not A's old render.
        bus = MessageBus()
        bus.publish("raw_clip", "a_raw.mp4")
        bus.publish("edited_clip", "a_edit.mp4")
        bus.publish("raw_clip", "b_raw.mp4")
        self.assertEqual(self._pick(bus), ("b_raw.mp4", "raw_clip"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
