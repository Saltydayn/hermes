"""Phase 5.5.2a - the pure UndoStack (no Tk, no editor)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.editor.undo import UndoStack   # noqa: E402


def _snap(i):
    return {"n": i, "keyframes": [i]}


class TestUndoStack(unittest.TestCase):
    def test_round_trip_exact_dicts(self):
        st = UndoStack()
        a, b, c = _snap(0), _snap(1), _snap(2)
        st.record(a)          # state moved a -> b
        st.record(b)          # state moved b -> c; current is c
        self.assertIs(st.undo(c), b)
        self.assertIs(st.undo(b), a)
        self.assertIs(st.redo(a), b)
        self.assertIs(st.redo(b), c)
        self.assertIsNone(st.redo(c))

    def test_record_clears_redo(self):
        st = UndoStack()
        a, b = _snap(0), _snap(1)
        st.record(a)
        self.assertIs(st.undo(b), a)
        self.assertTrue(st.can_redo)
        st.record(a)          # a new edit forks history
        self.assertFalse(st.can_redo)

    def test_cap_evicts_oldest(self):
        st = UndoStack(cap=50)
        snaps = [_snap(i) for i in range(60)]
        for s in snaps:
            st.record(s)
        self.assertEqual(len(st._undo), 50)
        self.assertIs(st._undo[0], snaps[10])   # 0..9 evicted
        self.assertIs(st._undo[-1], snaps[59])

    def test_empty_and_flags(self):
        st = UndoStack()
        self.assertIsNone(st.undo(_snap(0)))
        self.assertFalse(st.can_undo)
        self.assertFalse(st.can_redo)   # a failed undo must not seed redo
        st.record(_snap(0))
        self.assertTrue(st.can_undo)
        st.undo(_snap(1))
        self.assertFalse(st.can_undo)
        self.assertTrue(st.can_redo)

    def test_reset_clears_both(self):
        st = UndoStack()
        st.record(_snap(0))
        st.undo(_snap(1))
        st.reset(_snap(2))
        self.assertFalse(st.can_undo)
        self.assertFalse(st.can_redo)


if __name__ == "__main__":
    unittest.main(verbosity=2)
