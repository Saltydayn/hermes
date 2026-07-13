"""Scrub/timeline seek throttle regression test.

Found investigating a "several seconds to jump to a new section" report on long
clips (raised alongside SPEC_5.5.3a's preview-drag throttle, but a different UI
surface - the timeline drag-to-scrub and the Scale scrub bar, not the preview
canvas - so it is NOT part of that spec's named scope).

Both _on_tl_drag (dragging on the timeline to scrub) and _on_scrub (the Scale
widget's command) used to call _seek_to directly on every single motion event,
unthrottled. _seek_to's _read_frame call is a real cv2 decode-seek on a cache miss;
profiled against a 10-minute 1920x1080/60fps H.264 clip with a 5-second GOP (a
common encoder default), a cache-miss seek averaged ~70ms. A fast drag across a long
timeline fires dozens of motion events, each triggering one of these - a burst of 80
simulated events, played back synchronously the old way, measured 5.9 real seconds
with the UI completely frozen throughout (single-threaded Tk can't process anything
else mid-callback). _schedule_seek applies the same throttle shape already shipped
for the preview canvas (_schedule_preview_update) and the source-canvas box guides
(_schedule_box_draw): a pending job is left alone instead of being cancelled and
re-armed, and _seek_to reads the latest requested frame fresh when the job fires.
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


class TestScrubThrottle(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"Tk unavailable: {ex}")
        cls.root.geometry("1100x700+80+80")
        try:
            cls.clip = synth.make_temp_clip(n=200, fps=30.0)
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

        for attr in ("_preview_job", "_box_draw_job", "_seek_job"):
            job = getattr(ed, attr)
            if job is not None:
                real_after_cancel(job)
                setattr(ed, attr, None)

        sched = _FakeScheduler()
        ed.after = sched.after
        ed.after_cancel = sched.after_cancel

        calls = []

        def _stub(idx, from_scrub=False):
            ed._seek_job = None
            calls.append(idx)

        ed._seek_to = _stub
        return ed, sched, calls

    def test_throttle_caps_seeks_during_drag_burst(self):
        """80 scheduled seeks, 4ms apart (a fast timeline drag) should collapse to
        roughly 320ms/30ms fires, not 80 - proving the per-event decode-seek is
        actually throttled."""
        ed, sched, calls = self._boot()
        for i in range(80):
            ed._schedule_seek(1000 + i * 10)
            sched.advance(4)
        self.assertGreaterEqual(len(calls), 5,
                                 "scrub throttle should fire repeatedly during a drag")
        self.assertLessEqual(len(calls), 20,
                              "scrub throttle should not fire once per event")

    def test_last_position_always_wins(self):
        """Only the final requested position should ever render - never a stale
        intermediate one from earlier in the burst."""
        ed, sched, calls = self._boot()
        for i in range(80):
            ed._schedule_seek(1000 + i * 10)
            sched.advance(4)
        sched.advance(1000)   # drain anything still pending
        self.assertEqual(calls[-1], 1000 + 79 * 10)

    def test_discrete_call_still_fires_once(self):
        ed, sched, calls = self._boot()
        ed._schedule_seek(42)
        sched.advance(30)
        self.assertEqual(calls, [42])

    def test_from_scrub_flag_preserved_through_throttle(self):
        """_on_tl_drag relies on from_scrub defaulting False (so _seek_to keeps
        syncing the Scale thumb during a timeline drag) while _on_scrub passes True
        (so the Scale's own command echo doesn't loop). A throttled batch mixing both
        must keep whichever flag came with the LAST call, same as the position."""
        ed, sched, calls = self._boot()
        received = []
        real_stub = ed._seek_to

        def _stub(idx, from_scrub=False):
            received.append(from_scrub)
            real_stub(idx, from_scrub=from_scrub)

        ed._seek_to = _stub
        ed._schedule_seek(10, from_scrub=False)
        ed._schedule_seek(20, from_scrub=True)
        sched.advance(30)
        self.assertEqual(received, [True])


if __name__ == "__main__":
    unittest.main(verbosity=2)
