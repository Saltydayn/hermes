"""Editor-internal package (Phase 2.9.2).

Stateless, reusable pieces extracted out of the ~3,400-line modules/clip_editor.py so the
UI class shrinks and the proven logic can be tested in isolation. NOTHING here reads a
widget or a thread-shared object - every function is pure (inputs in, value out), safe to
call from the render/playback worker threads.

This is clip_editor's OWN private package, not a peer module: only `modules.clip_editor`
imports it, and it imports only stdlib + third-party (cv2/numpy). The architecture's
"modules never import each other" rule is about the peer tabs (home/importer/exporter/…),
which this is not. Compositor stays editor-internal for now - promote a piece to shared/
only if a second module (e.g. Phase 3 subtitle burn-in) actually needs it.
"""
