"""Phase 2.9.1 - headless regression net for the editor's PURE module-level code.

These lock the exact pieces 2.9.2 will move into modules/editor/ (compositor, interp,
filter_visible, FrameCache, _set_process_priority) plus the persistence math helpers and
the bus. No Tk, no display, no real clip - fully headless. After the 2.9.2 refactor these
must stay green unchanged.
"""

import hashlib
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2          # noqa: E402
import numpy as np  # noqa: E402

import modules.clip_editor as ce          # noqa: E402
from modules.editor.compositor import _resize_smart   # noqa: E402
from shared.bus import MessageBus          # noqa: E402
from tests import synth                    # noqa: E402


def _src_frame():
    """A deterministic BGR source frame to composite from."""
    h, w = synth.SRC_H, synth.SRC_W
    f = np.zeros((h, w, 3), np.uint8)
    f[:, :, 0] = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
    f[:, :, 1] = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
    f[20:60, 40:120] = (10, 200, 250)
    return f


def _compose_kwargs():
    params, _ = synth.sample_render_params()
    return dict(
        scale_x=params["scale_x"], scale_y=params["scale_y"],
        game_box=params["keyframes"][0]["game_box"],
        cam_box=params["keyframes"][0]["cam_box"],
        split_ratio=50, melt=params["melt"], melt_px=params["melt_px"],
        solid_border=params["border"], border_color=params["border_color"],
        watermark=params["watermark"], highlights=params["highlights"],
    )


class TestComposite(unittest.TestCase):
    """composite_frame: shape/dtype, determinism, and input non-mutation (purity)."""

    def test_shape_and_dtype(self):
        out = ce.composite_frame(_src_frame(), **_compose_kwargs())
        self.assertEqual(out.shape, (ce.OUT_H, ce.OUT_W, 3))   # 1920×1080×3
        self.assertEqual(out.dtype, np.uint8)

    def test_deterministic(self):
        kw = _compose_kwargs()
        a = ce.composite_frame(_src_frame(), **kw)
        b = ce.composite_frame(_src_frame(), **kw)
        self.assertTrue(np.array_equal(a, b))

    def test_does_not_mutate_source(self):
        src = _src_frame()
        before = src.copy()
        ce.composite_frame(src, **_compose_kwargs())
        self.assertTrue(np.array_equal(src, before), "compositor mutated its input frame")

    def test_border_px_default_and_thickness(self):
        """2.9.5: border_px is a defaulted signature EXTENSION - omitting it equals the
        old fixed 12, and a larger value paints a thicker seam band."""
        kw = _compose_kwargs()
        kw["solid_border"] = True
        omitted = ce.composite_frame(_src_frame(), **kw)
        explicit12 = ce.composite_frame(_src_frame(), **dict(kw, border_px=12))
        self.assertTrue(np.array_equal(omitted, explicit12),
                        "default border_px must equal the original fixed 12")
        bc = (7, 7, 7)   # a value the synthetic source never produces at the sample column
        thin = ce.composite_frame(_src_frame(), **dict(kw, border_px=4, border_color=bc))
        thick = ce.composite_frame(_src_frame(), **dict(kw, border_px=40, border_color=bc))
        col = 5           # near the left edge: only the full-width border band lands here
        n_thin = int(np.all(thin[:, col] == bc, axis=1).sum())
        n_thick = int(np.all(thick[:, col] == bc, axis=1).sum())
        self.assertGreater(n_thin, 0)
        self.assertGreater(n_thick, n_thin, "border_px did not thicken the band")

    # ── 2.10.1 single-panel layout mode (defaulted kwarg; one 9:16 crop fills the frame) ──
    def test_single_panel_defaulted_kwarg_equivalence(self):
        """Omitting single_panel == passing single_panel=False (the two-panel default)."""
        kw = _compose_kwargs()
        omitted = ce.composite_frame(_src_frame(), **kw)
        explicit = ce.composite_frame(_src_frame(), **dict(kw, single_panel=False))
        self.assertTrue(np.array_equal(omitted, explicit),
                        "single_panel=False must equal omitting the kwarg")

    def test_single_panel_full_frame_fill(self):
        """single_panel=True (no furniture) == an independent full-frame resize of the
        game crop - proves one crop covers 1080x1920 and no cam panel is drawn."""
        kw = dict(_compose_kwargs(), single_panel=True, melt=False,
                  solid_border=False, watermark=None, highlights=())
        src = _src_frame()
        out = ce.composite_frame(src, **kw)
        gb, sx, sy = kw["game_box"], kw["scale_x"], kw["scale_y"]
        g_x1, g_y1 = int(gb[0] * sx), int(gb[1] * sy)
        g_x2, g_y2 = g_x1 + int(gb[2] * sx), g_y1 + int(gb[3] * sy)
        expected = _resize_smart(src[g_y1:g_y2, g_x1:g_x2], ce.OUT_W, ce.OUT_H)
        self.assertTrue(np.array_equal(out, expected),
                        "single-panel output is not the full-frame game-crop resize")

    def test_single_panel_ignores_cam_box(self):
        """With single_panel=True the cam crop is never composited → output is
        independent of cam_box."""
        kw = dict(_compose_kwargs(), single_panel=True)
        a = ce.composite_frame(_src_frame(), **dict(kw, cam_box=[0, 0, 100, 50]))
        b = ce.composite_frame(_src_frame(), **dict(kw, cam_box=[150, 80, 120, 60]))
        self.assertTrue(np.array_equal(a, b), "single-panel output depended on cam_box")

    def test_single_panel_line_tracks_split(self):
        """The decorative line/plate anchor (boundary_y) slides the full 0..100% range:
        the solid-border band centers near split% of the height."""
        bc = (3, 5, 7)   # R=7: the synthetic source never produces it
        kw = dict(_compose_kwargs(), single_panel=True, solid_border=True,
                  border_color=bc, watermark=None, highlights=(), melt=False)

        def band_center(split):
            out = ce.composite_frame(_src_frame(), **dict(kw, split_ratio=split))
            rows = np.where(np.all(np.all(out == bc, axis=2), axis=1))[0]
            self.assertGreater(rows.size, 0, f"no border band at split={split}")
            return float(rows.mean())

        c20, c80 = band_center(20), band_center(80)
        self.assertNotAlmostEqual(c20, c80, msg="line did not move with split")
        self.assertLess(abs(c20 - 0.20 * ce.OUT_H), 14)   # ±border half-thickness (12)+1
        self.assertLess(abs(c80 - 0.80 * ce.OUT_H), 14)

    def test_single_panel_melt_is_noop(self):
        """No seam in single-panel → melt on/off produce identical output."""
        kw = dict(_compose_kwargs(), single_panel=True)
        on = ce.composite_frame(_src_frame(), **dict(kw, melt=True))
        off = ce.composite_frame(_src_frame(), **dict(kw, melt=False))
        self.assertTrue(np.array_equal(on, off), "melt changed single-panel output")

    def test_single_panel_does_not_mutate_source(self):
        src = _src_frame()
        before = src.copy()
        ce.composite_frame(src, **dict(_compose_kwargs(), single_panel=True))
        self.assertTrue(np.array_equal(src, before),
                        "single-panel compositor mutated its input frame")


class TestSharpenRender(unittest.TestCase):
    """3.1: render-only sharpen applied inside _compose_render_frame, AFTER composite_frame.
    Off / strength 0 must be a guaranteed no-op (byte-identical to pre-3.1); on must change
    pixels but keep shape/dtype; and the PREVIEW compositor (composite_frame) must never
    sharpen. Headless - no Tk, no clip."""

    def _compose(self, **over):
        params, static = synth.sample_render_params()
        params = dict(params, **over)
        return ce.EditorModule._compose_render_frame(_src_frame(), 10, params, static)

    def test_off_equals_no_key_and_strength_zero(self):
        """Pre-3.1 (no key) == sharpen=False == sharpen=True/strength 0 - the default path
        is byte-for-byte untouched."""
        no_key = self._compose()                                   # params carry no sharpen key
        off = self._compose(sharpen=False, sharpen_strength=1.0)
        zero = self._compose(sharpen=True, sharpen_strength=0.0)
        self.assertTrue(np.array_equal(no_key, off), "sharpen=False drifted from the default")
        self.assertTrue(np.array_equal(off, zero), "sharpen strength 0 was not a no-op")

    def test_on_changes_pixels_same_shape_dtype(self):
        off = self._compose(sharpen=False)
        on = self._compose(sharpen=True, sharpen_strength=1.0)
        self.assertEqual(on.shape, (ce.OUT_H, ce.OUT_W, 3))
        self.assertEqual(on.dtype, np.uint8)
        self.assertFalse(np.array_equal(on, off), "sharpen on did not change any pixels")

    def test_render_frame_calls_sharpen_only_when_on(self):
        """_compose_render_frame calls video_utils.sharpen exactly once when on, never when
        off."""
        from unittest import mock
        params, static = synth.sample_render_params()
        with mock.patch.object(ce.video_utils, "sharpen",
                               side_effect=lambda f, s: f) as spy:
            ce.EditorModule._compose_render_frame(
                _src_frame(), 10, dict(params, sharpen=False), static)
            self.assertEqual(spy.call_count, 0)
            ce.EditorModule._compose_render_frame(
                _src_frame(), 10, dict(params, sharpen=True, sharpen_strength=1.0), static)
            self.assertEqual(spy.call_count, 1)

    def test_preview_compositor_never_sharpens(self):
        """The live-preview/playback path composites via composite_frame directly (no sharpen
        wrapper) - proving sharpen is render-only."""
        from unittest import mock
        with mock.patch.object(ce.video_utils, "sharpen",
                               side_effect=lambda f, s: f) as spy:
            ce.composite_frame(_src_frame(), **_compose_kwargs())
            self.assertEqual(spy.call_count, 0, "the preview compositor called sharpen")


class TestSegments(unittest.TestCase):
    """2.10.2a multi-cut model: the normalizer + the source/output index mappers. All
    static (segment-list-driven), so headless - no Tk, no clip."""

    norm = staticmethod(ce.EditorModule._normalize_segments)
    o_of_s = staticmethod(ce.EditorModule._output_of_source)
    s_of_o = staticmethod(ce.EditorModule._source_of_output)
    kept_total = staticmethod(ce.EditorModule._kept_total)
    kept_frames = staticmethod(ce.EditorModule._kept_frames)
    remove = staticmethod(ce.EditorModule._remove_range)
    add = staticmethod(ce.EditorModule._add_range)
    mark = staticmethod(ce.EditorModule._insert_marker)
    unmark = staticmethod(ce.EditorModule._delete_marker)

    def test_normalize(self):
        # 5.5.2b: output is always [start, end, gain_db]; legacy 2-length reads as 0.0.
        self.assertEqual(self.norm([[5, 10], [0, 3]], 20),
                         [[0, 3, 0.0], [5, 10, 0.0]])                           # sorted
        self.assertEqual(self.norm([[0, 5], [3, 9]], 20), [[0, 9, 0.0]])        # overlap
        self.assertEqual(self.norm([[0, 4], [5, 9]], 20), [[0, 9, 0.0]])        # adjacent
        self.assertEqual(self.norm([[-2, 3], [8, 999]], 20),
                         [[0, 3, 0.0], [8, 19, 0.0]])                           # clamp
        self.assertEqual(self.norm([], 20), [[0, 19, 0.0]])                     # never empty
        self.assertEqual(self.norm([[7, 2]], 20), [[0, 19, 0.0]])               # e<s dropped

    def test_kept_total_and_frames(self):
        seg = [[0, 3], [10, 12]]
        self.assertEqual(self.kept_total(seg), 7)
        self.assertEqual(list(self.kept_frames(seg)), [0, 1, 2, 3, 10, 11, 12])

    def test_mappers_roundtrip(self):
        seg = [[0, 3], [10, 12]]
        self.assertEqual(self.o_of_s(seg, 11), 5)
        self.assertIsNone(self.o_of_s(seg, 7))          # removed gap
        self.assertEqual(self.s_of_o(seg, 5), 11)
        self.assertEqual(self.s_of_o(seg, 0), 0)
        for o in range(self.kept_total(seg)):           # exact round-trip over the kept set
            self.assertEqual(self.o_of_s(seg, self.s_of_o(seg, o)), o)

    def test_remove_range(self):
        """2.10.2d: subtracting a source range splits/drops kept segments; refuses to
        empty. 5.5.2b: splits inherit the parent gain (legacy input reads as 0.0)."""
        self.assertEqual(self.remove([[0, 20]], 5, 10),
                         [[0, 4, 0.0], [11, 20, 0.0]])                           # mid split
        self.assertEqual(self.remove([[0, 4], [11, 20]], 3, 13),
                         [[0, 2, 0.0], [14, 20, 0.0]])                           # spans 2
        self.assertEqual(self.remove([[0, 4], [11, 20]], 0, 4),
                         [[11, 20, 0.0]])                                        # covers one
        self.assertEqual(self.remove([[0, 20]], 0, 20), [[0, 20]])              # refuse-empty
        self.assertEqual(self.remove([[0, 20, 4.0]], 5, 10),
                         [[0, 4, 4.0], [11, 20, 4.0]])                           # gain rides

    def test_add_range(self):
        """2.10.2d: restoring a range unions it back; normalize merges overlaps/adjacency."""
        norm = lambda segs: self.norm(segs, 21)
        self.assertEqual(norm(self.add([[0, 4], [11, 20]], 5, 10)),
                         [[0, 20, 0.0]])                                         # bridges gap
        self.assertEqual(norm(self.add([[0, 4], [14, 20]], 8, 11)),
                         [[0, 4, 0.0], [8, 11, 0.0], [14, 20, 0.0]])             # new island
        segs = norm(self.add([[0, 4], [14, 20]], 8, 11))
        segs = norm(self.add(segs, 5, 7))                                        # fills 5..7
        segs = norm(self.add(segs, 12, 13))                                      # fills 12..13
        self.assertEqual(segs, [[0, 20, 0.0]])                                  # collapses whole

    def test_single_segment_equivalence(self):
        """One segment must reduce to today's In/Out behavior (the b/c safety guarantee)."""
        in_f, out_f = 4, 17
        seg = [[in_f, out_f]]
        self.assertEqual(list(self.kept_frames(seg)), list(range(in_f, out_f + 1)))
        for k in range(out_f - in_f + 1):
            self.assertEqual(self.s_of_o(seg, k), in_f + k)

    def test_insert_marker(self):
        """5.5.2f: a single marker splits the segment it falls in WITHOUT removing
        frames - two markers then enclose the piece between them, one boundary at a
        time (no start/end pairing, unlike _remove_range)."""
        self.assertEqual(self.mark([[0, 100, 0.0]], 20),
                         [[0, 20, 0.0], [21, 100, 0.0]])                        # one boundary
        self.assertEqual(self.mark([[0, 20, 0.0], [21, 100, 0.0]], 40),
                         [[0, 20, 0.0], [21, 40, 0.0], [41, 100, 0.0]])         # a second encloses
        self.assertEqual(self.mark([[0, 100, 3.0]], 20),
                         [[0, 20, 3.0], [21, 100, 3.0]])                        # inherits gain
        self.assertEqual(self.mark([[0, 100, 0.0]], 100),
                         [[0, 100, 0.0]])                                       # already a boundary
        self.assertEqual(self.mark([[0, 20, 0.0], [60, 100, 0.0]], 40),
                         [[0, 20, 0.0], [60, 100, 0.0]])                        # inside a cut gap

    def test_insert_marker_self_cleans_at_zero_gain(self):
        """A marked-off piece left at 0 dB is indistinguishable from its neighbor, so
        the next normalize pass merges it away; a distinct gain survives normalize."""
        marked = self.mark([[0, 100, 0.0]], 20)
        self.assertEqual(self.norm(marked, 101), [[0, 100, 0.0]])
        marked[0][2] = 3.0
        self.assertEqual(self.norm(marked, 101), [[0, 20, 3.0], [21, 100, 0.0]])

    def test_delete_marker(self):
        """5.5.2f: right-click deletes a Mark Audio boundary by merging the two
        segments it separates, regardless of gain (unlike normalize's equal-gain-only
        merge) - the merged piece keeps the LEFT segment's gain."""
        self.assertEqual(self.unmark([[0, 20, 0.0], [21, 100, 0.0]], 20),
                         [[0, 100, 0.0]])                                    # plain merge
        self.assertEqual(self.unmark([[0, 20, 5.0], [21, 100, -3.0]], 20),
                         [[0, 100, 5.0]])                                    # keeps LEFT gain
        self.assertEqual(self.unmark([[0, 20, 0.0], [60, 100, 0.0]], 20),
                         [[0, 20, 0.0], [60, 100, 0.0]])                     # a cut gap, no-op
        self.assertEqual(self.unmark([[0, 100, 0.0]], 50),
                         [[0, 100, 0.0]])                                    # no boundary there


class TestSerialVsParallel(unittest.TestCase):
    """The 2.8.4 proof, made permanent: the Eco (serial) and Performance (parallel)
    feed paths must emit a BIT-IDENTICAL ordered frame sequence. Drives the REAL
    EditorModule._feed_serial / _feed_parallel against a tiny stub self + a hash-
    recording stub writer (no ffmpeg, no UI)."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.clip = synth.make_temp_clip()
        except RuntimeError as ex:
            raise unittest.SkipTest(str(ex))

    @classmethod
    def tearDownClass(cls):
        try:
            os.remove(cls.clip)
        except OSError:
            pass

    class _Stub:
        # _feed_* reference self._compose_render_frame + self._render_walk (real
        # staticmethods) and marshal progress through self._marshal(self._render_progress,
        # ...). The stub supplies exactly those names and nothing else.
        _compose_render_frame = staticmethod(ce.EditorModule._compose_render_frame)
        _render_walk = staticmethod(ce.EditorModule._render_walk)

        def _marshal(self, *a, **k):
            pass

        def _render_progress(self, *a, **k):
            pass

    class _HashWriter:
        def __init__(self):
            self.hashes = []

        def write(self, frame):
            self.hashes.append(
                hashlib.sha1(np.ascontiguousarray(frame).tobytes()).hexdigest())

    def _run(self, feed_name, params, static):
        cap = cv2.VideoCapture(self.clip)
        cap.set(cv2.CAP_PROP_POS_FRAMES, params["in_f"])   # _render_encode seeks; mirror it
        w = self._HashWriter()
        fed, err = getattr(ce.EditorModule, feed_name)(
            self._Stub(), cap, w, params, static, threading.Event())
        cap.release()
        self.assertTrue(fed, f"{feed_name} reported not-fed: {err}")
        return w.hashes

    def test_bit_identical(self):
        params, static = synth.sample_render_params()
        expected = params["out_f"] - params["in_f"] + 1
        serial = self._run("_feed_serial", params, static)
        parallel = self._run("_feed_parallel", params, static)
        self.assertEqual(len(serial), expected)
        self.assertEqual(serial, parallel, "serial vs parallel render diverged")

    def test_bit_identical_with_cuts(self):
        """2.10.2b: the serial==parallel invariant must hold under multi-cut."""
        params, static = synth.sample_render_params(segments=[[0, 3], [8, 11]])
        serial = self._run("_feed_serial", params, static)
        parallel = self._run("_feed_parallel", params, static)
        self.assertEqual(len(serial), ce.EditorModule._kept_total([[0, 3], [8, 11]]))
        self.assertEqual(serial, parallel, "serial vs parallel diverged under cuts")

    def test_bit_identical_with_sharpen(self):
        """3.1: the serial==parallel invariant must hold with render-only sharpen on, and the
        sharpened sequence must differ from the unsharpened one."""
        params, static = synth.sample_render_params()
        params = dict(params, sharpen=True, sharpen_strength=1.0)
        serial = self._run("_feed_serial", params, static)
        parallel = self._run("_feed_parallel", params, static)
        self.assertEqual(serial, parallel, "serial vs parallel diverged with sharpen on")
        plain = self._run("_feed_serial", synth.sample_render_params()[0], static)
        self.assertNotEqual(serial, plain, "sharpen on produced the unsharpened frames")


class TestRenderCuts(unittest.TestCase):
    """2.10.2b: the render FEED skips removed frames (video), preserving kept-frame order,
    with keyframes staying SOURCE-indexed. Hash-based; reuses TestSerialVsParallel's stubs."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.clip = synth.make_temp_clip()      # 40 frames, moving block ⇒ all differ
        except RuntimeError as ex:
            raise unittest.SkipTest(str(ex))

    @classmethod
    def tearDownClass(cls):
        try:
            os.remove(cls.clip)
        except OSError:
            pass

    @staticmethod
    def _hash(frame):
        return hashlib.sha1(np.ascontiguousarray(frame).tobytes()).hexdigest()

    def _feed(self, feed_name, params, static):
        cap = cv2.VideoCapture(self.clip)
        cap.set(cv2.CAP_PROP_POS_FRAMES, params["in_f"])   # _render_encode seeks; mirror it
        w = TestSerialVsParallel._HashWriter()
        fed, err = getattr(ce.EditorModule, feed_name)(
            TestSerialVsParallel._Stub(), cap, w, params, static, threading.Event())
        cap.release()
        self.assertTrue(fed, f"{feed_name} not fed: {err}")
        return w.hashes

    def _compose_hash(self, src, params, static):
        """Independently decode SOURCE frame `src` and composite it with `params` (keyframes
        included) - the reference for what the feed should emit at that source index."""
        cap = cv2.VideoCapture(self.clip)
        cap.set(cv2.CAP_PROP_POS_FRAMES, src)
        ok, frame = cap.read()
        cap.release()
        self.assertTrue(ok, f"could not read source frame {src}")
        return self._hash(ce.EditorModule._compose_render_frame(frame, src, params, static))

    def _expected(self, params, static):
        """The reference hash list: each KEPT source frame composited, in output order."""
        return [self._compose_hash(src, params, static)
                for src in ce.EditorModule._kept_frames(params["segments"])]

    def test_single_segment_unchanged(self):
        """One segment → the feed composites exactly in_f..out_f, in order (default path)."""
        params, static = synth.sample_render_params()      # one segment
        self.assertEqual(self._feed("_feed_serial", params, static),
                         self._expected(params, static))

    def test_kept_count_and_order(self):
        seg = [[0, 3], [8, 11]]
        params, static = synth.sample_render_params(segments=seg)
        hashes = self._feed("_feed_serial", params, static)
        self.assertEqual(len(hashes), ce.EditorModule._kept_total(seg))   # 8 kept
        self.assertEqual(hashes, self._expected(params, static))          # exact order

    def test_keyframe_source_indexing(self):
        """A keyframe at SOURCE frame 9 with segments [[0,3],[8,11]] (kept order
        [0,1,2,3,8,9,10,11]) governs OUTPUT frame 5 (src 9), not output 4 (src 8) - the cut
        shifts output time, the key stays source-indexed."""
        seg = [[0, 3], [8, 11]]
        params, static = synth.sample_render_params(segments=seg)
        A = {"game_box": [0, 0, 320, 90], "cam_box": [0, 90, 320, 90],
             "split_ratio": 50, "highlights": []}
        B = {"game_box": [0, 0, 320, 30], "cam_box": [0, 30, 320, 150],
             "split_ratio": 17, "highlights": []}   # a layout the static baseline never makes
        params = dict(params, keyframes=[{"frame": 0, "type": "jump", **A},
                                         {"frame": 9, "type": "jump", **B}])
        hashes = self._feed("_feed_serial", params, static)
        self.assertEqual(hashes, self._expected(params, static))
        # Output 5 (src 9) reflects B; output 4 (src 8) is still A → they differ.
        self.assertEqual(hashes[5], self._compose_hash(9, params, static))
        self.assertNotEqual(hashes[5], hashes[4], "src-9 keyframe did not land at output 5")
        self.assertEqual(hashes[0], self._compose_hash(0, params, static))


class TestRenderWriterAudio(unittest.TestCase):
    """2.10.2b writer-level: cut-aware audio mux through H264PipeWriter (atrim+concat+apad).
    Real ffmpeg required - skips when absent. Verifies no final-frame drop (the 2.8.5 fix)
    and graceful video-only output on a silent source."""

    @classmethod
    def setUpClass(cls):
        from shared import video_utils
        if video_utils.find_ffmpeg() is None:
            raise unittest.SkipTest("ffmpeg not available")

    @staticmethod
    def _pipe(writer, n, w, h):
        frame = np.zeros((h, w, 3), np.uint8)
        for i in range(n):
            frame[:] = (i * 3) % 256        # distinct frames; content is irrelevant here
            writer.write(frame)

    @staticmethod
    def _count_frames(path):
        cap = cv2.VideoCapture(path)
        c = 0
        while cap.read()[0]:
            c += 1
        cap.release()
        return c

    def test_audio_video_alignment_no_final_drop(self):
        from shared import video_utils
        fps, w, h = 30.0, 64, 64
        seg_f = [[0, 9], [20, 29]]
        kept = ce.EditorModule._kept_total(seg_f)                 # 20
        seg_s = [(s / fps, (e + 1) / fps) for s, e in seg_f]
        tone = synth.make_temp_tone_wav(seconds=1.5)
        out = tempfile.mktemp(suffix=".mp4", prefix="sc_seg_av_")
        try:
            writer = video_utils.H264PipeWriter(out, w, h, fps, audio_src=tone,
                                                segments=seg_s)
            self._pipe(writer, kept, w, h)
            ok, err = writer.close()
            self.assertTrue(ok, f"writer failed: {err}")
            # Exactly kept_total video frames out → no 2.8.5 final-frame drop (apad+shortest).
            self.assertEqual(self._count_frames(out), kept)
            self.assertTrue(video_utils.has_audio(out), "cut audio was not muxed")
        finally:
            for p in (tone, out):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def test_no_audio_source_video_only(self):
        from shared import video_utils
        fps, w, h = 30.0, 64, 64
        seg_f = [[0, 3], [8, 11]]
        kept = ce.EditorModule._kept_total(seg_f)                 # 8
        seg_s = [(s / fps, (e + 1) / fps) for s, e in seg_f]
        silent = synth.make_temp_clip()                          # video-only .avi
        out = tempfile.mktemp(suffix=".mp4", prefix="sc_seg_noaud_")
        try:
            writer = video_utils.H264PipeWriter(out, w, h, fps, audio_src=silent,
                                                segments=seg_s)
            self._pipe(writer, kept, w, h)
            ok, err = writer.close()
            self.assertTrue(ok, f"writer crashed on a silent source: {err}")
            self.assertEqual(self._count_frames(out), kept)
            self.assertFalse(video_utils.has_audio(out), "expected video-only output")
        finally:
            for p in (silent, out):
                try:
                    os.remove(p)
                except OSError:
                    pass


class TestInterpolate(unittest.TestCase):
    """interpolate_layout: empty→None, edge clamps, fluid midpoint, jump cut, and
    per-index highlight interpolation."""

    def _kfs(self, k2_type="fluid"):
        return [
            {"frame": 0, "type": "fluid", "game_box": [0, 0, 100, 100],
             "cam_box": [0, 0, 50, 50], "split_ratio": 40,
             "highlights": [{"src_box": [0, 0, 10, 10], "dest_box": [0, 0, 20, 20]}]},
            {"frame": 100, "type": k2_type, "game_box": [100, 100, 100, 100],
             "cam_box": [10, 10, 50, 50], "split_ratio": 60,
             "highlights": [{"src_box": [10, 10, 10, 10], "dest_box": [100, 100, 20, 20]}]},
        ]

    def test_empty_returns_none(self):
        self.assertIsNone(ce.interpolate_layout([], 5))

    def test_before_first_and_after_last_clamp(self):
        kfs = self._kfs()
        self.assertEqual(ce.interpolate_layout(kfs, -10)["game_box"], [0, 0, 100, 100])
        self.assertEqual(ce.interpolate_layout(kfs, 999)["game_box"], [100, 100, 100, 100])

    def test_fluid_midpoint(self):
        lay = ce.interpolate_layout(self._kfs(), 50)
        self.assertEqual(lay["game_box"], [50, 50, 100, 100])   # exact average
        self.assertEqual(lay["split_ratio"], 50)                # round(lerp(40,60,.5))
        self.assertEqual(lay["highlights"][0]["src_box"], [5, 5, 10, 10])

    def test_jump_holds_then_cuts(self):
        kfs = self._kfs(k2_type="jump")
        self.assertEqual(ce.interpolate_layout(kfs, 99)["game_box"], [0, 0, 100, 100])
        self.assertEqual(ce.interpolate_layout(kfs, 100)["game_box"], [100, 100, 100, 100])


class TestFilterVisible(unittest.TestCase):
    """filter_visible: ranged model entries gate by frame; None range = always; geom
    entries beyond the model are always visible (never raise - the 2.5 mismatch hazard)."""

    def test_ranges_and_overflow(self):
        geom = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        model = [{"visible": [10, 20]}, {"visible": None}]   # 3rd geom has no model entry
        at15 = ce.filter_visible(geom, model, 15)
        self.assertEqual([g["id"] for g in at15], ["a", "b", "c"])
        at5 = ce.filter_visible(geom, model, 5)
        self.assertEqual([g["id"] for g in at5], ["b", "c"])  # 'a' out of [10,20]
        self.assertIsInstance(at5, tuple)


class TestFrameCache(unittest.TestCase):
    def test_lru_eviction_and_touch(self):
        c = ce.FrameCache(maxsize=3)
        for k in (1, 2, 3):
            c.put(k, k * 10)
        self.assertEqual(c.get(1), 10)   # touch 1 → now most-recent
        c.put(4, 40)                     # evicts the LRU, which is now 2
        self.assertIsNone(c.get(2))
        self.assertEqual(c.get(1), 10)
        self.assertEqual(c.get(4), 40)


class TestProcessPriority(unittest.TestCase):
    def test_never_raises(self):
        ce._set_process_priority(True)    # boost then restore - no-op off-Windows
        ce._set_process_priority(False)


class TestPersistenceMath(unittest.TestCase):
    """The pure persistence helpers (the home of the 0..1 arithmetic + the per-file key)."""

    def test_norm_denorm_roundtrip(self):
        box = [12.0, 34.0, 56.0, 78.0]
        n = ce.EditorModule._norm_box(box, 300, 200)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in n))
        back = ce.EditorModule._denorm_box(n, 300, 200)
        for a, b in zip(box, back):
            self.assertAlmostEqual(a, b, places=9)

    def test_clip_key_stable_and_case_insensitive(self):
        k1 = ce.EditorModule._clip_key(r"C:\Clips\Boss.mp4")
        self.assertEqual(k1, ce.EditorModule._clip_key(r"C:\Clips\Boss.mp4"))
        self.assertEqual(len(k1), 16)
        if os.name == "nt":   # normcase lowercases on Windows
            self.assertEqual(k1, ce.EditorModule._clip_key(r"c:\clips\boss.mp4"))


class TestBusHandoff(unittest.TestCase):
    """The workflow chain raw_clip → edited_clip → final_file flows through the bus:
    publish records latest, subscribers fire, request_handoff routes the latest path."""

    def test_chain(self):
        bus = MessageBus()
        seen = []
        bus.subscribe("edited_clip", lambda p: seen.append(p))
        routed = []
        bus.set_handoff_handler(lambda dt, tgt, p: routed.append((dt, tgt, p)))

        bus.publish("raw_clip", "raw.mp4")
        self.assertEqual(bus.latest("raw_clip"), "raw.mp4")

        bus.publish("edited_clip", "edited.mp4")
        self.assertEqual(bus.latest("edited_clip"), "edited.mp4")
        self.assertEqual(seen, ["edited.mp4"])              # subscriber notified

        bus.request_handoff("edited_clip", "exporter")      # user "Send to Export"
        self.assertEqual(routed, [("edited_clip", "exporter", "edited.mp4")])

        bus.publish("final_file", "out.mp4")
        self.assertEqual(bus.latest("final_file"), "out.mp4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
