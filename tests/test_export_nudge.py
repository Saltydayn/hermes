"""Phase 4.4 - export-count thank-you toast.

Drives a REAL ExporterModule's _copy_done against a fake app (plain-dict config, never the
user's config.json) with shared.ui_helpers.show_toast patched to a recorder, so no real toast
window appears. Also checks show_toast itself is non-modal and teardown-safe. Self-skips where
Tk can't start.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk                       # noqa: E402

import shared.config as config_mod         # noqa: E402
import shared.ui_helpers as ui             # noqa: E402
import modules.exporter as ex              # noqa: E402
from shared import paths as shared_paths   # noqa: E402
from shared.bus import MessageBus          # noqa: E402


class _FakeApp:
    def __init__(self, config=None):
        self.bus = MessageBus()
        self.config = config if config is not None else copy.deepcopy(config_mod.DEFAULTS)
        self.paths = shared_paths
        self.root = None        # set by the test to the Tk root
        self.save_calls = 0

    def save_config(self):
        self.save_calls += 1


class TestExportNudge(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex_:
            raise unittest.SkipTest(f"Tk unavailable: {ex_}")
        cls.root.geometry("1000x700+80+80")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except tk.TclError:
            pass

    def _boot(self, config=None):
        app = _FakeApp(config)
        app.root = self.root
        xp = ex.ExporterModule(self.root, app)
        xp.pack(fill="both", expand=True)
        for _ in range(3):
            self.root.update()
        self.addCleanup(xp.destroy)
        self.addCleanup(xp.on_close)
        return xp, app

    def _patch_toast(self):
        fired = []
        original = ex.show_toast
        ex.show_toast = lambda *a, **k: fired.append((a, k))
        self.addCleanup(lambda: setattr(ex, "show_toast", original))
        return fired

    def test_increment_on_success_only(self):
        xp, app = self._boot()
        app.config["stats"]["full_exports"] = 0
        self._patch_toast()
        xp._copy_done(True, r"C:\tmp\out1.mp4", "")
        self.assertEqual(app.config["stats"]["full_exports"], 1)
        self.assertGreaterEqual(app.save_calls, 1)        # persisted
        xp._copy_done(False, r"C:\tmp\out2.mp4", "boom")  # failure path
        self.assertEqual(app.config["stats"]["full_exports"], 1)   # NOT incremented

    def test_toast_fires_exactly_once_at_ten(self):
        xp, app = self._boot()
        app.config["stats"]["full_exports"] = 0
        app.config["nudges"]["export_thankyou_shown"] = False
        fired = self._patch_toast()
        for i in range(1, 13):
            xp._copy_done(True, fr"C:\tmp\e{i}.mp4", "")
            self.assertEqual(len(fired), 0 if i < ex.NUDGE_AFTER else 1, f"after export {i}")
        self.assertEqual(app.config["stats"]["full_exports"], 12)
        self.assertTrue(app.config["nudges"]["export_thankyou_shown"])

    def test_no_toast_when_flag_already_set(self):
        cfg = copy.deepcopy(config_mod.DEFAULTS)
        cfg["stats"]["full_exports"] = 0
        cfg["nudges"]["export_thankyou_shown"] = True
        xp, app = self._boot(cfg)
        fired = self._patch_toast()
        for i in range(1, 16):
            xp._copy_done(True, fr"C:\tmp\f{i}.mp4", "")
        self.assertEqual(len(fired), 0)
        self.assertEqual(app.config["stats"]["full_exports"], 15)

    def test_copy_has_no_em_dash(self):
        self.assertNotIn("—", ex._NUDGE_TITLE)
        self.assertNotIn("—", ex._NUDGE_BODY)

    def test_editor_never_touches_counter(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "modules", "clip_editor.py")
        with open(path, encoding="utf-8") as f:
            self.assertNotIn("full_exports", f.read())


class TestShowToast(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex_:
            raise unittest.SkipTest(f"Tk unavailable: {ex_}")
        cls.root.geometry("900x600+60+60")
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except tk.TclError:
            pass

    def test_non_modal_and_closeable(self):
        top = ui.show_toast(self.root, "Title", "Body text", timeout_ms=50000)
        self.root.update()
        self.assertIsInstance(top, tk.Toplevel)
        self.assertIn(self.root.grab_current(), (None, ""))   # nothing grabbed -> non-modal
        top.destroy()                                         # close path
        self.root.update()                                    # pending after must not fire/raise

    def test_action_button_runs_and_closes(self):
        clicked = []
        top = ui.show_toast(self.root, "T", "B", timeout_ms=50000,
                            action_label="Support on Ko-fi", action=lambda: clicked.append(1))
        self.root.update()
        buttons = []

        def walk(w):
            if isinstance(w, tk.Button):
                buttons.append(w)
            for c in w.winfo_children():
                walk(c)
        walk(top)
        self.assertTrue(buttons, "action button should exist")
        buttons[0].invoke()
        self.root.update()
        self.assertEqual(clicked, [1])
        self.assertFalse(top.winfo_exists())                 # action closed the toast


if __name__ == "__main__":
    unittest.main()
