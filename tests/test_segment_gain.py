"""Phase 5.5.2b - per-segment audio gain: normalize rules + filtergraph strings.

All pure and headless: the normalizer is a static on EditorModule, the filtergraph a
static on H264PipeWriter (no process is spawned here).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modules.clip_editor as ce                    # noqa: E402
from shared.video_utils import H264PipeWriter      # noqa: E402

norm = ce.EditorModule._normalize_segments
graph = H264PipeWriter._segment_filtergraph


class TestNormalizeGains(unittest.TestCase):
    def test_legacy_two_length_reads_as_zero(self):
        self.assertEqual(norm([[2, 5], [8, 9]], 20), [[2, 5, 0.0], [8, 9, 0.0]])

    def test_equal_gain_merge_keeps_left(self):
        self.assertEqual(norm([[0, 4, 2.0], [5, 9, 2.0]], 20), [[0, 9, 2.0]])
        self.assertEqual(norm([[0, 5, 2.0], [3, 9, 2.0]], 20), [[0, 9, 2.0]])

    def test_differing_gains_do_not_merge(self):
        self.assertEqual(norm([[0, 4, 2.0], [5, 9, -1.0]], 20),
                         [[0, 4, 2.0], [5, 9, -1.0]])
        # Zero vs nonzero are also distinct regions.
        self.assertEqual(norm([[0, 4], [5, 9, 6.0]], 20),
                         [[0, 4, 0.0], [5, 9, 6.0]])

    def test_differing_gain_overlap_clips(self):
        # A true overlap with differing gains keeps the no-overlap invariant: the
        # later range clips to start after the earlier one ends.
        self.assertEqual(norm([[0, 5, 2.0], [3, 9, -1.0]], 20),
                         [[0, 5, 2.0], [6, 9, -1.0]])
        # Fully swallowed -> dropped.
        self.assertEqual(norm([[0, 9, 2.0], [3, 5, -1.0]], 20), [[0, 9, 2.0]])

    def test_gain_clamps(self):
        self.assertEqual(norm([[0, 5, 99.0]], 20), [[0, 5, 30.0]])
        self.assertEqual(norm([[0, 5, -99.0]], 20), [[0, 5, -30.0]])


class TestFiltergraphStrings(unittest.TestCase):
    def test_all_zero_offsets_identical_to_before(self):
        # The pre-5.5.2b graph, byte for byte (the no-regression anchor).
        self.assertEqual(
            graph([(0, 1, 0.0), (2, 3, 0.0)], 0),
            "[1:a]atrim=start=0.000000:end=1.000000,asetpts=N/SR/TB[a0];"
            "[1:a]atrim=start=2.000000:end=3.000000,asetpts=N/SR/TB[a1];"
            "[a0][a1]concat=n=2:v=0:a=1[ac];"
            "[ac]apad[aout]")
        # 2-tuples (old callers) produce the same graph.
        self.assertEqual(graph([(0, 1, 0.0), (2, 3, 0.0)], 0),
                         graph([(0, 1), (2, 3)], 0))

    def test_offset_branch_plus_post_concat_gain(self):
        self.assertEqual(
            graph([(0, 1, 3.0), (2, 3, 0.0)], -2),
            "[1:a]atrim=start=0.000000:end=1.000000,asetpts=N/SR/TB,volume=3.0dB[a0];"
            "[1:a]atrim=start=2.000000:end=3.000000,asetpts=N/SR/TB[a1];"
            "[a0][a1]concat=n=2:v=0:a=1[ac];"
            "[ac]volume=-2dB,apad[aout]")


if __name__ == "__main__":
    unittest.main(verbosity=2)
