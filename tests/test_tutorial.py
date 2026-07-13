"""Phase 5.5.4a: onboarding tutorial engine + Home wiring.

Covers: the config schema, TutorialIntro's two-button modal, TutorialWalkthrough's
step navigation / ring geometry / reposition-on-move / clean teardown (Escape and
Skip), TutorialReplayButton's always-present placement, and HomeModule's own
maybe_show_tutorial hook (first-visit intro, replay button, re-enable resetting
"seen"). Needs Tk; self-skips headless so CI never false-fails.
"""

import copy
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk    # noqa: E402

import shared.config as config_mod         # noqa: E402
from shared import paths as shared_paths    # noqa: E402
from shared.bus import MessageBus           # noqa: E402
from shared.ui_helpers import (             # noqa: E402
    ScrollableFrame, TutorialIntro, TutorialReplayButton, TutorialWalkthrough,
)


class TestTutorialConfigSchema(unittest.TestCase):
    """Pure - no Tk needed."""

    def test_defaults_shape(self):
        t = config_mod.DEFAULTS["tutorial"]
        self.assertFalse(t["asked"])
        self.assertTrue(t["enabled"])
        for key in ("home", "importer", "editor", "exporter", "about"):
            self.assertIn(key, t["seen"])
            self.assertFalse(t["seen"][key])

    def test_seen_keys_match_registry(self):
        from shared import registry
        self.assertEqual(set(config_mod.DEFAULTS["tutorial"]["seen"]),
                         set(registry.MODULE_REGISTRY))

    def test_load_config_merges_forward(self):
        # A config.json saved before this feature existed (no "tutorial" key at all)
        # must still merge in the full default block.
        merged = config_mod._deep_merge(config_mod.DEFAULTS, {"theme": "dark"})
        self.assertIn("tutorial", merged)
        self.assertTrue(merged["tutorial"]["enabled"])


class _TkTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"no Tk display: {ex}")
        cls.root.geometry("900x600+80+80")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except tk.TclError:
            pass

    def _pump(self, n=5):
        for _ in range(n):
            self.root.update()

    @staticmethod
    def _toplevels(root):
        found, stack = [], list(root.winfo_children())
        while stack:
            w = stack.pop()
            if isinstance(w, tk.Toplevel):
                found.append(w)
            stack.extend(w.winfo_children())
        return found

    def _skip_walkthrough(self):
        """Close an open TutorialWalkthrough via its own Skip button so its _cleanup()
        runs and cancels the 5.5.4b live-reposition tick - raw-destroying the Toplevels
        instead would leave that after() job dangling and fire post-test."""
        for top in self._toplevels(self.root):
            stack = list(top.winfo_children())
            while stack:
                w = stack.pop()
                if isinstance(w, tk.Button) and str(w.cget("text")) == "Skip":
                    w.invoke()
                    return
                stack.extend(w.winfo_children())


class TestTutorialIntro(_TkTestBase):
    def test_skip_closes_and_calls_on_skip(self):
        calls = {"skip": 0, "show": 0}
        dlg = TutorialIntro(self.root, "Welcome", "Body text.",
                            on_show_me=lambda: calls.__setitem__("show", calls["show"] + 1),
                            on_skip=lambda: calls.__setitem__("skip", calls["skip"] + 1))
        self._pump()
        # Find the "Skip" button and invoke it.
        skip_btn = None
        stack = list(dlg.winfo_children())
        while stack:
            w = stack.pop()
            if isinstance(w, tk.Button) and str(w.cget("text")) == "Skip":
                skip_btn = w
                break
            stack.extend(w.winfo_children())
        self.assertIsNotNone(skip_btn, "Skip button not found")
        skip_btn.invoke()
        self._pump()
        self.assertEqual(calls, {"skip": 1, "show": 0})

    def test_show_me_around_closes_and_calls_on_show(self):
        calls = {"skip": 0, "show": 0}
        dlg = TutorialIntro(self.root, "Welcome", "Body text.",
                            on_show_me=lambda: calls.__setitem__("show", calls["show"] + 1),
                            on_skip=lambda: calls.__setitem__("skip", calls["skip"] + 1))
        self._pump()
        show_btn = None
        stack = list(dlg.winfo_children())
        while stack:
            w = stack.pop()
            if isinstance(w, tk.Button) and str(w.cget("text")) == "Show me around":
                show_btn = w
                break
            stack.extend(w.winfo_children())
        self.assertIsNotNone(show_btn, "Show me around button not found")
        show_btn.invoke()
        self._pump()
        self.assertEqual(calls, {"skip": 0, "show": 1})

    def test_escape_counts_as_skip(self):
        calls = {"skip": 0}
        dlg = TutorialIntro(self.root, "Welcome", "Body.",
                            on_skip=lambda: calls.__setitem__("skip", 1))
        self._pump()
        dlg.focus_force()   # a real modal dialog has WM focus; event_generate needs it too
        self._pump()
        dlg.event_generate("<Escape>")
        self._pump()
        self.assertEqual(calls["skip"], 1)


class TestTutorialWalkthrough(_TkTestBase):
    def _steps(self, target_widget):
        return [
            {"target": lambda: target_widget, "title": "Step one", "body": "First."},
            {"target": lambda: target_widget, "title": "Step two", "body": "Second."},
            {"target": lambda: None, "title": "Step three (no target)", "body": "Third."},
        ]

    def test_navigation_and_labels(self):
        w = tk.Label(self.root, text="target")
        w.place(x=100, y=100, width=80, height=30)
        self._pump()
        tour = TutorialWalkthrough(self.root, self._steps(w))
        tour.start()
        self._pump()
        self.assertEqual(tour.idx, 0)
        self.assertEqual(len(tour._ring), 4)   # step 1 has a real target -> ring drawn
        self.assertIsNotNone(tour._card)

        tour._next()
        self._pump()
        self.assertEqual(tour.idx, 1)

        tour._next()
        self._pump()
        self.assertEqual(tour.idx, 2)
        self.assertEqual(tour._ring, [], "step 3's target() returns None -> no ring")
        self.assertIsNotNone(tour._card, "card still shows, centered, with no ring")

        tour._back()
        self._pump()
        self.assertEqual(tour.idx, 1)

        w.destroy()
        tour._cleanup()

    def test_ring_surrounds_without_covering_target(self):
        w = tk.Label(self.root, text="target")
        w.place(x=150, y=150, width=120, height=40)
        self._pump()
        tour = TutorialWalkthrough(self.root, self._steps(w))
        tour.start()
        self._pump()

        tx, ty = w.winfo_rootx(), w.winfo_rooty()
        tw, th = w.winfo_width(), w.winfo_height()
        target_box = (tx, ty, tx + tw, ty + th)

        def overlaps(a, b):
            ax0, ay0, ax1, ay1 = a
            bx0, by0, bx1, by1 = b
            return ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1

        for strip in tour._ring:
            sx, sy = strip.winfo_x(), strip.winfo_y()
            sw, sh = strip.winfo_width(), strip.winfo_height()
            strip_box = (sx, sy, sx + sw, sy + sh)
            self.assertFalse(overlaps(strip_box, target_box),
                             f"ring strip {strip_box} overlaps target {target_box}")

        w.destroy()
        tour._cleanup()

    def test_reposition_on_root_configure(self):
        w = tk.Label(self.root, text="target")
        w.place(x=100, y=100, width=80, height=30)
        self._pump()
        tour = TutorialWalkthrough(self.root, self._steps(w))
        tour.start()
        self._pump()
        before = [(t.winfo_x(), t.winfo_y()) for t in tour._ring]

        # Move the widget and simulate the window resizing/moving (a real move is hard
        # to script headlessly; call the handler directly the way the bound <Configure>
        # would, since the position math itself is what's under test).
        w.place_configure(x=300, y=250)
        self._pump()
        tour._on_root_configure(None)
        self._pump()
        after = [(t.winfo_x(), t.winfo_y()) for t in tour._ring]
        self.assertNotEqual(before, after, "ring did not follow the target after reposition")

        w.destroy()
        tour._cleanup()

    def test_skip_button_tears_down_cleanly(self):
        w = tk.Label(self.root, text="target")
        w.place(x=100, y=100, width=80, height=30)
        self._pump()
        before = set(map(str, self._toplevels(self.root)))
        tour = TutorialWalkthrough(self.root, self._steps(w))
        tour.start()
        self._pump()
        self.assertTrue(tour._ring)
        self.assertIsNotNone(tour._card)

        tour._cleanup()
        self._pump()
        self.assertEqual(tour._ring, [])
        self.assertIsNone(tour._card)
        after = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before, after, "cleanup left orphaned Toplevels")
        w.destroy()

    def test_escape_tears_down_cleanly(self):
        w = tk.Label(self.root, text="target")
        w.place(x=100, y=100, width=80, height=30)
        self._pump()
        before = set(map(str, self._toplevels(self.root)))
        tour = TutorialWalkthrough(self.root, self._steps(w))
        tour.start()
        self._pump()

        tour._on_escape()
        self._pump()
        self.assertEqual(tour._ring, [])
        self.assertIsNone(tour._card)
        after = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before, after, "Escape left orphaned Toplevels")
        w.destroy()

    def test_last_step_next_button_says_done(self):
        w = tk.Label(self.root, text="target")
        w.place(x=100, y=100, width=80, height=30)
        self._pump()
        tour = TutorialWalkthrough(self.root, self._steps(w))
        tour.start()
        tour._next()
        tour._next()
        self._pump()

        def find_button(text):
            stack = list(tour._card.winfo_children())
            while stack:
                x = stack.pop()
                if isinstance(x, tk.Button) and str(x.cget("text")) == text:
                    return x
                stack.extend(x.winfo_children())
            return None

        self.assertIsNotNone(find_button("Done"))
        self.assertIsNone(find_button("Next"))
        w.destroy()
        tour._cleanup()


class TestTutorialReplayButton(_TkTestBase):
    def test_present_and_clicking_invokes_callback(self):
        frame = tk.Frame(self.root)
        frame.pack(fill="both", expand=True)
        self._pump()
        calls = []
        outer = TutorialReplayButton(frame, lambda: calls.append(1), "Replay the tour.")
        self._pump()
        self.assertTrue(outer.winfo_ismapped())

        btn = None
        for w in outer.winfo_children():
            if isinstance(w, tk.Button):
                btn = w
                break
        self.assertIsNotNone(btn)
        btn.invoke()
        self.assertEqual(calls, [1])
        frame.destroy()


class _FakePaths:
    def __init__(self, userdata, assets):
        self.USER_DATA_DIR = userdata
        self._assets = assets

    def get_path(self, name, create=True):
        return self._assets if name == "assets" else self.USER_DATA_DIR


class _FakeApp:
    def __init__(self, root):
        self.root = root
        self.bus = MessageBus()   # importer/exporter touch app.bus at build/visibility time
        self.config = copy.deepcopy(config_mod.DEFAULTS)
        self.config["supporters_url"] = ""
        # Pre-answer the one-shot first-launch prompts so _build() never schedules the
        # real after(1200, ...) job. These tests drive _ask_tutorial_prompt /
        # maybe_show_tutorial / _start_walkthrough directly and don't want a real
        # 1200ms timer outliving a destroyed instance mid-suite (a pre-existing
        # dangling-after gotcha shared with _ask_keyboard_layout, not new to this spec).
        self.config["keyboard_layout_asked"] = True
        self.config["tutorial"]["asked"] = True
        self.paths = _FakePaths(tempfile.mkdtemp(),
                                shared_paths.get_path("assets", create=False))
        self.restart = None

    def save_config(self):
        pass


class TestHomeTutorialWiring(_TkTestBase):
    def setUp(self):
        import modules.home as home_mod
        self.home_mod = home_mod
        self.app = _FakeApp(self.root)
        self.home = home_mod.HomeModule(self.root, self.app)
        self.home.pack(fill="both", expand=True)
        self._pump()

    def tearDown(self):
        try:
            self.home.destroy()
        except tk.TclError:
            pass
        self._pump()

    def test_maybe_show_tutorial_noop_before_asked(self):
        self.app.config["tutorial"]["asked"] = False
        before = set(map(str, self._toplevels(self.root)))
        self.home.maybe_show_tutorial()
        self._pump()
        after = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before, after, "intro must not show before the first-launch ask")

    def test_maybe_show_tutorial_shows_intro_once(self):
        self.app.config["tutorial"]["asked"] = True
        self.app.config["tutorial"]["enabled"] = True
        self.app.config["tutorial"]["seen"]["home"] = False
        before = set(map(str, self._toplevels(self.root)))
        self.home.maybe_show_tutorial()
        self._pump()
        after = [w for w in self._toplevels(self.root) if str(w) not in before]
        self.assertTrue(after, "first visit did not show the intro popup")
        self.assertTrue(self.app.config["tutorial"]["seen"]["home"],
                        "seen.home must be marked True as soon as the intro is shown")
        for w in after:
            w.destroy()
        self._pump()

        # Second call (already seen): no new popup.
        before2 = set(map(str, self._toplevels(self.root)))
        self.home.maybe_show_tutorial()
        self._pump()
        after2 = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before2, after2, "intro reappeared on a second visit")

    def test_replay_button_opens_walkthrough_directly(self):
        self.app.config["tutorial"]["seen"]["home"] = True   # already seen, no intro
        before = set(map(str, self._toplevels(self.root)))
        self.home._start_walkthrough()
        self._pump()
        after = [w for w in self._toplevels(self.root) if str(w) not in before]
        self.assertTrue(after, "replay did not open the walkthrough")
        self._skip_walkthrough()
        self._pump()

    def test_toggle_resets_seen(self):
        tcfg = self.app.config["tutorial"]
        tcfg["enabled"] = False
        for k in tcfg["seen"]:
            tcfg["seen"][k] = True
        self.home._toggle_tutorials()   # OFF -> ON
        self.assertTrue(tcfg["enabled"])
        self.assertTrue(all(v is False for v in tcfg["seen"].values()),
                        "enabling tutorials must reset every module's seen state")

    def test_toggle_off_does_not_touch_seen(self):
        tcfg = self.app.config["tutorial"]
        tcfg["enabled"] = True
        tcfg["seen"]["home"] = True
        self.home._toggle_tutorials()   # ON -> OFF
        self.assertFalse(tcfg["enabled"])
        self.assertTrue(tcfg["seen"]["home"], "turning off must not touch seen")


class TestImporterTutorialWiring(_TkTestBase):
    """5.5.4b: same shape as TestHomeTutorialWiring, against the real ImporterModule."""

    def setUp(self):
        import modules.importer as importer_mod
        self.app = _FakeApp(self.root)
        self.mod = importer_mod.ImporterModule(self.root, self.app)
        self.mod.pack(fill="both", expand=True)
        self._pump()

    def tearDown(self):
        try:
            self.mod.on_close()
        except Exception:
            pass
        try:
            self.mod.destroy()
        except tk.TclError:
            pass
        self._pump()

    def test_maybe_show_tutorial_noop_before_asked(self):
        self.app.config["tutorial"]["asked"] = False
        before = set(map(str, self._toplevels(self.root)))
        self.mod.maybe_show_tutorial()
        self._pump()
        after = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before, after, "intro must not show before the first-launch ask")

    def test_maybe_show_tutorial_shows_intro_once(self):
        self.app.config["tutorial"]["enabled"] = True
        self.app.config["tutorial"]["seen"]["importer"] = False
        before = set(map(str, self._toplevels(self.root)))
        self.mod.maybe_show_tutorial()
        self._pump()
        after = [w for w in self._toplevels(self.root) if str(w) not in before]
        self.assertTrue(after, "first visit did not show the intro popup")
        self.assertTrue(self.app.config["tutorial"]["seen"]["importer"])
        for w in after:
            w.destroy()
        self._pump()

        before2 = set(map(str, self._toplevels(self.root)))
        self.mod.maybe_show_tutorial()
        self._pump()
        after2 = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before2, after2, "intro reappeared on a second visit")

    def test_replay_button_opens_walkthrough_directly(self):
        self.app.config["tutorial"]["seen"]["importer"] = True
        before = set(map(str, self._toplevels(self.root)))
        self.mod._start_walkthrough()
        self._pump()
        after = [w for w in self._toplevels(self.root) if str(w) not in before]
        self.assertTrue(after, "replay did not open the walkthrough")
        self._skip_walkthrough()
        self._pump()


class TestExporterTutorialWiring(_TkTestBase):
    """5.5.4b: same shape as TestHomeTutorialWiring, against the real ExporterModule."""

    def setUp(self):
        import modules.exporter as exporter_mod
        self.app = _FakeApp(self.root)
        self.mod = exporter_mod.ExporterModule(self.root, self.app)
        self.mod.pack(fill="both", expand=True)
        self._pump()

    def tearDown(self):
        try:
            self.mod.on_close()
        except Exception:
            pass
        try:
            self.mod.destroy()
        except tk.TclError:
            pass
        self._pump()

    def test_maybe_show_tutorial_noop_before_asked(self):
        self.app.config["tutorial"]["asked"] = False
        before = set(map(str, self._toplevels(self.root)))
        self.mod.maybe_show_tutorial()
        self._pump()
        after = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before, after, "intro must not show before the first-launch ask")

    def test_maybe_show_tutorial_shows_intro_once(self):
        self.app.config["tutorial"]["enabled"] = True
        self.app.config["tutorial"]["seen"]["exporter"] = False
        before = set(map(str, self._toplevels(self.root)))
        self.mod.maybe_show_tutorial()
        self._pump()
        after = [w for w in self._toplevels(self.root) if str(w) not in before]
        self.assertTrue(after, "first visit did not show the intro popup")
        self.assertTrue(self.app.config["tutorial"]["seen"]["exporter"])
        for w in after:
            w.destroy()
        self._pump()

        before2 = set(map(str, self._toplevels(self.root)))
        self.mod.maybe_show_tutorial()
        self._pump()
        after2 = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before2, after2, "intro reappeared on a second visit")

    def test_replay_button_opens_walkthrough_directly(self):
        self.app.config["tutorial"]["seen"]["exporter"] = True
        before = set(map(str, self._toplevels(self.root)))
        self.mod._start_walkthrough()
        self._pump()
        after = [w for w in self._toplevels(self.root) if str(w) not in before]
        self.assertTrue(after, "replay did not open the walkthrough")
        self._skip_walkthrough()
        self._pump()


class TestAboutTutorialWiring(_TkTestBase):
    """5.5.4b: same shape as TestHomeTutorialWiring, against the real AboutModule."""

    def setUp(self):
        import modules.about as about_mod
        self.app = _FakeApp(self.root)
        self.mod = about_mod.AboutModule(self.root, self.app)
        self.mod.pack(fill="both", expand=True)
        self._pump()

    def tearDown(self):
        try:
            self.mod.on_close()
        except Exception:
            pass
        try:
            self.mod.destroy()
        except tk.TclError:
            pass
        self._pump()

    def test_maybe_show_tutorial_noop_before_asked(self):
        self.app.config["tutorial"]["asked"] = False
        before = set(map(str, self._toplevels(self.root)))
        self.mod.maybe_show_tutorial()
        self._pump()
        after = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before, after, "intro must not show before the first-launch ask")

    def test_maybe_show_tutorial_shows_intro_once(self):
        self.app.config["tutorial"]["enabled"] = True
        self.app.config["tutorial"]["seen"]["about"] = False
        before = set(map(str, self._toplevels(self.root)))
        self.mod.maybe_show_tutorial()
        self._pump()
        after = [w for w in self._toplevels(self.root) if str(w) not in before]
        self.assertTrue(after, "first visit did not show the intro popup")
        self.assertTrue(self.app.config["tutorial"]["seen"]["about"])
        for w in after:
            w.destroy()
        self._pump()

        before2 = set(map(str, self._toplevels(self.root)))
        self.mod.maybe_show_tutorial()
        self._pump()
        after2 = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before2, after2, "intro reappeared on a second visit")

    def test_replay_button_opens_walkthrough_directly(self):
        self.app.config["tutorial"]["seen"]["about"] = True
        before = set(map(str, self._toplevels(self.root)))
        self.mod._start_walkthrough()
        self._pump()
        after = [w for w in self._toplevels(self.root) if str(w) not in before]
        self.assertTrue(after, "replay did not open the walkthrough")
        self._skip_walkthrough()
        self._pump()


class TestEditorTutorialWiring(_TkTestBase):
    """5.5.4c: same shape as the other modules' wiring tests, against the real
    EditorModule, plus editor-specific coverage for the collapsible-section steps
    (auto-expand + scroll-into-view, and that the tour never persists a collapsed
    state change the user didn't make themselves)."""

    #: step index -> the inspector section key it targets (see _start_walkthrough).
    #: Steps 0-2 (Timeline, Cut & audio markers, Keyframes) and 9-10 (Player options,
    #: Keybinds & Render) target plain widgets, not inspector sections.
    _SECTION_STEPS = {3: "seam", 4: "branding", 5: "content_boxes", 6: "layouts",
                      7: "audio", 8: "render_options"}

    def setUp(self):
        import modules.clip_editor as ce
        self.ce = ce
        self.app = _FakeApp(self.root)
        self.mod = ce.EditorModule(self.root, self.app)
        self.mod.pack(fill="both", expand=True)
        self._pump()

    def tearDown(self):
        try:
            self.mod.on_close()
        except Exception:
            pass
        try:
            self.mod.destroy()
        except tk.TclError:
            pass
        self._pump()

    def test_maybe_show_tutorial_noop_before_asked(self):
        self.app.config["tutorial"]["asked"] = False
        before = set(map(str, self._toplevels(self.root)))
        self.mod.maybe_show_tutorial()
        self._pump()
        after = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before, after, "intro must not show before the first-launch ask")

    def test_maybe_show_tutorial_shows_intro_once(self):
        self.app.config["tutorial"]["enabled"] = True
        self.app.config["tutorial"]["seen"]["editor"] = False
        before = set(map(str, self._toplevels(self.root)))
        self.mod.maybe_show_tutorial()
        self._pump()
        after = [w for w in self._toplevels(self.root) if str(w) not in before]
        self.assertTrue(after, "first visit did not show the intro popup")
        self.assertTrue(self.app.config["tutorial"]["seen"]["editor"])
        for w in after:
            w.destroy()
        self._pump()

        before2 = set(map(str, self._toplevels(self.root)))
        self.mod.maybe_show_tutorial()
        self._pump()
        after2 = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before2, after2, "intro reappeared on a second visit")

    def test_replay_button_opens_walkthrough_directly(self):
        self.app.config["tutorial"]["seen"]["editor"] = True
        before = set(map(str, self._toplevels(self.root)))
        self.mod._start_walkthrough()
        self._pump()
        after = [w for w in self._toplevels(self.root) if str(w) not in before]
        self.assertTrue(after, "replay did not open the walkthrough")
        self._skip_walkthrough()
        self._pump()

    def test_all_steps_resolve_and_sections_expand_into_view(self):
        """Drives the real 11-step tour end to end: every step must resolve a real,
        mapped target; every section step must auto-expand its (collapsed-by-default)
        section and scroll its header into the inspector's visible canvas bounds
        before the ring is drawn."""
        captured = {}
        real_walkthrough = self.ce.TutorialWalkthrough

        def _capture(root, steps):
            tour = real_walkthrough(root, steps)
            captured["tour"] = tour
            return tour

        with mock.patch.object(self.ce, "TutorialWalkthrough", side_effect=_capture):
            self.mod._start_walkthrough()
        self._pump()
        tour = captured["tour"]
        n = len(tour.steps)
        self.assertEqual(n, 11, "editor tour step count drifted from spec")

        canvas = self.mod._inspector.canvas
        for i in range(n):
            self._pump()
            self.assertEqual(tour.idx, i)
            self.assertTrue(tour._ring,
                            f"step {i} ({tour.steps[i]['title']}) has no ring - "
                            "target unresolved")
            key = self._SECTION_STEPS.get(i)
            if key is not None:
                sec = self.mod._sections[key]
                self.assertTrue(sec["open"], f"section '{key}' did not auto-expand")
                wy = sec["header"].winfo_rooty() - canvas.winfo_rooty()
                self.assertGreaterEqual(wy, -5,
                                        f"section '{key}' header scrolled above the "
                                        "visible inspector viewport")
                self.assertLessEqual(wy, canvas.winfo_height() + 5,
                                     f"section '{key}' header scrolled below the "
                                     "visible inspector viewport")
            if i < n - 1:
                tour._next()
        tour._cleanup()

    def test_programmatic_expand_does_not_persist_collapsed_state(self):
        """A section collapsed by default (e.g. BRANDING) must stay collapsed in the
        saved config after the tour walks past it and expands it for viewing -
        _tour_show_section's expand is programmatic, not a manual header click, and
        only the click handler (_section's toggle()) writes to
        editor.inspector_collapsed."""
        ecfg = self.app.config.setdefault("editor", {})
        ecfg.pop("inspector_collapsed", None)
        self.assertFalse(self.mod._sections["branding"]["open"],
                         "test assumes BRANDING starts collapsed by default")

        self.mod._tour_show_section("branding")
        self._pump()

        self.assertTrue(self.mod._sections["branding"]["open"],
                        "section did not expand for the tour step")
        self.assertNotIn("inspector_collapsed", self.app.config.get("editor", {}),
                         "programmatic expand-for-tour must not persist a "
                         "collapsed-state change")


class TestModuleSeenIndependence(_TkTestBase):
    """5.5.4b acceptance: visiting one tab's tutorial must not mark another tab seen -
    the three modules share one config dict, like the real app's tabs do."""

    def test_seen_flags_are_independent(self):
        import modules.about as about_mod
        import modules.exporter as exporter_mod
        import modules.importer as importer_mod

        app = _FakeApp(self.root)
        app.config["tutorial"]["enabled"] = True

        imp = importer_mod.ImporterModule(self.root, app)
        imp.pack(fill="both", expand=True)
        self._pump()
        imp.maybe_show_tutorial()
        self._pump()
        for w in self._toplevels(self.root):
            w.destroy()
        self._pump()

        seen = app.config["tutorial"]["seen"]
        self.assertTrue(seen["importer"])
        self.assertFalse(seen["exporter"])
        self.assertFalse(seen["about"])

        try:
            imp.on_close()
        except Exception:
            pass
        imp.destroy()
        self._pump()

        exp = exporter_mod.ExporterModule(self.root, app)
        exp.pack(fill="both", expand=True)
        self._pump()
        abt = about_mod.AboutModule(self.root, app)
        abt.pack(fill="both", expand=True)
        self._pump()

        self.assertFalse(seen["exporter"])
        self.assertFalse(seen["about"])

        try:
            exp.on_close()
        except Exception:
            pass
        try:
            abt.on_close()
        except Exception:
            pass
        exp.destroy()
        abt.destroy()
        self._pump()


class TestPositionCardAvoidsOverlap(_TkTestBase):
    """5.5.4b beta fix: a target too wide (or too close to an edge) for the card to
    sit beside without overlap must fall back to below/above, not clamp back onto the
    target. Regression test for the reported "step card covers the very control it
    points at" bug (module cards / performance mode)."""

    def test_card_does_not_cover_a_full_width_target(self):
        self.root.update_idletasks()
        root_w = self.root.winfo_width()
        w = tk.Frame(self.root, bg="red")
        w.place(x=10, y=80, width=max(200, root_w - 20), height=100)
        self._pump()

        steps = [{"target": lambda: w, "title": "Wide", "body": "Body."}]
        tour = TutorialWalkthrough(self.root, steps)
        tour.start()
        self._pump()

        tx, ty = w.winfo_rootx(), w.winfo_rooty()
        tw, th = w.winfo_width(), w.winfo_height()
        target_box = (tx, ty, tx + tw, ty + th)

        card = tour._card
        self.assertIsNotNone(card)
        cx, cy = card.winfo_rootx(), card.winfo_rooty()
        cw, ch = card.winfo_width(), card.winfo_height()
        card_box = (cx, cy, cx + cw, cy + ch)

        def overlaps(a, b):
            ax0, ay0, ax1, ay1 = a
            bx0, by0, bx1, by1 = b
            return ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1

        self.assertFalse(overlaps(card_box, target_box),
                         f"card {card_box} overlaps a full-width target {target_box}")

        w.destroy()
        tour._cleanup()


class TestScrollableFrameScrollTo(_TkTestBase):
    """5.5.4b beta fix: a step target scrolled below the fold is still "mapped" in Tk
    (winfo_ismapped stays True), so the walkthrough drew its ring at stale, off-screen
    coordinates. scroll_to() is what TutorialWalkthrough step on_show hooks call to
    bring the target into the viewport first."""

    def test_scroll_to_brings_offscreen_widget_into_view(self):
        frame = ScrollableFrame(self.root)
        frame.place(x=0, y=0, width=300, height=200)
        rows = [tk.Label(frame.body, text=f"row {i}", height=2) for i in range(40)]
        for r in rows:
            r.pack(fill="x")
        self._pump()

        target = rows[-1]   # far past the initial viewport
        rel_y_before = target.winfo_rooty() - frame.canvas.winfo_rooty()
        self.assertGreater(rel_y_before, frame.canvas.winfo_height(),
                           "test setup didn't actually scroll the target out of view")

        frame.scroll_to(target)
        self._pump()

        rel_y_after = target.winfo_rooty() - frame.canvas.winfo_rooty()
        self.assertGreaterEqual(rel_y_after, -1)
        self.assertLessEqual(rel_y_after, frame.canvas.winfo_height() + 1,
                             "target still outside the viewport after scroll_to")

        frame.destroy()

    def test_scroll_to_noop_when_everything_already_fits(self):
        frame = ScrollableFrame(self.root)
        frame.place(x=0, y=0, width=300, height=200)
        lbl = tk.Label(frame.body, text="only row")
        lbl.pack()
        self._pump()
        frame.scroll_to(lbl)   # must not raise, nothing to scroll
        self._pump()
        frame.destroy()


class TestAskTutorialPromptFiresIntroImmediately(_TkTestBase):
    """5.5.4b beta fix: Home is already the selected tab at launch, so the hub's
    generic <<NotebookTabChanged>> hook never fires for it on its own. Before this
    fix, Home's intro sat dormant until the user happened to switch away and back -
    reported as "it fires again when I click back on Home"."""

    def setUp(self):
        import modules.home as home_mod
        self.home_mod = home_mod
        # _FakeApp pre-answers both first-launch flags (see its docstring) specifically
        # so building a real HomeModule never schedules the real after(1200, ...) job.
        # Flip "asked" back to False only AFTER construction, purely to exercise
        # _ask_tutorial_prompt()'s own logic directly - never let the flag be False
        # while HomeModule.__init__ runs, or the dangling-timer gotcha comes back.
        self.app = _FakeApp(self.root)
        self.home = home_mod.HomeModule(self.root, self.app)
        self.home.pack(fill="both", expand=True)
        self._pump()
        self.app.config["tutorial"]["asked"] = False
        self._pump()

    def tearDown(self):
        try:
            self.home.destroy()
        except tk.TclError:
            pass
        self._pump()

    def test_answering_yes_shows_intro_without_a_tab_switch(self):
        with mock.patch.object(self.home_mod, "messagebox") as mb:
            mb.askyesno.return_value = True
            before = set(map(str, self._toplevels(self.root)))
            self.home._ask_tutorial_prompt()
            self._pump()
            after = [w for w in self._toplevels(self.root) if str(w) not in before]
        self.assertTrue(after, "Home's own intro did not fire immediately after the prompt")
        self.assertTrue(self.app.config["tutorial"]["seen"]["home"])
        for w in after:
            w.destroy()
        self._pump()

    def test_answering_no_does_not_show_intro(self):
        with mock.patch.object(self.home_mod, "messagebox") as mb:
            mb.askyesno.return_value = False
            before = set(map(str, self._toplevels(self.root)))
            self.home._ask_tutorial_prompt()
            self._pump()
            after = set(map(str, self._toplevels(self.root)))
        self.assertEqual(before, after)
        self.assertFalse(self.app.config["tutorial"]["seen"]["home"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
