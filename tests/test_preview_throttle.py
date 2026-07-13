"""SPEC 5.5.3a - preview-drag throttle regression test.

The reported "laggy" preview drag was not Tk render cost: it was
_schedule_preview_update using a cancel-and-reschedule debounce. Tk fires
<B1-Motion> on nearly every mouse-move sample, often faster than the 30ms delay
drags request, so every event cancelled the pending job and re-armed a fresh one;
during a real fast drag the timer could get pushed into the future forever and
never fire. The fix leaves a pending job alone (a throttle, not a debounce);
update_live_preview reads all state fresh at fire time, so nothing is lost.

Drives a real EditorModule + a synthetic clip (Tk needed; self-skips headless), but
replaces Tk's after/after_cancel with a virtual-clock fake so the burst is simulated
in zero wall-clock time. update_live_preview is replaced with a counting stub that
only does the one thing _schedule_preview_update's pending-job check depends on
(clearing self._preview_job); the real compositor/render path is exercised by the
other editor tests, not this one.
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
    def __init__(self):
        self.bus = MessageBus()
        self.config = {"editor": {}}
        self.paths = shared_paths

    def save_config(self):
        pass


class _FakeScheduler:
    """A virtual-clock stand-in for widget.after/after_cancel, no real sleeps."""

    def __init__(self):
        self.now = 0
        self._next_id = 1
        self.jobs = {}   # id -> [fire_time, callback, cancelled]

    def after(self, delay_ms, callback):
        jid = self._next_id
        self._next_id += 1
        self.jobs[jid] = [self.now + delay_ms, callback, False]
        return jid

    def after_cancel(self, jid):
        if jid in self.jobs:
            self.jobs[jid][2] = True

    def advance(self, ms):
        """Move the virtual clock forward and fire anything due, including jobs a
        callback schedules along the way, in fire-time order."""
        self.now += ms
        while True:
            due = [jid for jid, (t, _cb, cancelled) in self.jobs.items()
                   if t <= self.now and not cancelled]
            if not due:
                break
            jid = min(due, key=lambda j: self.jobs[j][0])
            _t, cb, cancelled = self.jobs.pop(jid)
            if not cancelled:
                cb()


def _pre_fix_schedule(ed, delay_ms=30, fast=True):
    """The old cancel-and-reschedule debounce, kept here verbatim (in shape) so the
    starvation bug it caused stays provable without reverting clip_editor.py."""
    ed._preview_fast = fast
    if ed._preview_job is not None:
        ed.after_cancel(ed._preview_job)
    ed._preview_job = ed.after(delay_ms, ed.update_live_preview)


class TestPreviewThrottle(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"Tk unavailable: {ex}")
        cls.root.geometry("1100x700+80+80")
        try:
            cls.clip = synth.make_temp_clip(n=40, fps=30.0)
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

    def _boot(self):
        app = _FakeApp()
        ed = ce.EditorModule(self.root, app)
        ed.pack(fill="both", expand=True)
        self._settle()
        ed.load_clip(self.clip)
        self._settle()

        real_after, real_after_cancel = ed.after, ed.after_cancel
        self.addCleanup(ed.destroy)
        self.addCleanup(ed.on_close)
        # Restore the real Tk scheduler before on_close/destroy run (LIFO), so its
        # own job cleanup (grep "_autosave_job" teardown loop) cancels real jobs.
        self.addCleanup(setattr, ed, "after_cancel", real_after_cancel)
        self.addCleanup(setattr, ed, "after", real_after)

        for attr in ("_preview_job", "_settle_job"):
            job = getattr(ed, attr)
            if job is not None:
                # Kill load_clip's real-Tk job. Orphaning it (not cancelling) would
                # fire a real callback against this widget after teardown, in a
                # later test.
                real_after_cancel(job)
                setattr(ed, attr, None)

        sched = _FakeScheduler()
        ed.after = sched.after
        ed.after_cancel = sched.after_cancel

        calls = []

        def _stub():
            ed._preview_job = None
            ed._preview_fast = False
            calls.append(1)

        ed.update_live_preview = _stub
        return ed, sched, calls

    def _drive_burst(self, schedule_fn, sched, calls=60, step_ms=5):
        """60 fast-mode schedule calls, 5ms apart: 300ms of virtual drag time,
        faster than the 30ms delay, matching a real fast drag."""
        for _ in range(calls):
            schedule_fn(30, True)
            sched.advance(step_ms)

    def test_debounce_starves_under_burst(self):
        """Pins down the bug: cancel-and-reschedule on every call means a burst
        faster than the delay pushes the job into the future forever."""
        ed, sched, calls = self._boot()
        self._drive_burst(lambda d, f: _pre_fix_schedule(ed, d, f), sched)
        self.assertEqual(len(calls), 0,
                          "pre-fix debounce logic should starve under a fast burst")

    def test_throttle_fires_during_burst(self):
        """The fix: a pending job is left alone, so it fires on its own schedule
        even while more fast requests keep arriving."""
        ed, sched, calls = self._boot()
        self._drive_burst(ed._schedule_preview_update, sched)
        self.assertGreaterEqual(len(calls), 5,
                                 "throttle should fire repeatedly during a sustained burst")
        self.assertLessEqual(len(calls), 15,
                              "throttle should not fire once per event either")

    def test_cadence_does_not_degrade_over_a_long_drag(self):
        """Regression for a real bug this exact suite missed: an earlier version had
        the settle pass reschedule itself from inside update_live_preview onto the
        SAME job slot the throttle uses. Since the throttle no-ops while any job is
        pending, that silently dropped the render cadence from ~30ms to the settle's
        ~250ms for the rest of a continuous drag - invisible to the 300ms burst test
        above because it never ran long enough to see the settle interval kick in.
        Reported by Joshua as a "very consistent" 250-450ms lag on the preview
        canvas. Over 900ms of continuous fast events, a healthy ~30ms cadence should
        produce on the order of 900/30=30 fires; a degraded ~250ms cadence would
        produce closer to 900/250=4 (after the first render). This pins the cadence
        to the throttle's delay, not the settle's."""
        ed, sched, calls = self._boot()
        self._drive_burst(ed._schedule_preview_update, sched, calls=180, step_ms=5)
        self.assertGreaterEqual(len(calls), 15,
                                 "render cadence degraded toward the settle interval "
                                 f"during a sustained drag (only {len(calls)} fires in 900ms)")

    def test_settle_fires_once_after_drag_stops(self):
        """The settle pass must still land exactly once, ~250ms after the last fast
        request - proving the redesign didn't just delete the crisp final render
        along with the cadence bug."""
        ed, sched, calls = self._boot()
        self._drive_burst(ed._schedule_preview_update, sched)
        during_drag = len(calls)
        sched.advance(400)   # past the 250ms settle window, well past any throttle delay
        self.assertEqual(len(calls), during_drag + 1,
                          "exactly one settle render should land after the drag goes quiet")
        sched.advance(1000)   # nothing should be pending anymore
        self.assertEqual(len(calls), during_drag + 1,
                          "no further renders should fire once settled")

    def test_discrete_call_still_fires_once(self):
        """A single non-burst call (slider release, checkbox toggle) behaves the
        same under throttle as it did under debounce: one call, one fire."""
        ed, sched, calls = self._boot()
        ed._schedule_preview_update(80, fast=False)
        sched.advance(80)
        self.assertEqual(len(calls), 1)


class TestBoxDrawThrottle(unittest.TestCase):
    """Companion fix found chasing a residual-choppiness report after the throttle
    above shipped: _on_source_drag called _draw_boxes() directly on every
    <B1-Motion> event (a full canvas.delete("boxes") + rect/corner-tick rebuild per
    event, unthrottled) - the identical per-event-unthrottled-Tk-work shape as the
    preview bug, just on the source canvas, and outside SPEC_5.5.3a's named scope
    (_on_preview_drag/_schedule_preview_update/update_live_preview only).
    _schedule_box_draw mirrors _schedule_preview_update's throttle pattern."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"Tk unavailable: {ex}")
        cls.root.geometry("1100x700+80+80")
        try:
            cls.clip = synth.make_temp_clip(n=40, fps=30.0)
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

    def _boot(self):
        app = _FakeApp()
        ed = ce.EditorModule(self.root, app)
        ed.pack(fill="both", expand=True)
        self._settle()
        ed.load_clip(self.clip)
        self._settle()

        real_after, real_after_cancel = ed.after, ed.after_cancel
        self.addCleanup(ed.destroy)
        self.addCleanup(ed.on_close)
        self.addCleanup(setattr, ed, "after_cancel", real_after_cancel)
        self.addCleanup(setattr, ed, "after", real_after)

        # load_clip schedules its own real-Tk preview job; not under test here, but
        # must be cancelled (not just untracked) before the fake scheduler takes
        # over, or it fires against a destroyed widget in a later test.
        for attr in ("_preview_job", "_box_draw_job"):
            job = getattr(ed, attr)
            if job is not None:
                real_after_cancel(job)
                setattr(ed, attr, None)

        sched = _FakeScheduler()
        ed.after = sched.after
        ed.after_cancel = sched.after_cancel

        calls = []

        def _stub():
            ed._box_draw_job = None
            calls.append(1)

        ed._draw_boxes = _stub
        return ed, sched, calls

    def test_throttle_caps_calls_during_burst(self):
        """60 calls to _schedule_box_draw, 5ms apart (300ms of virtual drag time,
        faster than the 30ms delay) should produce roughly 300ms/30ms fires, not
        one per call - proving the per-event rebuild is actually throttled."""
        ed, sched, calls = self._boot()
        for _ in range(60):
            ed._schedule_box_draw()
            sched.advance(5)
        self.assertGreaterEqual(len(calls), 5,
                                 "box-draw throttle should fire repeatedly during a burst")
        self.assertLessEqual(len(calls), 15,
                              "box-draw throttle should not fire once per event")

    def test_discrete_call_still_fires_once(self):
        ed, sched, calls = self._boot()
        ed._schedule_box_draw()
        sched.advance(30)
        self.assertEqual(len(calls), 1)


class TestPreviewCanvasReuse(unittest.TestCase):
    """Second follow-up found chasing the same choppiness report after both throttle
    fixes above shipped: at a realistic preview size (1920x1080 source, a ~640x1140
    portrait canvas), update_live_preview's delete("all") + a fresh ImageTk.PhotoImage
    every render measured at ~5ms of a ~12ms call (profiled, not guessed - the naive
    hypothesis that Tk cost was negligible held only at a small 720p/416x740 test
    size). update_live_preview now reuses one PhotoImage (.paste() when the pixel
    size is unchanged) and one canvas image item (moved via coords(), tagged guides
    cleared via delete("guide") instead of delete("all")). Outside SPEC_5.5.3a's
    named scope (it anticipated this exact change as an OPTIONAL section 3, gated on
    a manual retest still showing stutter after the throttle fix - which it did)."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"Tk unavailable: {ex}")
        cls.root.geometry("1100x900+80+80")
        try:
            cls.clip = synth.make_temp_clip(n=40, fps=30.0)
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

    def _boot(self):
        app = _FakeApp()
        ed = ce.EditorModule(self.root, app)
        ed.pack(fill="both", expand=True)
        self._settle()
        self.addCleanup(ed.destroy)
        self.addCleanup(ed.on_close)
        ed.load_clip(self.clip)
        self._settle()
        ed.hl_on.set(True)
        if not ed.highlights:
            ed.add_highlight()   # also schedules its own real preview job
        self._settle()
        self._kill_pending(ed)
        return ed

    def _kill_pending(self, ed):
        """Cancel whatever real-Tk preview job is currently pending. Every direct
        call to update_live_preview(fast=True) below arms its own settle-pass timer
        (see the "if fast:" tail of update_live_preview) - calling it again without
        cancelling that timer first orphans it exactly like load_clip's own initial
        job did (TestPreviewThrottle._boot), so this runs before every call, not
        just once at boot."""
        if ed._preview_job is not None:
            ed.after_cancel(ed._preview_job)
            ed._preview_job = None

    def _render(self, ed, fast=True):
        self._kill_pending(ed)
        ed._preview_fast = fast
        ed.update_live_preview()
        self._kill_pending(ed)   # kill the settle-pass timer this call may have armed

    def test_image_item_and_photo_reused_at_same_size(self):
        ed = self._boot()
        c = ed._preview_canvas
        self._render(ed)
        item1, photo1 = ed._preview_image_item, ed._preview_photo
        self.assertIsNotNone(item1)
        ed.frame_idx = min(5, ed.total_frames - 1)
        self._render(ed)
        self.assertEqual(ed._preview_image_item, item1,
                          "the canvas image item should be reused, not recreated")
        self.assertIs(ed._preview_photo, photo1,
                       "the PhotoImage object should be reused via paste(), not rebuilt")
        self.assertEqual(c.type(item1), "image")

    def test_repeated_renders_do_not_leak_canvas_items(self):
        ed = self._boot()
        c = ed._preview_canvas
        self._render(ed)
        baseline = len(c.find_all())
        for i in range(10):
            ed.frame_idx = i % ed.total_frames
            self._render(ed)
        self.assertEqual(len(c.find_all()), baseline,
                          "guide items must be cleared each render, not accumulated")

    def test_paste_actually_updates_pixel_content(self):
        """A minimal, isolated proof that .paste() mutates displayed pixels in place
        (not just object identity) - the property the reuse optimization depends on."""
        from PIL import Image, ImageTk
        photo = ImageTk.PhotoImage(Image.new("RGB", (20, 20), (255, 0, 0)))
        self.assertEqual(self.root.tk.call(str(photo), "get", 5, 5), (255, 0, 0))
        photo.paste(Image.new("RGB", (20, 20), (0, 255, 0)))
        self.assertEqual(self.root.tk.call(str(photo), "get", 5, 5), (0, 255, 0))

    def test_recovers_after_external_canvas_wipe(self):
        """_render_play_frame and the no-clip placeholder still delete("all") on this
        same canvas (out of scope to change - see SPEC_5.5.3a). A stale cached item id
        must not raise; update_live_preview should detect it via c.type() and recreate
        cleanly."""
        ed = self._boot()
        c = ed._preview_canvas
        self._render(ed)
        c.delete("all")   # simulate _render_play_frame's / _show_empty's wipe
        self._render(ed)   # must not raise tk.TclError
        self.assertEqual(c.type(ed._preview_image_item), "image")
        self.assertEqual(sum(1 for i in c.find_all() if c.type(i) == "image"), 1,
                          "exactly one image item after recovering from an external wipe")


if __name__ == "__main__":
    unittest.main(verbosity=2)
