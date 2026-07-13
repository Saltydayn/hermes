"""Phase 2.9.1 - per-clip edit persistence round-trip (the 2.8.2 invariant, made lasting).

Boots a REAL EditorModule against a lightweight fake app (a plain-dict config - NEVER the
user's config.json) and a synthetic clip, mutates the edit model, persists the block,
switches clips, switches back, and asserts the block restores exactly. Needs Tk (a display);
self-skips where Tk can't start so a headless CI run never false-fails.
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
    """Just the AppContext surface EditorModule touches: bus, a dict config, save_config,
    and the real (read-only) paths module. No disk writes to the user's config."""

    def __init__(self):
        self.bus = MessageBus()
        self.config = {"editor": {}}
        self.paths = shared_paths
        self.save_calls = 0

    def save_config(self):
        self.save_calls += 1   # in-memory only - assert nothing hit disk


class TestPerClipPersistence(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"Tk unavailable: {ex}")
        cls.root.geometry("1100x700+80+80")
        try:
            cls.clip_a = synth.make_temp_clip()
            cls.clip_b = synth.make_temp_clip()
        except RuntimeError as ex:
            cls.root.destroy()
            raise unittest.SkipTest(str(ex))

    @classmethod
    def tearDownClass(cls):
        for p in (getattr(cls, "clip_a", None), getattr(cls, "clip_b", None)):
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
        # Full update so the Notebook/canvas geometry resolves and load_clip's render
        # computes a real displayed-image size (boxes won't materialize at size 0).
        for _ in range(5):
            self.root.update()

    def test_block_roundtrip(self):
        app = _FakeApp()
        ed = ce.EditorModule(self.root, app)
        ed.pack(fill="both", expand=True)
        ed._load_waveform = lambda *a, **k: None   # no audio thread/ffmpeg in a unit test
        self._settle()
        self.addCleanup(ed.destroy)
        self.addCleanup(ed.on_close)

        ed.load_clip(self.clip_a)
        self._settle()
        self.assertGreater(ed._disp_w, 0, "source image never got a display size")
        self.assertIsNotNone(ed.game_box)

        # ── Mutate the full edit model: trim, gain, split, a moved box, two keyframes ──
        # 2.10.2a: trim is the single-segment case of `segments` (in_frame/out_frame derive).
        ed.segments = ed._normalize_segments([[5, 25]], ed.total_frames)
        ed.gain_var.set(-6.0)
        ed.split_var.set(63)
        ed.cam_box[0] += 10.0
        ed.keyframes = [
            {"frame": 0, "type": "fluid",
             "game_box": list(ed.game_box), "cam_box": list(ed.cam_box),
             "split_ratio": 63, "highlights": []},
            {"frame": 12, "type": "jump",
             "game_box": list(ed.game_box), "cam_box": list(ed.cam_box),
             "split_ratio": 63, "highlights": []},
        ]
        ed._save_clip_edits()   # persist the block into app.config (real path)

        key = ce.EditorModule._clip_key(self.clip_a)
        block = app.config["editor"]["clip_edits"][key]
        self.assertEqual(block["trim"], [5, 25])
        self.assertEqual(block["gain_db"], -6.0)
        self.assertEqual(block["layout"]["split_ratio"], 63)
        self.assertEqual(len(block["keyframes"]), 2)
        saved_cam_norm = block["keyframes"][0]["cam_box"]

        # ── Switch to a block-less clip, then back: the block must restore ──
        ed.load_clip(self.clip_b)
        self._settle()
        self.assertEqual(ed.keyframes, [])          # B has no saved edits → clean
        self.assertEqual([ed.in_frame, ed.out_frame], [0, ed.total_frames - 1])

        ed.load_clip(self.clip_a)
        self._settle()

        self.assertEqual([ed.in_frame, ed.out_frame], [5, 25], "trim not restored")
        self.assertAlmostEqual(float(ed.gain_var.get()), -6.0, places=6)
        self.assertEqual(int(ed.split_var.get()), 63, "split not restored")
        self.assertEqual(len(ed.keyframes), 2, "keyframes not restored")
        self.assertEqual([kf["type"] for kf in ed.keyframes], ["fluid", "jump"])

        # Re-snapshot and compare the normalized geometry to what was saved (same clip +
        # window ⇒ same display size ⇒ identical normalized boxes within float epsilon).
        re_snap = ed._snapshot_clip_edits()
        for a, b in zip(re_snap["keyframes"][0]["cam_box"], saved_cam_norm):
            self.assertAlmostEqual(a, b, places=6)

        self.assertEqual(app.save_calls, app.save_calls)  # sanity: save_config was reachable


class TestSinglePanelMode(unittest.TestCase):
    """2.10.1: single-panel mode rides in the per-clip block (the layout dict), restores
    on reload, and a split→single→split round-trip leaves the (hidden) cam box intact."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"Tk unavailable: {ex}")
        cls.root.geometry("1100x700+80+80")
        try:
            cls.clip_a = synth.make_temp_clip()
            cls.clip_b = synth.make_temp_clip()
        except RuntimeError as ex:
            cls.root.destroy()
            raise unittest.SkipTest(str(ex))

    @classmethod
    def tearDownClass(cls):
        for p in (getattr(cls, "clip_a", None), getattr(cls, "clip_b", None)):
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
        for _ in range(5):
            self.root.update()

    def _boot(self, app):
        ed = ce.EditorModule(self.root, app)
        ed.pack(fill="both", expand=True)
        ed._load_waveform = lambda *a, **k: None
        self._settle()
        # destroy MUST also run, not just on_close - this class's root is shared across
        # every test method, and an undestroyed instance stays packed (fill/expand=True),
        # stealing canvas height from the next test's instance (5.5.4c-correction regression: a taller
        # toolbar was enough to push the next instance's source canvas to zero height).
        self.addCleanup(ed.destroy)
        self.addCleanup(ed.on_close)
        return ed

    @staticmethod
    def _scale_from(ed):
        """The split scale's `from` (a tk.Scale stores it as a float string)."""
        return int(float(str(ed._split_scale.cget("from"))))

    @staticmethod
    def _scale_to(ed):
        return int(float(str(ed._split_scale.cget("to"))))

    def test_persistence_roundtrip_and_slider_range(self):
        app = _FakeApp()
        ed = self._boot(app)
        ed.load_clip(self.clip_a)
        self._settle()
        self.assertFalse(ed.single_panel.get())                  # default = split
        self.assertEqual(self._scale_from(ed), 30)

        # Enable single-panel through the real toggle path, persist the block.
        ed.single_panel.set(True)
        ed._on_mode_change()
        self.assertEqual(self._scale_from(ed), 0)   # 0..100 line range
        self.assertEqual(self._scale_to(ed), 100)
        ed._save_clip_edits()
        key = ce.EditorModule._clip_key(self.clip_a)
        self.assertTrue(app.config["editor"]["clip_edits"][key]["layout"]["single_panel"])

        # Switch away (clean clip → split), then back: single-panel restores + 0..100 range.
        ed.load_clip(self.clip_b)
        self._settle()
        self.assertFalse(ed.single_panel.get(), "clip B should default to split")
        self.assertEqual(self._scale_from(ed), 30)

        ed.load_clip(self.clip_a)
        self._settle()
        self.assertTrue(ed.single_panel.get(), "single-panel mode not restored")
        self.assertEqual(self._scale_from(ed), 0)

    def test_preset_strips_to_split(self):
        """A PRESET slot is layout-only; single_panel rides in the layout dict, so it is
        carried by a preset too - but applying a split preset (single_panel absent/False)
        restores split mode with the 30..80 range."""
        app = _FakeApp()
        ed = self._boot(app)
        ed.load_clip(self.clip_a)
        self._settle()
        ed.single_panel.set(True)
        ed._on_mode_change()
        # Apply a layout dict WITHOUT single_panel (an old/split preset) → split restored.
        ed._apply_layout({"split_ratio": 50})
        self._settle()
        self.assertFalse(ed.single_panel.get(), "missing single_panel must default split")
        self.assertEqual(self._scale_from(ed), 30)

    def test_cam_box_survives_round_trip(self):
        app = _FakeApp()
        ed = self._boot(app)
        ed.load_clip(self.clip_a)
        self._settle()
        self.assertIsNotNone(ed.cam_box)
        cam_before = list(ed.cam_box)

        ed.single_panel.set(True)
        ed._on_mode_change()
        self._settle()
        ed.single_panel.set(False)
        ed._on_mode_change()
        self._settle()
        # The split-mode aspect re-lock recomputes cam height from its width; the width +
        # position must be unchanged, height within the aspect re-lock it already applies.
        for a, b in zip(ed.cam_box, cam_before):
            self.assertAlmostEqual(a, b, places=3, msg=f"cam_box drifted: {ed.cam_box} vs {cam_before}")

    def test_seam_drag_range_follows_mode(self):
        """Dragging the preview seam: split mode keeps the 30..80 panel-division limits;
        single-panel lets the decorative line slide the full 0..100% (matching the slider).
        Extremes only (the seam's int(pct*100) truncation is ±1 at fractional positions)."""
        import types
        ed = self._boot(_FakeApp())
        ed.load_clip(self.clip_a)
        self._settle()

        def drag(frac):
            ed.update_live_preview()        # populate a real _pv_h/_pv_off_y (bypass debounce)
            y = ed._pv_off_y + round(ed._pv_h * frac)
            ed._pv_seam_drag = True
            ed._on_preview_drag(types.SimpleNamespace(x=ed._pv_off_x + 5, y=y))
            ed._pv_seam_drag = False
            return int(ed.split_var.get())

        # Split: past-limit drags clamp to the 30 / 80 panel limits.
        self.assertEqual(drag(-0.2), 30, "split top did not clamp to 30")
        self.assertEqual(drag(1.2), 80, "split bottom did not clamp to 80")

        # Single-panel: the full 0..100 range is reachable.
        ed.single_panel.set(True)
        ed._on_mode_change()
        self._settle()
        self.assertEqual(drag(0.0), 0, "single-panel top did not reach 0")
        self.assertEqual(drag(1.0), 100, "single-panel bottom did not reach 100")

        # Park the line past the split range, switch off → it snaps back into 30..80.
        drag(0.97)
        ed.single_panel.set(False)
        ed._on_mode_change()
        self._settle()
        self.assertTrue(30 <= int(ed.split_var.get()) <= 80,
                        f"switch-off did not snap into 30..80: {int(ed.split_var.get())}")


class TestSegmentsPersistence(unittest.TestCase):
    """2.10.2a: the multi-cut `segments` list round-trips through the per-clip block,
    falls back from the legacy `trim` field, derives in/out bounds, and never leaks into
    layout presets. Uses a 50-frame clip so [[2,5],[20,40]] is fully in range."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"Tk unavailable: {ex}")
        cls.root.geometry("1100x700+80+80")
        try:
            cls.clip_a = synth.make_temp_clip(n=50)
            cls.clip_b = synth.make_temp_clip(n=50)
        except RuntimeError as ex:
            cls.root.destroy()
            raise unittest.SkipTest(str(ex))

    @classmethod
    def tearDownClass(cls):
        for p in (getattr(cls, "clip_a", None), getattr(cls, "clip_b", None)):
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
        for _ in range(5):
            self.root.update()

    def _boot(self, app):
        ed = ce.EditorModule(self.root, app)
        ed.pack(fill="both", expand=True)
        ed._load_waveform = lambda *a, **k: None
        self._settle()
        # Destroy the frame too (not just on_close) so stacked modules don't accumulate on
        # the shared root and starve a later module's canvas of a real display size. LIFO:
        # on_close runs first, then destroy.
        self.addCleanup(ed.destroy)
        self.addCleanup(ed.on_close)
        return ed

    def test_segments_roundtrip(self):
        app = _FakeApp()
        ed = self._boot(app)
        ed.load_clip(self.clip_a)
        self._settle()
        self.assertGreaterEqual(ed.total_frames, 41)

        ed.segments = ed._normalize_segments([[2, 5], [20, 40]], ed.total_frames)
        ed._save_clip_edits()
        key = ce.EditorModule._clip_key(self.clip_a)
        block = app.config["editor"]["clip_edits"][key]
        self.assertEqual(block["segments"], [[2, 5, 0.0], [20, 40, 0.0]])
        self.assertEqual(block["trim"], [2, 40])              # breadcrumb = outer bounds

        ed.load_clip(self.clip_b)                              # clean clip → whole-clip
        self._settle()
        self.assertEqual(ed.segments, [[0, ed.total_frames - 1, 0.0]])

        ed.load_clip(self.clip_a)                              # back → restored exactly
        self._settle()
        self.assertEqual(ed.segments, [[2, 5, 0.0], [20, 40, 0.0]],
                         "segments not restored")

    def test_legacy_trim_fallback(self):
        app = _FakeApp()
        ed = self._boot(app)
        ed.load_clip(self.clip_a)
        self._settle()
        # Persist a block, then strip segments + leave only a legacy trim (an old block).
        ed.segments = [[3, 17]]
        ed._save_clip_edits()
        key = ce.EditorModule._clip_key(self.clip_a)
        block = app.config["editor"]["clip_edits"][key]
        block.pop("segments", None)
        block["trim"] = [3, 17]

        ed.load_clip(self.clip_b)
        self._settle()
        ed.load_clip(self.clip_a)
        self._settle()
        self.assertEqual(ed.segments, [[3, 17, 0.0]],
                         "legacy trim did not seed one segment")

    def test_bounds_derive(self):
        # (The trim-label readout was removed with the TRIM inspector section in the
        # 5.5.1a correction round - the timeline shading is the readout now.)
        app = _FakeApp()
        ed = self._boot(app)
        ed.load_clip(self.clip_a)
        self._settle()
        ed.segments = ed._normalize_segments([[2, 5], [20, 40]], ed.total_frames)
        self.assertEqual(ed.in_frame, 2)
        self.assertEqual(ed.out_frame, 40)

    def test_presets_carry_no_segments(self):
        app = _FakeApp()
        ed = self._boot(app)
        ed.load_clip(self.clip_a)
        self._settle()
        ed.segments = ed._normalize_segments([[2, 5], [20, 40]], ed.total_frames)
        ed._save_slot(0)                                      # presets are layout-only
        preset = app.config["editor"]["presets"][0]
        self.assertNotIn("segments", preset)
        self.assertNotIn("trim", preset)


class TestSharpenPersistence(unittest.TestCase):
    """3.1: the render-only sharpen toggle + strength ride in the per-clip block like gain.
    A fresh clip loads off / 1.0; a saved block restores exactly; a block missing the keys
    restores to the off / 1.0 defaults (deep-merge safety). Boots a real EditorModule."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"Tk unavailable: {ex}")
        cls.root.geometry("1100x700+80+80")
        try:
            cls.clip_a = synth.make_temp_clip()
            cls.clip_b = synth.make_temp_clip()
        except RuntimeError as ex:
            cls.root.destroy()
            raise unittest.SkipTest(str(ex))

    @classmethod
    def tearDownClass(cls):
        for p in (getattr(cls, "clip_a", None), getattr(cls, "clip_b", None)):
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
        for _ in range(5):
            self.root.update()

    def _boot(self, app):
        ed = ce.EditorModule(self.root, app)
        ed.pack(fill="both", expand=True)
        ed._load_waveform = lambda *a, **k: None
        self._settle()
        self.addCleanup(ed.destroy)
        self.addCleanup(ed.on_close)
        return ed

    def test_default_and_roundtrip(self):
        app = _FakeApp()
        ed = self._boot(app)
        ed.load_clip(self.clip_a)
        self._settle()
        self.assertFalse(ed.sharpen_on.get())                    # fresh clip: off
        self.assertAlmostEqual(float(ed.sharpen_strength.get()), 1.0, places=6)

        ed.sharpen_on.set(True)
        ed.sharpen_strength.set(1.25)
        ed._save_clip_edits()
        key = ce.EditorModule._clip_key(self.clip_a)
        block = app.config["editor"]["clip_edits"][key]
        self.assertTrue(block["sharpen"])
        self.assertAlmostEqual(block["sharpen_strength"], 1.25, places=6)

        ed.load_clip(self.clip_b)                                # clean clip → reset to off
        self._settle()
        self.assertFalse(ed.sharpen_on.get())
        self.assertAlmostEqual(float(ed.sharpen_strength.get()), 1.0, places=6)

        ed.load_clip(self.clip_a)                                # back → restored exactly
        self._settle()
        self.assertTrue(ed.sharpen_on.get(), "sharpen toggle not restored")
        self.assertAlmostEqual(float(ed.sharpen_strength.get()), 1.25, places=6)

    def test_missing_keys_restore_defaults(self):
        """A pre-3.1 block (no sharpen keys) restored via _apply_clip_edits must default the
        vars off / 1.0, even when they were left non-default beforehand."""
        app = _FakeApp()
        ed = self._boot(app)
        ed.load_clip(self.clip_a)
        self._settle()
        ed.sharpen_on.set(True)
        ed.sharpen_strength.set(2.0)
        block = ed._snapshot_clip_edits()
        block.pop("sharpen", None)                               # an old block predating 3.1
        block.pop("sharpen_strength", None)
        ed._apply_clip_edits(block)
        self.assertFalse(ed.sharpen_on.get(), "missing key did not default off")
        self.assertAlmostEqual(float(ed.sharpen_strength.get()), 1.0, places=6)


class TestCutEditingUI(unittest.TestCase):
    """2.10.2d: the timeline cut UI - mark+remove a range, draw the removed band, restore via
    right-click, persist, and refuse to remove everything. Boots a real EditorModule."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"Tk unavailable: {ex}")
        cls.root.geometry("1100x700+80+80")
        try:
            cls.clip_a = synth.make_temp_clip(n=50)
            cls.clip_b = synth.make_temp_clip(n=50)
        except RuntimeError as ex:
            cls.root.destroy()
            raise unittest.SkipTest(str(ex))

    @classmethod
    def tearDownClass(cls):
        for p in (getattr(cls, "clip_a", None), getattr(cls, "clip_b", None)):
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

    @staticmethod
    def _cut(ed, a, b):
        """One-button cut: mark the start at `a`, move to `b`, press again to remove."""
        ed.frame_idx = a
        ed._toggle_cut()                               # marks the start
        ed.frame_idx = b
        ed._toggle_cut()                               # removes [a, b]

    def test_cut_via_controls(self):
        ed = self._boot()
        total = ed.total_frames
        before = ce.EditorModule._kept_total(ed.segments)
        self._cut(ed, 5, 10)
        self.assertEqual(ed.segments, [[0, 4, 0.0], [11, total - 1, 0.0]])
        self.assertEqual(ce.EditorModule._kept_total(ed.segments), before - 6)
        self.assertIsNone(ed._cut_start)               # pending cleared after the cut

    def test_cut_cancel_on_same_frame(self):
        """Pressing Cut twice without moving cancels (no change to the kept set)."""
        ed = self._boot()
        before = [list(s) for s in ed.segments]
        ed.frame_idx = 10
        ed._toggle_cut()                               # mark start
        self.assertEqual(ed._cut_start, 10)
        ed._toggle_cut()                               # same frame → cancel
        self.assertIsNone(ed._cut_start)
        self.assertEqual(ed.segments, before)

    def test_removed_region_drawn(self):
        ed = self._boot()
        if ed._timeline.winfo_width() < 100:
            self.skipTest("timeline canvas not laid out")
        self._cut(ed, 5, 10)
        self._settle()
        self.assertEqual(len(ed._timeline.find_withtag("cut_gap")), 1)  # one gap → one band
        self._cut(ed, 20, 25)
        self._settle()
        self.assertEqual(len(ed._timeline.find_withtag("cut_gap")), 2)  # two gaps

    def test_restore_via_right_click(self):
        import types
        ed = self._boot()
        if ed._timeline.winfo_width() < 100:
            self.skipTest("timeline canvas not laid out")
        self._cut(ed, 5, 10)
        self._settle()
        x = ed._tl_x_of(7)                              # inside the removed gap (5..10)
        ed._on_tl_rpress(types.SimpleNamespace(x=x, y=ce.TL_TRACK_H + 10))  # lower band
        self.assertEqual(ed.segments, [[0, ed.total_frames - 1, 0.0]])
        self._settle()
        self.assertEqual(len(ed._timeline.find_withtag("cut_gap")), 0)

    def test_persistence_roundtrip(self):
        app = _FakeApp()
        ed = self._boot(app)
        self._cut(ed, 5, 10)
        cut_segs = [list(s) for s in ed.segments]
        ed._save_clip_edits()
        ed.load_clip(self.clip_b)
        self._settle()
        ed.load_clip(self.clip_a)
        self._settle()
        self.assertEqual(ed.segments, cut_segs, "cut not restored from the per-clip block")

    def test_refuse_remove_everything(self):
        ed = self._boot()
        said = []
        ed._say = lambda msg, *a, **k: said.append(msg)
        before = [list(s) for s in ed.segments]
        self._cut(ed, 0, ed.total_frames - 1)          # try to remove the whole clip
        self.assertEqual(ed.segments, before, "removing everything was not refused")
        self.assertTrue(any("empty the clip" in m for m in said),
                        f"no refusal message: {said}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
