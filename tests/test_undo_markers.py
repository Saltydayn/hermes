"""Undo/redo x Mark Audio regression tests (bugfix round, 2026-07-12).

Two bugs compounded to make undo look broken whenever Mark Audio boundaries were
involved: (1) discrete keyboard/button edits (C / V / I / O) relied on the 600ms
coalesce timer to close their own undo step, so two quick presses collapsed into
one; (2) undo/redo restore ran the restored segments back through the equal-gain
merge pass, which silently erases a Mark Audio boundary left at 0 dB (5.5.2f's
documented "self-cleans" behavior) - so undoing a single marker removal wiped every
untouched 0 dB marker in the clip, not just the one removal. A third, related bug:
live operations that touch one end of the clip (Cut, Set In, Set Out) ran the SAME
global equal-gain merge, so cutting anywhere in the clip could erase a distant,
untouched marker.

Boots a real EditorModule against a fake app + synthetic clip, same pattern as
tests/test_editor_persistence.py.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk    # noqa: E402

import modules.clip_editor as ce          # noqa: E402
from shared.bus import MessageBus          # noqa: E402
from shared import paths as shared_paths   # noqa: E402
from tests import synth                    # noqa: E402


class _FakeApp:
    def __init__(self):
        self.bus = MessageBus()
        self.config = {"editor": {}}
        self.paths = shared_paths
        self.save_calls = 0

    def save_config(self):
        self.save_calls += 1


class TestUndoMarkers(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"Tk unavailable: {ex}")
        cls.root.geometry("1100x700+80+80")
        try:
            cls.clip_a = synth.make_temp_clip(n=100)
        except RuntimeError as ex:
            cls.root.destroy()
            raise unittest.SkipTest(str(ex))

    @classmethod
    def tearDownClass(cls):
        try:
            if getattr(cls, "clip_a", None):
                os.remove(cls.clip_a)
        except OSError:
            pass
        try:
            cls.root.destroy()
        except tk.TclError:
            pass

    def _settle(self):
        for _ in range(6):
            self.root.update()

    def _boot(self, app=None):
        ed = ce.EditorModule(self.root, app or _FakeApp())
        ed.pack(fill="both", expand=True)
        ed._load_waveform = lambda *a, **k: None
        self._settle()
        self.addCleanup(ed.destroy)
        self.addCleanup(ed.on_close)
        ed.load_clip(self.clip_a)
        self._settle()
        return ed

    def test_two_rapid_markers_are_two_undo_steps(self):
        """Two Mark Audio presses fired back-to-back (well inside the 600ms coalesce
        window) must still land as two distinct undo steps, not one."""
        ed = self._boot()
        ed.frame_idx = 20
        ed._mark_audio()
        ed.frame_idx = 60
        ed._mark_audio()
        # Both markers present before touching undo at all.
        self.assertEqual(ed.segments,
                          [[0, 20, 0.0], [21, 60, 0.0], [61, 99, 0.0]])

        ed._do_undo()
        self.assertEqual(ed.segments, [[0, 20, 0.0], [21, 99, 0.0]],
                          "one undo should remove only the SECOND marker")

        ed._do_undo()
        self.assertEqual(ed.segments, [[0, 99, 0.0]],
                          "a second undo should remove the FIRST marker")
        self.assertFalse(ed._undo_stack.can_undo)

    def test_undo_redo_roundtrip_two_markers(self):
        ed = self._boot()
        ed.frame_idx = 20
        ed._mark_audio()
        ed.frame_idx = 60
        ed._mark_audio()
        full = [[0, 20, 0.0], [21, 60, 0.0], [61, 99, 0.0]]
        self.assertEqual(ed.segments, full)

        ed._do_undo()
        ed._do_undo()
        self.assertEqual(ed.segments, [[0, 99, 0.0]])

        ed._do_redo()
        self.assertEqual(ed.segments, [[0, 20, 0.0], [21, 99, 0.0]])
        ed._do_redo()
        self.assertEqual(ed.segments, full, "redo did not restore both markers")

    def test_undo_marker_removal_keeps_other_markers(self):
        """Placing two 0 dB markers, removing ONE via right-click, then undoing that
        removal must restore BOTH markers - not wipe every 0 dB boundary in the clip
        (the renormalize-on-restore bug)."""
        ed = self._boot()
        ed.frame_idx = 20
        ed._mark_audio()
        ed.frame_idx = 60
        ed._mark_audio()
        full = [[0, 20, 0.0], [21, 60, 0.0], [61, 99, 0.0]]
        self.assertEqual(ed.segments, full)

        ed._remove_marker(60)          # delete only the second boundary
        self.assertEqual(ed.segments, [[0, 20, 0.0], [21, 99, 0.0]])

        ed._do_undo()
        self.assertEqual(ed.segments, full,
                          "undo of one marker removal must not erase the other marker")

    def test_cut_elsewhere_does_not_erase_a_distant_marker(self):
        """A Mark Audio boundary must survive a Cut made somewhere else in the clip -
        the global equal-gain merge must not sweep up an untouched marker."""
        ed = self._boot()
        ed.frame_idx = 20
        ed._mark_audio()               # boundary at 20, both sides 0 dB
        self.assertEqual(ed.segments, [[0, 20, 0.0], [21, 99, 0.0]])

        ed.frame_idx = 70
        ed._toggle_cut()                # mark cut start at 70
        ed.frame_idx = 80
        ed._toggle_cut()                # remove [70, 80], far from the marker at 20

        self.assertEqual(ed.segments,
                          [[0, 20, 0.0], [21, 69, 0.0], [81, 99, 0.0]],
                          "the marker at frame 20 must survive an unrelated cut")

    def test_set_in_point_does_not_erase_a_distant_marker(self):
        ed = self._boot()
        ed.frame_idx = 50
        ed._mark_audio()
        self.assertEqual(ed.segments, [[0, 50, 0.0], [51, 99, 0.0]])

        ed.frame_idx = 5
        ed.set_in_point()
        self.assertEqual(ed.segments, [[5, 50, 0.0], [51, 99, 0.0]],
                          "Set In must not merge away the untouched marker at 50")

    def test_set_out_point_does_not_erase_a_distant_marker(self):
        ed = self._boot()
        ed.frame_idx = 50
        ed._mark_audio()
        self.assertEqual(ed.segments, [[0, 50, 0.0], [51, 99, 0.0]])

        ed.frame_idx = 90
        ed.set_out_point()
        self.assertEqual(ed.segments, [[0, 50, 0.0], [51, 90, 0.0]],
                          "Set Out must not merge away the untouched marker at 50")

    def test_undo_current_stays_in_sync_after_restore(self):
        """The bug's underlying symptom: _undo_current (the basis for the NEXT undo
        step) must match the materialized ed.segments after every mutation and every
        restore, or the following undo/redo desyncs into an apparent no-op ('empty
        step')."""
        ed = self._boot()
        ed.frame_idx = 30
        ed._mark_audio()
        self.assertEqual(ed._undo_current["segments"], ed.segments)

        ed._do_undo()
        self.assertEqual(ed._undo_current["segments"], ed.segments)

        ed._do_redo()
        self.assertEqual(ed._undo_current["segments"], ed.segments)


if __name__ == "__main__":
    unittest.main()
