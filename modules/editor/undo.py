"""Undo/redo stack for the editor's edit model (5.5.2a).

Pure and Tk-free: two lists of opaque snapshot dicts. The stack never inspects a
snapshot, so shape changes (segment gains, future keys) ride through untouched.
Ownership contract: snapshots are treated as immutable after creation; the editor's
_snapshot_clip_edits builds all-fresh structures, so no copying happens here.
"""


class UndoStack:
    """Snapshot-based undo/redo. record(prev) pushes the settled PRE-mutation state;
    undo/redo exchange the caller's CURRENT snapshot for the popped one, so the
    caller always holds the live state and the stack holds everything else."""

    def __init__(self, cap=50):
        self.cap = int(cap)
        self._undo = []
        self._redo = []

    @property
    def can_undo(self):
        return bool(self._undo)

    @property
    def can_redo(self):
        return bool(self._redo)

    def reset(self, snapshot=None):
        """New clip: clear both stacks. `snapshot` is accepted for call-site symmetry
        (the editor tracks its own current state); the stack keeps nothing."""
        self._undo.clear()
        self._redo.clear()

    def record(self, prev_snapshot):
        """Push the pre-mutation snapshot; any new edit invalidates the redo branch.
        Past the cap the oldest step falls off."""
        self._undo.append(prev_snapshot)
        if len(self._undo) > self.cap:
            del self._undo[0]
        self._redo.clear()

    def undo(self, current):
        """The snapshot to restore, or None when nothing is undoable. `current` (the
        live state) moves onto the redo branch."""
        if not self._undo:
            return None
        self._redo.append(current)
        return self._undo.pop()

    def redo(self, current):
        """The snapshot to restore, or None when nothing is redoable. `current`
        moves back onto the undo branch."""
        if not self._redo:
            return None
        self._undo.append(current)
        return self._redo.pop()
