"""Phase 5.5.1d - the share compressor: exact bitrate math + a real re-encode.

compress_kbps is pure (exact-value asserts). compress_to_size runs ffmpeg against a
synthetic clip; that part self-skips when ffmpeg is absent so CI never false-fails.
Also covers the side-hit zone helper (pure static geometry, no Tk).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import video_utils              # noqa: E402
from tests import synth                      # noqa: E402


class TestCompressKbps(unittest.TestCase):
    def test_exact_values(self):
        # 10*8000*0.92/60 - 96 = 1130.67 -> int 1130
        self.assertEqual(video_utils.compress_kbps(10, 60), 1130)
        self.assertEqual(video_utils.compress_kbps(10, 60, audio_kbps=128), 1098)

    def test_quality_floor(self):
        # 5*8000*0.92/600 - 96 = -34.7 -> below the 150 kbps floor
        self.assertIsNone(video_utils.compress_kbps(5, 600))
        # Exactly at the floor passes.
        # video = 150 when target_mb*8000*0.92/dur = 246 -> dur = target*8000*0.92/246
        dur = 10 * 8000 * 0.92 / 246.0
        self.assertEqual(video_utils.compress_kbps(10, dur), 150)

    def test_bad_duration(self):
        self.assertIsNone(video_utils.compress_kbps(10, 0))
        self.assertIsNone(video_utils.compress_kbps(10, -5))


class TestCompressToSize(unittest.TestCase):
    def test_synthetic_clip_fits_target(self):
        if video_utils.find_ffmpeg() is None:
            raise unittest.SkipTest("ffmpeg unavailable")
        try:
            clip = synth.make_temp_clip()
        except RuntimeError as ex:
            raise unittest.SkipTest(str(ex))
        out = clip + "_c.mp4"
        try:
            meta = video_utils.probe_video(clip)
            self.assertIsNotNone(meta)
            _w, _h, fps, n = meta
            target_mb = 1
            kbps = video_utils.compress_kbps(target_mb, n / fps)
            self.assertIsNotNone(kbps)
            ok, err = video_utils.compress_to_size(clip, out, kbps)
            self.assertTrue(ok, err)
            self.assertTrue(os.path.exists(out))
            self.assertIsNotNone(video_utils.probe_video(out),
                                 "compressed file unreadable")
            self.assertLessEqual(os.path.getsize(out), target_mb * 1_000_000)
        finally:
            for p in (clip, out):
                try:
                    os.remove(p)
                except OSError:
                    pass


class TestSideHit(unittest.TestCase):
    """The pure side-band hit test (corner zones excluded; CORNER_TOL = 12)."""

    def setUp(self):
        import modules.clip_editor as ce
        self.hit = ce.EditorModule._side_hit
        self.corner = ce.EditorModule._corner_hit
        self.box = [100.0, 100.0, 200.0, 100.0]   # x 100..300, y 100..200

    def test_side_bands(self):
        self.assertEqual(self.hit(self.box, 300, 150), "e")
        self.assertEqual(self.hit(self.box, 100, 150), "w")
        self.assertEqual(self.hit(self.box, 200, 100), "n")
        self.assertEqual(self.hit(self.box, 200, 200), "s")

    def test_corner_zone_excluded(self):
        # (300, 105) is inside the NE corner zone -> the side test must not claim it.
        self.assertIsNone(self.hit(self.box, 300, 105))
        self.assertEqual(self.corner(self.box, 300, 105), "ne")

    def test_interior_and_outside(self):
        self.assertIsNone(self.hit(self.box, 200, 150))   # deep interior
        self.assertIsNone(self.hit(self.box, 330, 150))   # beyond the tolerance


if __name__ == "__main__":
    unittest.main(verbosity=2)
