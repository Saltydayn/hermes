"""Phase 3.2 - Export tab quick-share / preview / copy slots.

Boots a REAL ExporterModule against a lightweight fake app (a plain-dict config - NEVER the
user's config.json) and patches the OS hand-offs (webbrowser.open, the reveal subprocess, and
os.startfile) with recorders, so nothing actually opens a browser, Explorer, or a player. The
config-defaults check is pure (no Tk). Self-skips where Tk can't start.
"""

import copy
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk    # noqa: E402

import shared.config as config_mod          # noqa: E402
import modules.exporter as ex               # noqa: E402
from shared import paths as shared_paths    # noqa: E402
from shared.bus import MessageBus           # noqa: E402
from tests import synth                     # noqa: E402


class _FakeApp:
    """Just the AppContext surface ExporterModule touches: bus, a dict config, save_config,
    the real (read-only) paths module."""

    def __init__(self, config=None):
        self.bus = MessageBus()
        self.config = config if config is not None else copy.deepcopy(config_mod.DEFAULTS)
        self.paths = shared_paths
        self.save_calls = 0

    def save_config(self):
        self.save_calls += 1


class TestConfigDefaults(unittest.TestCase):
    """Pure: the new exporter block is present and deep-merges onto configs that lack it."""

    def test_defaults_present(self):
        merged = config_mod._deep_merge(config_mod.DEFAULTS, {})
        self.assertIn("exporter", merged)
        sb = merged["exporter"]["share_buttons"]
        self.assertEqual([b["label"] for b in sb], ["YouTube", "TikTok", "Instagram"])
        self.assertTrue(all("url" in b and b["url"] for b in sb))
        cs = merged["exporter"]["copy_slots"]
        self.assertIsInstance(cs, list)
        self.assertLessEqual(len(cs), 10)

    def test_deep_merge_adds_block_to_old_config(self):
        merged = config_mod._deep_merge(config_mod.DEFAULTS, {"theme": "dark"})
        self.assertIn("exporter", merged)
        self.assertEqual(len(merged["exporter"]["share_buttons"]), 3)


class TestExporterShare(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex_:
            raise unittest.SkipTest(f"Tk unavailable: {ex_}")
        cls.root.geometry("1100x700+80+80")
        try:
            cls.clip = synth.make_temp_clip()
            cls.export = synth.make_temp_clip()   # stands in for a finished export file
        except RuntimeError as ex_:
            cls.root.destroy()
            raise unittest.SkipTest(str(ex_))

    @classmethod
    def tearDownClass(cls):
        for p in (getattr(cls, "clip", None), getattr(cls, "export", None)):
            try:
                if p:
                    os.remove(p)
            except OSError:
                pass
        try:
            cls.root.destroy()
        except tk.TclError:
            pass

    def _settle(self):
        for _ in range(4):
            self.root.update()

    def _boot(self, app=None):
        app = app or _FakeApp()
        xp = ex.ExporterModule(self.root, app)
        xp.pack(fill="both", expand=True)
        self._settle()
        self.addCleanup(xp.destroy)
        self.addCleanup(xp.on_close)
        return xp, app

    # ── disabled-until-a-file ────────────────────────────────────────────────────────
    def test_share_disabled_until_file(self):
        xp, _ = self._boot()
        for btn in [xp._preview_btn, xp._reveal_btn, *xp._share_btns]:
            self.assertEqual(str(btn["state"]), "disabled")
        xp._set_current(self.clip, "raw_clip")
        for btn in [xp._preview_btn, xp._reveal_btn, *xp._share_btns]:
            self.assertEqual(str(btn["state"]), "normal")

    # ── the share seam fires both effects ────────────────────────────────────────────
    def test_share_opens_browser_and_reveals(self):
        xp, _ = self._boot()
        xp._last_export_path = self.export    # an existing file so reveal proceeds to Popen
        with mock.patch.object(ex.webbrowser, "open") as wopen, \
                mock.patch.object(ex.subprocess, "Popen") as popen:
            xp._share("https://x/upload")
            self.assertEqual(wopen.call_count, 1)
            self.assertEqual(wopen.call_args[0][0], "https://x/upload")
            self.assertEqual(popen.call_count, 1)
            xp._share("")                     # no url → reveal only, no browser
            self.assertEqual(wopen.call_count, 1)
            self.assertEqual(popen.call_count, 2)

    @staticmethod
    def _cmdline(popen):
        """The Popen command as one string (Windows reveal passes a STRING; the *nix
        branches pass a list) so substring asserts work on either."""
        a = popen.call_args[0][0]
        return a if isinstance(a, str) else " ".join(a)

    # ── reveal targets the export, else the destination folder, never the source ─────
    def test_reveal_targets_export(self):
        xp, _ = self._boot()
        xp._last_export_path = self.export    # an export exists → select it (in its folder)
        xp._current_path = self.clip
        with mock.patch.object(ex.subprocess, "Popen") as popen:
            xp._reveal_file()
            self.assertEqual(popen.call_count, 1)
            cmd = self._cmdline(popen)
            self.assertIn(os.path.normpath(self.export), cmd,
                          f"reveal did not target the export: {cmd}")
            if os.name == "nt":
                # The path must be QUOTED with /select, OUTSIDE the quotes (the spaced-path fix).
                self.assertIn(f'/select,"{os.path.normpath(self.export)}"', cmd)

    def test_reveal_opens_destination_when_no_export(self):
        """Before any export, reveal opens the chosen DESTINATION folder (not the source clip)."""
        import tempfile
        dest = tempfile.mkdtemp(prefix="sc_exp_dest_")
        app = _FakeApp()
        app.config["output_dirs"] = {"Shorts": dest}
        xp, _ = self._boot(app)
        xp._current_path = self.clip          # a source clip is loaded but NOT exported
        with mock.patch.object(ex.subprocess, "Popen") as popen:
            xp._reveal_file()
            self.assertEqual(popen.call_count, 1)
            cmd = self._cmdline(popen)
            self.assertIn(os.path.normpath(dest), cmd,
                          f"reveal did not open the destination: {cmd}")
            self.assertNotIn("/select,", cmd, "folder reveal should not use /select,")
        import shutil
        shutil.rmtree(dest, ignore_errors=True)

    def test_reveal_guards_when_no_export_and_no_destination(self):
        app = _FakeApp()
        app.config["output_dirs"] = {}        # no destination set
        xp, _ = self._boot(app)
        xp._current_path = self.clip
        with mock.patch.object(ex.subprocess, "Popen") as popen:
            xp._reveal_file()
            self.assertEqual(popen.call_count, 0)
            self.assertIn("destination", str(xp._status.cget("text")).lower())

    # ── preview precedence + guard ───────────────────────────────────────────────────
    def test_preview_precedence_and_guard(self):
        xp, _ = self._boot()
        with mock.patch.object(ex.os, "startfile", create=True) as sf:
            xp._last_export_path = self.export
            xp._current_path = self.clip
            xp._preview()
            self.assertEqual(sf.call_args[0][0], self.export)   # export beats current
            sf.reset_mock()
            xp._last_export_path = None
            xp._preview()
            self.assertEqual(sf.call_args[0][0], self.clip)     # falls back to current
            sf.reset_mock()
            xp._current_path = None
            xp._preview()
            self.assertEqual(sf.call_count, 0)                  # nothing → guarded
            self.assertIn("Nothing to preview", str(xp._status.cget("text")))

    # ── editable share buttons rebuild from config ───────────────────────────────────
    def test_share_buttons_edit_roundtrip(self):
        xp, app = self._boot()
        self.assertEqual(len(xp._share_btns), 3)
        app.config["exporter"]["share_buttons"] = [
            {"label": "YT", "url": "https://www.youtube.com/upload"},
            {"label": "TikTok", "url": "https://www.tiktok.com/upload"},
            {"label": "Instagram", "url": "https://www.instagram.com/"},
            {"label": "X", "url": "https://x.com/"},
        ]
        xp._build_share_buttons()
        self.assertEqual(len(xp._share_btns), 4)
        self.assertEqual(str(xp._share_btns[0].cget("text")), "YT")
        # Reset to defaults restores the three.
        app.config["exporter"]["share_buttons"] = [dict(b) for b in ex.DEFAULT_SHARE_BUTTONS]
        xp._build_share_buttons()
        self.assertEqual([str(b.cget("text")) for b in xp._share_btns],
                         ["YouTube", "TikTok", "Instagram"])

    def test_edit_dialog_opens_and_closes(self):
        """Smoke: the edit modal builds and tears down without error."""
        xp, _ = self._boot()
        xp._edit_share_buttons()
        self._settle()
        tops = [w for w in xp.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertTrue(tops, "edit dialog did not open")
        for t in tops:
            t.destroy()
        self._settle()

    # ── copy slots: copy + edit round-trip ───────────────────────────────────────────
    def test_copy_slot_roundtrip_and_clipboard(self):
        xp, app = self._boot()
        app.config["exporter"]["copy_slots"][0] = "#tag1 #tag2"
        xp._build_copy_slots()
        xp._copy_slot(0)
        self.assertEqual(self.root.clipboard_get(), "#tag1 #tag2")
        self.assertIn("Copied", str(xp._status.cget("text")))
        # Unlock, edit, save → config updated and the row relocks.
        xp._unlock_slot(1)
        self.assertEqual(xp._unlocked_slot, 1)
        xp._save_slot(1, "  fresh snippet  ")
        self.assertEqual(app.config["exporter"]["copy_slots"][1], "fresh snippet")
        self.assertIsNone(xp._unlocked_slot)

    def test_empty_slot_click_unlocks(self):
        xp, _ = self._boot()
        xp._copy_slot(3)                       # empty by default → opens for edit
        self.assertEqual(xp._unlocked_slot, 3)

    # ── no regression: the threaded export still publishes final_file ────────────────
    def test_export_still_publishes_and_sets_last(self):
        import tempfile
        dest = tempfile.mkdtemp(prefix="sc_exp_dest_")
        app = _FakeApp()
        app.config["output_dirs"] = {"Shorts": dest}
        xp, _ = self._boot(app)
        published = []
        app.bus.subscribe("final_file", lambda p: published.append(p))
        xp._set_current(self.clip, "raw_clip")
        xp._export()
        for _ in range(150):                   # let the copy thread + 50ms pump finish
            self.root.update()
            if not xp._exporting:
                break
            time.sleep(0.02)
        self.assertFalse(xp._exporting, "export never completed")
        self.assertEqual(len(published), 1, "final_file was not published")
        self.assertEqual(xp._last_export_path, published[0])
        self.assertTrue(os.path.exists(xp._last_export_path))
        self.assertTrue(xp._open_shown)
        import shutil
        shutil.rmtree(dest, ignore_errors=True)

    def test_pick_source_prefers_edited(self):
        xp, app = self._boot()
        app.bus.publish("raw_clip", "raw.mp4")
        app.bus.publish("edited_clip", "edited.mp4")
        self.assertEqual(xp._pick_source(), ("edited.mp4", "edited_clip"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
