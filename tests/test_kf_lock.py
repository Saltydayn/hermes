"""Phase 5.5.1b - the Lock KFs toggle.

Boots a REAL EditorModule against a fake app (dict config, no disk writes) and a
synthetic clip, then drives the timeline's right-button retime gesture with
event_generate. Lock off: the drag moves the keyframe. Lock on: the identical gesture
leaves it untouched and only hints in the status line; add and delete keep working.
Also covers the config round trip (editor.lock_keyframes seeds the checkbox). Needs Tk;
self-skips headless.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk    # noqa: E402

import modules.clip_editor as ce          # noqa: E402
from shared import paths as shared_paths   # noqa: E402
from shared.bus import MessageBus          # noqa: E402
from tests import synth                    # noqa: E402


class _FakeApp:
    def __init__(self, editor_cfg=None):
        self.bus = MessageBus()
        self.config = {"editor": dict(editor_cfg or {})}
        self.paths = shared_paths

    def save_config(self):
        pass   # in-memory only


class TestKeyframeLock(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"Tk unavailable: {ex}")
        cls.root.geometry("1100x700+80+80")
        try:
            cls.clip = synth.make_temp_clip()
        except RuntimeError as ex:
            cls.root.destroy()
            raise unittest.SkipTest(str(ex))

    @classmethod
    def tearDownClass(cls):
        try:
            os.remove(cls.clip)
        except OSError:
            pass
        try:
            cls.root.destroy()
        except tk.TclError:
            pass

    def _settle(self):
        for _ in range(5):
            self.root.update()

    def _boot(self, app):
        ed = ce.EditorModule(self.root, app)
        ed.pack(fill="both", expand=True)
        ed._load_waveform = lambda *a, **k: None
        self._settle()
        # on_close first, then destroy (cleanups run LIFO) so the next test's editor
        # gets the full window height back.
        self.addCleanup(ed.destroy)
        self.addCleanup(ed.on_close)
        ed.load_clip(self.clip)
        self._settle()
        self.assertIsNotNone(ed.game_box)
        return ed

    def _right_drag(self, ed, from_frame, to_frame):
        """Simulate press on the marker at `from_frame`, drag to `to_frame`, release.
        BUILDER_NOTES: <B3-Motion> needs state=0x0400 (Button3) in event_generate."""
        c = ed._timeline
        x0 = int(round(ed._tl_x_of(from_frame)))
        x1 = int(round(ed._tl_x_of(to_frame)))
        y = ce.TL_MARKER_Y
        c.event_generate("<ButtonPress-3>", x=x0, y=y)
        self.root.update()
        c.event_generate("<B3-Motion>", x=x1, y=y, state=0x0400)
        self.root.update()
        c.event_generate("<ButtonRelease-3>", x=x1, y=y)
        self.root.update()

    def test_unlocked_drag_moves_marker(self):
        ed = self._boot(_FakeApp())
        self.assertFalse(ed._kf_lock_var.get())
        ed._seek_to(4)
        ed.add_keyframe("fluid")
        self._settle()
        self._right_drag(ed, 4, 20)
        frames = [k["frame"] for k in ed.keyframes]
        self.assertNotIn(4, frames, "drag did not move the keyframe")

    def test_locked_drag_is_ignored(self):
        ed = self._boot(_FakeApp())
        ed._kf_lock_var.set(True)
        ed._seek_to(4)
        ed.add_keyframe("fluid")
        self._settle()
        self._right_drag(ed, 4, 20)
        frames = [k["frame"] for k in ed.keyframes]
        self.assertEqual(frames, [4], "locked keyframe moved")
        self.assertIn("locked", str(ed._status.cget("text")).lower())
        self.assertIsNone(ed._tl_drag_kf, "locked press armed a drag")
        # Add and delete keep working while locked.
        ed._seek_to(10)
        ed.add_keyframe("jump")
        self.assertEqual(len(ed.keyframes), 2)
        ed.delete_keyframe()
        self.assertEqual(len(ed.keyframes), 1)

    def test_lock_pref_round_trip(self):
        # Saved pref seeds the checkbox; toggling writes it back.
        ed = self._boot(_FakeApp({"lock_keyframes": True}))
        self.assertTrue(ed._kf_lock_var.get())
        ed._kf_lock_var.set(False)
        ed._on_kf_lock_toggle()
        self.assertIs(ed.app.config["editor"]["lock_keyframes"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
