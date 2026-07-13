"""Phase 2.9.3 - trim-respecting playback ("In to Out only").

Drives the REAL playback engine (_start_play / _play_tick) on a live EditorModule + a
synthetic clip, forcing the wall clock to push playback past the window so the wrap/stop
branch runs deterministically. Asserts: trim ON clamps the start into [In,Out], loops to
In, and stops at Out; trim OFF keeps full-clip behavior. Needs Tk; self-skips headless.
"""

import os
import sys
import unittest
from time import perf_counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np      # noqa: E402
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


class TestTrimPlayback(unittest.TestCase):

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

    def _force_tick(self, ed, frames_ahead):
        """Pretend `frames_ahead` frames of wall time elapsed, run one tick, then cancel
        the rescheduled job so nothing fires later."""
        ed._play_clock0 = perf_counter() - frames_ahead / ed.fps
        ed._play_tick()
        if ed._play_job is not None:
            ed.after_cancel(ed._play_job)
            ed._play_job = None

    def _reset(self, ed, frame):
        ed._stop_play(refresh=False)
        if ed._play_job is not None:
            ed.after_cancel(ed._play_job)
            ed._play_job = None
        ed.frame_idx = frame

    def test_trim_window(self):
        app = _FakeApp()
        ed = ce.EditorModule(self.root, app)
        ed.pack(fill="both", expand=True)
        ed._load_waveform = lambda *a, **k: None       # no audio thread (no _pcm → silent clock)
        ed._render_play_frame = lambda *a, **k: None    # isolate the frame-index logic
        self._settle()
        self.addCleanup(ed.on_close)

        ed.load_clip(self.clip)
        self._settle()
        self.assertEqual(ed.total_frames, 40)
        # 2.10.2a: in_frame/out_frame derive from segments - set the one-segment trim window.
        ed.segments = [[10, 30]]             # wide window → timing headroom for the
        #                                      synchronous first tick (_play_frame0 is
        #                                      the deterministic clamp/loop signal)

        # ── trim ON, non-loop: start outside the window clamps to In, end stops at Out ──
        ed._play_trim_var.set(True)
        self._reset(ed, 5)
        ed._start_play(edits=True, loop=False)          # 5 < In → clamp to 10
        self.assertTrue(ed._playing)
        self.assertEqual(ed._play_frame0, 10, "start did not clamp to In")
        self.assertTrue(10 <= ed.frame_idx < 30, f"playhead left the window: {ed.frame_idx}")
        if ed._play_job is not None:
            ed.after_cancel(ed._play_job); ed._play_job = None
        self._force_tick(ed, 80)                         # blow past Out
        self.assertFalse(ed._playing, "non-loop trim playback did not stop")
        self.assertEqual(ed.frame_idx, 30, "stop did not clamp to Out")

        # ── trim ON, loop: wraps to In (not to the start frame) ──
        self._reset(ed, 12)
        ed._play_trim_var.set(True)
        ed._start_play(edits=True, loop=True)           # 12 inside → starts there
        self.assertEqual(ed._play_frame0, 12, "loop start should begin at the playhead")
        if ed._play_job is not None:
            ed.after_cancel(ed._play_job); ed._play_job = None
        self._force_tick(ed, 80)                         # past Out → wrap
        self.assertTrue(ed._playing, "looping trim playback should keep running")
        self.assertEqual(ed.frame_idx, 10, "loop did not wrap to In")
        self.assertEqual(ed._play_frame0, 10, "loop point not reset to In")
        self._reset(ed, 0)

        # ── trim OFF: full-clip behavior (loop point = start; stop lands on last frame) ──
        ed._play_trim_var.set(False)
        self._reset(ed, 5)
        ed._start_play(edits=True, loop=False)
        self.assertEqual(ed._play_frame0, 5, "trim-off start should not clamp")
        if ed._play_job is not None:
            ed.after_cancel(ed._play_job); ed._play_job = None
        self._force_tick(ed, 100)                        # past the clip end
        self.assertFalse(ed._playing)
        self.assertEqual(ed.frame_idx, ed.total_frames - 1, "full-clip stop not at last frame")


class TestMultiCutPlayback(unittest.TestCase):
    """2.10.2c: edits playback over the KEPT (output) timeline - removed ranges skipped,
    the playhead mapped back to source - plus the kept-PCM build. Single-segment playback
    is the unchanged 2.9.3 path (covered by TestTrimPlayback)."""

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

    def _boot(self):
        app = _FakeApp()
        ed = ce.EditorModule(self.root, app)
        ed.pack(fill="both", expand=True)
        ed._load_waveform = lambda *a, **k: None        # silent: tick uses the wall clock
        ed._render_play_frame = lambda *a, **k: None     # isolate the frame-index logic
        for _ in range(5):
            self.root.update()
        self.addCleanup(ed.destroy)
        self.addCleanup(ed.on_close)
        ed.load_clip(self.clip)
        for _ in range(5):
            self.root.update()
        return ed

    def _kill_job(self, ed):
        if ed._play_job is not None:
            ed.after_cancel(ed._play_job)
            ed._play_job = None

    # ── pure mappers (criterion 1) ──
    def test_output_to_source_sequence(self):
        seg = [[0, 3], [10, 12]]
        self.assertEqual([ce.EditorModule._source_of_output(seg, o) for o in range(7)],
                         [0, 1, 2, 3, 10, 11, 12])

    # ── kept-PCM build (criterion 2) ──
    def test_kept_pcm_build(self):
        ed = self._boot()
        ed.fps = 30.0
        rate = 48000                                     # rate/fps integer → exact slices
        ed._pcm_rate = rate
        n = 40 * rate // 30
        ed._pcm = (np.arange(n, dtype=np.int32) % 100).astype(np.int16)[:, None]
        ed.segments = [[0, 3], [10, 12]]
        ed._play_pcm = None
        pcm = ed._build_play_pcm()
        self.assertEqual(len(pcm), round(4 / 30 * rate) + round(3 / 30 * rate))
        self.assertEqual(int(pcm[0, 0]), int(ed._pcm[0, 0]))
        seam = round(4 / 30 * rate)
        self.assertEqual(int(pcm[seam, 0]), int(ed._pcm[round(10 / 30 * rate), 0]))
        # single segment → the exact trimmed slice
        ed.segments = [[5, 9]]
        pcm2 = ed._build_play_pcm()
        a, b = round(5 / 30 * rate), round(10 / 30 * rate)
        self.assertTrue(np.array_equal(pcm2, ed._pcm[a:b]))

    # ── gap clamp on play start (criterion 5) ──
    def test_gap_clamp_on_play_start(self):
        ed = self._boot()
        ed.segments = ed._normalize_segments([[0, 3], [10, 12]], ed.total_frames)
        ed.frame_idx = 6                                  # inside the removed gap (4..9)
        ed._start_play(edits=True, loop=False)
        self._kill_job(ed)
        self.assertTrue(ed._play_multiseg)
        self.assertEqual(ed._play_output0, 4)            # clamped forward to the next kept
        # The synchronous first tick may have advanced a frame or two, but the playhead is
        # ALWAYS a kept source frame (10/11/12) - never one of the removed 4..9.
        self.assertNotIn(ed.frame_idx, range(4, 10), "playback entered a removed range")
        self.assertIn(ed.frame_idx, set(ce.EditorModule._kept_frames(ed.segments)))
        ed._stop_play(refresh=False)

    # ── loop wrap + audio seek (criterion 4) ──
    def test_loop_wrap_audio_seek(self):
        ed = self._boot()
        ed.segments = ed._normalize_segments([[0, 3], [10, 12]], ed.total_frames)
        ed._pcm_rate = 48000
        ed.frame_idx = 10                                # output 4
        ed._start_play(edits=True, loop=True)            # trim OFF → window [output0, end]
        self._kill_job(ed)
        self.assertEqual(ed._play_output0, 4)
        ed._stream = object()                            # pretend audio is streaming
        ed._audio_start_sample = 0
        ed._audio_pos = 10_000 * 48000                   # far past the end → forces a wrap
        ed._play_tick()
        self._kill_job(ed)
        self.assertEqual(ed._play_output0, 4, "loop point not the start output")
        self.assertEqual(ed.frame_idx, 10, "wrap did not land on the first kept source")
        self.assertEqual(ed._audio_seek, int(4 / ed.fps * 48000))
        ed._stream = None
        ed._stop_play(refresh=False)

    # ── raw playback ignores cuts (criterion 6) ──
    def test_raw_ignores_cuts(self):
        ed = self._boot()
        ed.segments = ed._normalize_segments([[0, 3], [10, 12]], ed.total_frames)
        ed.frame_idx = 0
        ed._start_play(edits=False, loop=False)          # RAW → linear over the source
        self.assertFalse(ed._play_multiseg, "raw playback must not use the multiseg path")
        ed._play_clock0 = perf_counter() - 7 / ed.fps    # ~frame 7 (inside the gap)
        ed._play_tick()
        self._kill_job(ed)
        self.assertEqual(ed.frame_idx, 7, "raw did not advance linearly through the cut")
        ed._stop_play(refresh=False)

    # ── single-segment edits playback is NOT the multiseg path (criterion 3) ──
    def test_single_segment_stays_classic(self):
        ed = self._boot()
        ed.segments = ed._normalize_segments([[5, 25]], ed.total_frames)
        ed.frame_idx = 5
        ed._start_play(edits=True, loop=False)
        self._kill_job(ed)
        self.assertFalse(ed._play_multiseg, "one segment must use the 2.9.3 path")
        ed._stop_play(refresh=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
