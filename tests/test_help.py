"""Phase 2.10.3 - help/onboarding pass.

Two parts: (1) the reusable shared Tooltip's lifecycle (delayed show, self-cleaning on
leave/destroy, no after() firing post-teardown); (2) the editor's section `?` HelpDialog
buttons (present where opted in, absent otherwise, openable without error). Needs Tk;
self-skips headless so CI never false-fails.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk    # noqa: E402

import modules.clip_editor as ce          # noqa: E402
from shared import paths as shared_paths   # noqa: E402
from shared.bus import MessageBus          # noqa: E402
from shared.ui_helpers import Tooltip      # noqa: E402
from tests import synth                    # noqa: E402


class _FakeApp:
    def __init__(self):
        self.bus = MessageBus()
        self.config = {"editor": {}}
        self.paths = shared_paths

    def save_config(self):
        pass


class TestTooltip(unittest.TestCase):
    """The shared hover Tooltip: shows after the delay, hides + cancels on leave, and never
    fires an after() after the widget is destroyed (the editor closes/reopens constantly)."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"Tk unavailable: {ex}")
        cls.root.geometry("400x300+100+100")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except tk.TclError:
            pass

    def _pump(self, ms):
        """Run the event loop ~ms milliseconds so a pending after() actually fires."""
        end = time.time() + ms / 1000.0
        while time.time() < end:
            self.root.update()

    def test_show_then_hide_lifecycle(self):
        w = tk.Label(self.root, text="hover me")
        w.pack()
        tt = Tooltip(w, "explanation", delay_ms=20)
        self.root.update()

        w.event_generate("<Enter>", x=5, y=5)
        self._pump(80)                              # past the 20ms show delay
        self.assertIsNotNone(tt._tip, "tooltip popup never appeared after the delay")
        self.assertTrue(tt._tip.winfo_exists())

        w.event_generate("<Leave>")
        self.root.update()
        self.assertIsNone(tt._tip, "tooltip not destroyed on leave")
        self.assertIsNone(tt._after_id, "pending show timer not cleared on leave")
        w.destroy()

    def test_no_after_leak_on_leave(self):
        w = tk.Label(self.root, text="x")
        w.pack()
        tt = Tooltip(w, "txt", delay_ms=500)
        self.root.update()
        w.event_generate("<Enter>", x=5, y=5)
        self.root.update()
        self.assertIsNotNone(tt._after_id, "enter did not schedule a show")
        w.event_generate("<Leave>")
        self.root.update()
        self.assertIsNone(tt._after_id)             # cancelled, not left dangling
        w.destroy()

    def test_destroy_with_pending_show_no_tclerror(self):
        w = tk.Label(self.root, text="y")
        w.pack()
        tt = Tooltip(w, "txt", delay_ms=500)
        self.root.update()
        w.event_generate("<Enter>", x=5, y=5)
        self.root.update()
        self.assertIsNotNone(tt._after_id)          # a show is pending
        w.destroy()                                  # <Destroy> must cancel it
        self.assertIsNone(tt._after_id, "destroy did not cancel the pending show")
        # Pump well past the original delay: a stale after() would raise a TclError here.
        self._pump(80)                               # completes ⇒ no post-destroy fire


class TestSectionHelpButtons(unittest.TestCase):
    """The editor's inspector sections expose a `?` HelpDialog where opted in (and only
    there); each button opens a dialog without error."""

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

    def test_help_buttons_present_and_open(self):
        ed = ce.EditorModule(self.root, _FakeApp())
        ed.pack(fill="both", expand=True)
        ed._load_waveform = lambda *a, **k: None
        self._settle()
        self.addCleanup(ed.on_close)

        # 5 inspector sections opt into help (5.5.2d split SEAM & BRANDING and dropped
        # the LAYOUT MODE section, net zero): SEAM, BRANDING, CONTENT BOXES, AUDIO,
        # RENDER OPTIONS; LAYOUTS does NOT → no button. The action bar's
        # Trim&Cut/Keyframes `?` buttons are separate (not section buttons, not in
        # _help_btns).
        self.assertEqual(len(ed._help_btns), 5,
                         f"expected 5 section help buttons, got {len(ed._help_btns)}")
        for b in ed._help_btns:
            self.assertEqual(str(b.cget("text")), "?")

        def toplevels():
            """Every Toplevel anywhere under root (a HelpDialog's master is the editor
            frame, so it's NOT a direct child of root)."""
            found, stack = [], list(self.root.winfo_children())
            while stack:
                w = stack.pop()
                if isinstance(w, tk.Toplevel):
                    found.append(w)
                stack.extend(w.winfo_children())
            return found

        before = set(map(str, toplevels()))
        ed._help_btns[0].invoke()                    # opens a HelpDialog (non-blocking)
        self.root.update()
        new = [w for w in toplevels() if str(w) not in before]
        self.assertTrue(new, "section ? button did not open a HelpDialog")
        for dlg in new:                              # close it cleanly
            dlg.grab_release()
            dlg.destroy()
        self.root.update()


if __name__ == "__main__":
    unittest.main(verbosity=2)
