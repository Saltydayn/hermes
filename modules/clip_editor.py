"""Clip Editor - unified view (Phase 2.1 shell + 2.2 layout/compositor).

ONE view, DaVinci-style: source + preview viewers and a scrolling inspector on top, a
single timeline pinned across the bottom. 2.1 stood up the skeleton (persistent
VideoCapture + LRU frame cache, frame-accurate scrub, tiered keyboard nav). 2.2 adds the
real compositor: movable + corner-resizable game/cam crop boxes (aspect locked to their
output panel), split + preview-draggable seam, cosine seam-melt, watermark/name burn
(platform icons row/stacked + pickable colors), independent branding/watermark image
overlays (5.5.1g, list-based like highlights - logo, sponsor image, avatar, etc., with
free or aspect-locked resize and opacity), solid border, a live
~80ms-debounced 9:16 preview, and persistence (last-used layout + 5 saved slots). 2.3
replaces the placeholder timeline with a frame-accurate keyframe track: fluid (linear
interp) / jump (hold-then-cut) keyframes keyed by integer frame, editable markers
(left-click select+seek, right-drag retime with snap), K / Shift+K / Delete keybinds.
Keyframes are per-clip and in-memory (no persistence in v1, per spec). 2.4 adds silent
playback: an `after`-clock loop on the UI thread (NO threads) with a transport play/pause
button, a Space matrix (Space=edits · Shift=raw · Ctrl=raw+loop · Ctrl+Shift=edits+loop),
and a "True speed (drop frames)" toggle that holds real-time tempo by jumping to the
wall-clock-derived frame. Playback composites at PREVIEW resolution and per tick touches
only the preview canvas, the timeline playhead, the counter, and the scrub thumb. 2.5
adds Highlight boxes (PiP): each = a src_box cropped on the source (CANVAS space, orange
guide) + a dest_box placed on the preview (OUTPUT 1080×1920 space, aspect-locked to the
source crop, clamped). Multiple supported ("+ Add" warns gently past the first); the
compositor's `highlights=` loop crops→resizes→pastes with dest mapped proportionally to
the actual out size; keyframes capture and interpolate highlights by index; layouts
persist them (src normalized over the display, dest over the output). 2.6 makes the
editor PRODUCE: In/Out trim (buttons + I/O keys, shaded on the timeline), a whole-clip
±dB render gain, an off-thread audio waveform lane, and a threaded render - a daemon
worker with its OWN capture composites every frame In..Out from snapshotted params
(pure `interpolate_layout` + `composite_frame`, zero widget reads) and pipes raw BGR
into ONE ffmpeg H.264 encode that muxes the trimmed/gained source audio in the same
pass (`shared.video_utils.H264PipeWriter`), with progress, cancel (partial removed),
and the shared lamp. Publishes `edited_clip` on success. 2.8.2 persists the FULL edit
model per clip - layout + keyframes + trim + gain in one config block keyed by a path
hash, LRU-capped at 25, debounce-saved on every edit, flushed on close/clip switch,
and restored (keyframes re-materialized at the current display size) when the same
clip loads again; `last_layout` still seeds clips that have no block. 2.8.4 adds the
Performance render path: decode → bounded compositor pool → strictly ordered writes
(plus above-normal process priority), behind the global Eco/Performance mode on Home
(config `performance_mode`, default Eco = the proven serial loop at normal priority);
both paths share `_compose_render_frame` and produce bit-identical frame sequences.
2.8.6 gives playback SOUND: the waveform worker keeps the PCM it already decodes, a
sounddevice OutputStream opens per play, and `_play_tick`'s clock is the stream's
sample cursor - video chases audio, A/V sync for free. Live dB gain + Mute reach the
PortAudio callback as plain floats (threads never touch tkinter). The true-speed
checkbox is gone (always true-speed); perf_counter survives only as the silent
fallback - every audio failure (no sounddevice / no device / no track / device death
mid-play) degrades to the 2.4 silent playback, never breaks the editor. 2.8.7 gives
each highlight PiP a visible range `[from, to]` (None = whole clip): pure
`filter_visible` runs AFTER interpolation at every composite call site - the
compositor and keyframe model untouched; ranges persist in the 2.8.2 block, preset
slots strip them (clip-agnostic). 2.8.8 closes the phase with polish: the watermark
plate parks Left/Center/Right on the seam (a "position" key in the dict, default
center - old dicts render unchanged), crop/dest boxes show corner resize ticks
(affordance only - hit-testing untouched), and `?` opens a keyboard cheat-sheet
built from the ACTUAL _on_key map.

`composite_frame(...)` is a PURE module-level function - inputs in, frame out, zero
widget reads - so 2.4 (playback) and 2.6 (render thread) can call it from anywhere.
Box coordinates are CANVAS space (relative to the displayed source image; the image is
centered, so mouse events subtract `off_x/off_y` first) and the compositor multiplies by
`scale_x/scale_y` to reach source pixels. Layouts persist boxes NORMALIZED (0..1 of the
displayed image) so a layout saved on a maximized window restores correctly on a small one.

Consumes `raw_clip` from the bus (lazy pull on first visibility + receive_handoff).
Produces nothing yet - `edited_clip` arrives in 2.6. Imports ONLY from shared/, stdlib,
and third-party (modules never import each other).
"""

import copy
import hashlib
import os
import queue
import re
import tempfile
import threading
import time
import tkinter as tk
import wave
from time import perf_counter
from tkinter import colorchooser, filedialog, messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

try:
    import sounddevice as sd
except Exception:  # noqa: BLE001 - a PortAudio DLL load failure isn't ImportError
    sd = None      # permanent silent fallback - the editor must work without audio

from shared import video_utils
from shared.ui_helpers import (
    PALETTE, HelpDialog, ScrollableFrame, Tooltip, TutorialIntro, TutorialReplayButton,
    TutorialWalkthrough, bind_scale_jump, draw_lamp, mono_font, show_toast, themed_button,
    ui_font,
)
# Editor-internal package (2.9.2): the PURE stateless logic this UI class drives. NOT a
# peer module - modules/editor/ is clip_editor's own private package (the "modules never
# import each other" rule is about the peer tabs). Re-imported into this namespace so every
# existing call site (composite_frame(...), interpolate_layout(...), OUT_W, …) is unchanged.
from modules.editor.compositor import (   # noqa: F401 - _hex_to_bgr/_platform_icon used by UI
    OUT_W, OUT_H, composite_frame, _hex_to_bgr, _platform_icon,
)
from modules.editor.framecache import FrameCache
from modules.editor.interp import filter_visible, interpolate_layout
from modules.editor.sysutil import _set_process_priority
from modules.editor.undo import UndoStack

INSPECTOR_W = 320      # inspector column width (px)
TIMELINE_H = 80        # placeholder timeline lane height (px)
WARM_RADIUS = 15       # frames pre-decoded around the playhead after a load
SEQ_GRAB_MAX = 12      # forward gap (frames) bridged by grab() instead of a cap.set seek
# CACHE_FRAMES → modules/editor/framecache.py; OUT_W/OUT_H → modules/editor/compositor.py
# (imported above so this module's globals are unchanged).
MIN_SLOTS = 5          # layout-slot floor (5.5.2e: the list is now extendable, no max)
CLIP_EDITS_CAP = 25    # per-clip edit blocks kept in config (LRU by "saved" stamp)
MIN_BOX_W = 24         # smallest corner-drag box width (canvas px)
CORNER_TOL = 12        # px around a box corner that counts as a resize grab

# Output-content color defaults (the old app's proven values). User-overridable per
# layout via the inspector color pickers - these color the RENDERED video, not the UI,
# so they deliberately don't come from PALETTE.
DEFAULT_BORDER_HEX = "#0f0f0f"
DEFAULT_TEXT_HEX = "#ffffff"
DEFAULT_PLATE_HEX = "#0c0c0c"

_CORNER_CURSORS = {"nw": "top_left_corner", "ne": "top_right_corner",
                   "sw": "bottom_left_corner", "se": "bottom_right_corner"}
# 5.5.1d: side-edge resize. Sides share the corner hit slot (one-letter names) and
# get stock double-arrow cursors; corners keep hit-test priority.
_SIDE_CURSORS = {"e": "sb_h_double_arrow", "w": "sb_h_double_arrow",
                 "n": "sb_v_double_arrow", "s": "sb_v_double_arrow"}
_RESIZE_CURSORS = {**_CORNER_CURSORS, **_SIDE_CURSORS}

# Inspector sections collapsed on first run (5.5.1a). Saved state in config
# editor.inspector_collapsed overrides these per section key.
_COLLAPSED_DEFAULT = {"branding", "content_boxes", "layouts", "audio"}


def _section_key(title):
    """Stable config key for an inspector section title (alnum runs joined by _)."""
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def _initial_collapsed(key, saved):
    """Whether section `key` starts collapsed, given the saved inspector_collapsed
    dict (saved state wins; missing keys fall back to the first-run defaults)."""
    v = saved.get(key)
    return bool(v) if v is not None else key in _COLLAPSED_DEFAULT


def slot_delete_check(presets):
    """Can the LAST layout slot be deleted (5.5.2e)? Returns
    (allowed, needs_confirm, name):
    - allowed=False at the MIN_SLOTS floor (never drop below five).
    - needs_confirm=True when the last slot holds a saved preset (name = its display
      name) so the UI can ask before discarding it.
    Pure: reads the presets list only, never touches config or widgets."""
    count = max(MIN_SLOTS, len(presets))
    if count <= MIN_SLOTS:
        return False, False, None
    last = presets[count - 1] if count - 1 < len(presets) else None
    if last:
        return True, True, last.get("name", f"Slot {count}")
    return True, False, None


# Timeline geometry (the 80px lane from 2.1; the lower band is reserved for 2.6 audio).
TL_PAD = 40            # left/right margin inside the timeline canvas
TL_TRACK_H = 34        # top band: the keyframe track
TL_MARKER_Y = 16       # marker center line inside the track
TL_RULER_Y = 30        # ruler baseline with second ticks
TL_HIT_PX = 6          # marker grab / snap tolerance (px)


class EditorModule(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.bus = app.bus

        # ── Video state (later specs read these - keep the names stable) ──
        self._cap = None                  # persistent cv2.VideoCapture (ONE per loaded clip)
        self._cap_pos = -1                # frame index the capture will decode next (-1 = unknown)
        self._cache = FrameCache()
        self._path = None
        self.orig_w, self.orig_h = 1, 1
        self.fps = 30.0
        self.total_frames = 0
        self.frame_idx = 0
        self.scale_x, self.scale_y = 1.0, 1.0   # source px per displayed px (canvas→source)
        self._source_disp = None          # PIL image currently shown in the source viewer

        # ── Layout state (2.2) ──
        self.game_box = None              # [x,y,w,h] floats, canvas space (image-local)
        self.cam_box = None
        self._disp_w = self._disp_h = 0   # displayed source image size
        self.off_x = self.off_y = 0       # where the centered image sits in the canvas
        self._pending_norm = None         # normalized boxes awaiting a known display size
        self._active_box = None           # "game" | "cam" while dragging
        self._resize_corner = None        # "nw"/"ne"/"sw"/"se" while corner-resizing
        self._drag_x = self._drag_y = 0
        # ── Branding / watermark image overlays (5.5.1g: generalized from the single
        # 5.5.1f avatar into a list, like highlights - logo, sponsor image, avatar,
        # etc.). Each: {"path", "bgra", "box" (OUTPUT space [x,y,w,h]), "lock_aspect",
        # "opacity", "_cache" (private resize memo, never persisted)}.
        self.branding_items = []
        self.sel_brand = None             # selected index, or None
        self._pv_brand_drag = False       # dest-box drag in progress on the preview
        self._pv_brand_corner = None      # corner/side being resized, or None (= move)
        self._border_hex = DEFAULT_BORDER_HEX   # output colors (hex; user-pickable)
        self._text_hex = DEFAULT_TEXT_HEX
        self._plate_hex = DEFAULT_PLATE_HEX
        self._swatches = {}               # attr name -> swatch button (for re-tinting)

        # ── Preview state ──
        self._pv_off_x = self._pv_off_y = 0     # where the composite sits in the preview
        self._pv_w, self._pv_h = 1, 1           # displayed composite size
        self._pv_seam_drag = False

        # ── Highlights (2.5: PiP boxes) ──
        self.highlights = []              # [{"src_box": canvas space, "dest_box": OUTPUT space}]
        self.sel_hl = None                # selected index (editable/bright), or None
        self._pending_hl_norm = None      # normalized highlights awaiting a display size
        self._pv_hl_drag = False          # dest-box drag in progress on the preview
        self._pv_hl_corner = None         # corner being resized, or None (= move)
        self._pv_drag_x = self._pv_drag_y = 0
        self._hl_vis_label = None         # selected PiP's visibility label (2.8.7)

        # ── Keyframes / timeline (2.3; persisted per clip since 2.8.2) ──
        self.keyframes = []               # sorted by "frame", unique per frame
        self._pending_kf_norm = None      # normalized keyframes awaiting a display size
        self.selected_kf = None           # dict in self.keyframes, or None
        self._tl_drag_kf = None           # keyframe being right-drag retimed
        self._tl_scrubbing = False        # left-drag scrub on the empty track
        self._tl_tick_step = 1            # seconds between ruler ticks (set on redraw)
        self._tl_configure_job = None

        # ── Playback (2.4; `after`-clock on the UI thread - NO threads) ──
        self._playing = False
        self._play_job = None             # after-id of the next tick
        self._play_clock0 = 0.0           # perf_counter at (re)start - SILENT-fallback clock
        self._play_frame0 = 0             # frame playback started on (loop return point)
        self._play_edits = True           # composite with edits vs raw source
        self._play_loop = False           # loop back to _play_frame0 at the end
        self._play_trim = False           # 2.9.3: snapshot of "In→Out only" at play start
        # 2.10.2c: when the edits playback spans MULTIPLE kept segments, the clocks run on
        # the OUTPUT (kept) timeline and map to source via the 2.10.2a mappers. One segment
        # (or raw playback) → the K==1 path below, provably identical to 2.9.3.
        self._play_multiseg = False       # snapshot at play start (edits AND >1 segment)
        self._play_output0 = 0            # OUTPUT index playback started on (multiseg)

        # ── Audible playback (2.8.6; video chases the audio stream's sample clock) ──
        self._stream = None               # sd.OutputStream while playing w/ audio, else None
        self._audio_token = None          # identity of the CURRENT stream (stale-death guard)
        self._pcm = None                  # int16 (n, ch) decoded by the waveform worker
        self._play_pcm = None             # 2.10.2c kept-PCM (concatenated kept ranges), cached
        self._play_pcm_key = None         # the segments tuple _play_pcm was built for
        self._active_pcm = None           # the buffer the current stream plays (_pcm|_play_pcm)
        self._pcm_rate = 0
        self._audio_pos = 0               # sample cursor (advanced by the PortAudio callback)
        self._audio_seek = None           # one-slot seek, UI thread → callback (loop wrap)
        self._audio_start_sample = 0      # cursor at play start - the clock's zero
        self._muted = False               # plain floats/bools for the callback - no widget reads
        self._gain_lin = 1.0

        # ── Trim / waveform / render (2.6) ──
        # 2.10.2a: the trim is now the outer bounds of an ordered list of KEPT source-frame
        # ranges. `segments` is the single source of truth; in_frame/out_frame are derived
        # read-only properties. One segment = today's simple In/Out trim (byte-for-byte).
        self.segments = [[0, 0, 0.0]]     # kept [start, end, gain_db] ranges (5.5.2b)
        self._sel_seg = None              # timeline-selected segment index (5.5.2b)
        self._seg_gain_lin = 1.0          # selected-playback segment factor (callback)
        self._cut_start = None            # 2.10.2d: pending one-button cut - the marked
        #                                   start frame (None = no cut in progress). The
        #                                   second Cut press removes [start .. playhead].
        self._wave_peaks = None           # np.float32 0..1 peaks (fixed buckets) | None
        self._wave_state = "none"         # none | loading | ready | noaudio
        self._wave_token = None           # stale-load guard for the peak thread
        self._rendering = False
        self._render_state = "none"       # none | unrendered | stale | rendered (5.5.1c)

        # ── Undo/redo (5.5.2a: snapshot-based, per clip, Y/X) ──
        self._undo_stack = UndoStack(cap=50)
        self._undo_current = None         # the last SETTLED snapshot (pre-mutation state)
        self._undo_restoring = False      # guards _mark_clip_dirty during a restore
        self._undo_coalesce_job = None    # 600ms settle timer (drags = one step)
        self._render_cancel = None        # threading.Event while a render runs
        self._render_writer = None        # H264PipeWriter (for abort-on-close)
        self._closing = False             # marshal guard during teardown
        self._ui_queue = queue.Queue()    # worker→UI results (see _marshal)
        self._ui_pump_job = None

        # ── UI plumbing ──
        self._source_photo = None         # ImageTk refs (Tk GCs the image without these)
        self._preview_photo = None
        self._preview_photo_wh = (0, 0)   # size of the current _preview_photo, for reuse
        self._preview_image_item = None   # persistent preview canvas image item id
        self._syncing_scrub = False       # guards scale.set() round-trips
        self._scrub_echo = None           # last programmatic scrub value (echo filter)
        self._seek_job = None             # throttled-seek job (drag-scrub, see _schedule_seek)
        self._pending_seek_idx = None
        self._pending_seek_from_scrub = False
        self._preview_fast = False        # next preview pass composites at preview res
        self._applying = False            # guards control callbacks during _apply_layout
        self._resize_job = None
        self._render_retry = None
        self._preview_job = None
        self._settle_job = None           # debounced full-quality pass, see _schedule_preview_update
        self._box_draw_job = None
        self._autosave_job = None
        self._clipsave_job = None         # debounced per-clip edit-block save (2.8.2)
        self._warm_queue = []
        self._warm_job = None

        self._build()
        self.bind("<Configure>", self._on_resize)
        # Lazy pull: adopt the latest raw_clip the first time the tab becomes visible.
        self.bind("<Visibility>", self._on_visible)
        # Tab switched away (notebook unmaps the frame): halt playback.
        self.bind("<Unmap>", lambda _e: self._stop_play())
        # Worker→UI results pump (waveform peaks, render progress/done - see _marshal).
        self._ui_pump_job = self.after(50, self._ui_pump)

    # ── UI construction ──────────────────────────────────────────────────────────────
    def _build(self):
        # Bottom first (side="bottom") so the timeline stays pinned when the window shrinks.
        bottom = tk.Frame(self, bg=PALETTE["bg"])
        bottom.pack(side="bottom", fill="x")

        # Action bar splits into TWO rows (5.5.4c correction round: one row overflowed on
        # narrower windows - Tk's packer has no wrap, so buttons past the edge were
        # simply clipped with no scrollbar). Row 1 keeps the editing actions (undo/redo,
        # Trim+Cut group, Keyframes group), each with a visible `?` usage help. Row 2,
        # directly below, holds the lamp + status (still absorbs the squeeze) and
        # everything that used to pack side="right" on the single row: RENDER · state
        # readout · Keybinds · Reload · the playback cluster (Loop · In→Out only · Mute ·
        # ▶ · counter). Right side still packs first so a narrow window clips status
        # before the transport.
        row = tk.Frame(bottom, bg=PALETTE["bg"])
        row.pack(fill="x", padx=10, pady=(2, 2))
        row2 = tk.Frame(bottom, bg=PALETTE["bg"])
        row2.pack(fill="x", padx=10, pady=(0, 4))

        def vsep(parent):
            tk.Frame(parent, width=1, bg=PALETTE["border"]).pack(
                side="left", fill="y", padx=10, pady=4)

        # THE render button (5.5.1a; same attribute so the _start_render/_render_done
        # state flips keep working). Far right, visually prominent.
        self._render_btn = themed_button(row2, "RENDER", self._start_render,
                                         kind="primary")
        self._render_btn.configure(font=ui_font(10, "bold"), padx=18, pady=6)
        self._render_btn.pack(side="right", padx=(8, 0), pady=2)
        Tooltip(self._render_btn, "Render the edit to a vertical short in data/Shorts.")
        # Render-state readout: placeholder only, 5.5.1c wires text and colors.
        self._render_state_label = tk.Label(row2, text="", bg=PALETTE["bg"],
                                            fg=PALETTE["text_dim"], font=mono_font(9))
        self._render_state_label.pack(side="right", padx=(8, 0))
        self._keys_btn = themed_button(row2, "Keybinds", self._show_cheatsheet, kind="neutral")
        self._keys_btn.pack(side="right", padx=(8, 0), pady=2)
        Tooltip(self._keys_btn, "All keyboard shortcuts.")
        reload_btn = themed_button(row2, "Reload from import", self._reload_from_import,
                                   kind="neutral")
        reload_btn.pack(side="right", padx=(8, 0), pady=2)
        Tooltip(reload_btn, "Reload the clip most recently sent from the Import tab.")
        # Playback cluster (side="right" reverses visually: on screen it reads
        # counter · ▶ · Mute · In→Out only · Loop). Play/pause = plain playback (edits);
        # loop follows the Loop toggle (the Space matrix's Ctrl still forces loop
        # regardless). Both toggles are config-backed so they persist.
        self._loop_var = tk.BooleanVar(
            value=bool((self.app.config.get("editor") or {}).get("play_loop", False)))
        loop_cb = ttk.Checkbutton(row2, text="Loop", variable=self._loop_var,
                                  command=self._on_loop_toggle)
        loop_cb.pack(side="right", padx=(10, 18))
        Tooltip(loop_cb, "Keep playback repeating so you can watch and hear it on a loop "
                         "(Play and Space loop continuously). Takes effect on the next Play.")
        # 2.9.3: restrict playback to the In/Out trim window (loop wraps to In, stop
        # clamps to Out). Off = full clip. Config-backed global pref.
        self._play_trim_var = tk.BooleanVar(
            value=bool((self.app.config.get("editor") or {}).get("play_in_out_only", False)))
        self._play_trim_cb = ttk.Checkbutton(row2, text="In→Out only",
                                             variable=self._play_trim_var,
                                             command=self._on_play_trim_toggle)
        self._play_trim_cb.pack(side="right", padx=(10, 0))
        Tooltip(self._play_trim_cb, "Restrict playback to the In→Out window (loops to In, "
                                    "stops at Out). Off = play the whole clip.")
        self._mute_var = tk.BooleanVar(value=False)
        mute_cb = ttk.Checkbutton(row2, text="Mute", variable=self._mute_var,
                                  command=self._on_mute_toggle)
        mute_cb.pack(side="right", padx=(10, 0))
        Tooltip(mute_cb, "Silence preview playback. The rendered audio is unaffected.")
        self._play_btn = themed_button(
            row2, "▶", lambda: self._toggle_play(True, self._loop_var.get()),
            kind="primary", width=2)
        self._play_btn.pack(side="right", padx=(10, 0))
        self._counter = tk.Label(row2, text="0.00s / 0.00s   F:0", bg=PALETTE["bg"],
                                 fg=PALETTE["text_dim"], font=mono_font(10))
        self._counter.pack(side="right", padx=(12, 0))

        # Undo/redo pair (5.5.2a), leftmost.
        self._undo_btn = themed_button(row, "↶", self._do_undo, kind="neutral",
                                       width=2, disabledforeground=PALETTE["text_faint"])
        self._undo_btn.pack(side="left", pady=2)
        Tooltip(self._undo_btn, f"Undo ({self._undo_key().upper()})")
        self._redo_btn = themed_button(row, "↷", self._do_redo, kind="neutral",
                                       width=2, disabledforeground=PALETTE["text_faint"])
        self._redo_btn.pack(side="left", padx=(4, 0), pady=2)
        Tooltip(self._redo_btn, "Redo (X)")
        self._refresh_undo_buttons()
        vsep(row)

        # Trim + Cut group.
        btn = themed_button(row, "Set In (I)", self.set_in_point, kind="neutral")
        btn.pack(side="left", pady=2)
        Tooltip(btn, "Trim start: the render keeps the clip from this frame on.")
        btn = themed_button(row, "Set Out (O)", self.set_out_point, kind="neutral")
        btn.pack(side="left", padx=(6, 0), pady=2)
        Tooltip(btn, "Trim end: the render keeps the clip up to this frame.")
        btn = themed_button(row, "Reset", self.reset_trim, kind="neutral")
        btn.pack(side="left", padx=(6, 0), pady=2)
        Tooltip(btn, "Restore the whole clip: clears In/Out and every cut.")
        hbtn = themed_button(
            row, "?",
            lambda: HelpDialog(self, title="Trim & Reset", body=self._HELP_TRIM),
            kind="info", width=2)
        hbtn.pack(side="left", padx=(6, 0), pady=2)
        Tooltip(hbtn, "What Set In/Out and Reset do")
        self._cut_btn = themed_button(row, "Cut (V)", self._toggle_cut, kind="danger")
        self._cut_btn.pack(side="left", padx=(10, 0), pady=2)
        Tooltip(self._cut_btn,
                "Press once to mark the cut start at the playhead, move, then press "
                "again to remove that range. Press without moving to cancel.")
        btn = themed_button(row, "Mark Audio (C)", self._mark_audio, kind="neutral")
        btn.pack(side="left", padx=(6, 0), pady=2)
        Tooltip(btn, "Drop a marker at the playhead. Two nearby markers carve out their "
                     "own segment so you can give it its own volume - no frames removed. "
                     "Right-click a marker on the timeline to delete it.")
        hbtn = themed_button(
            row, "?",
            lambda: HelpDialog(self, title="Cut & Mark Audio", body=self._HELP_CUT_MARK),
            kind="info", width=2)
        hbtn.pack(side="left", padx=(6, 0), pady=2)
        Tooltip(hbtn, "What Cut and Mark Audio do")
        vsep(row)

        # Keyframes group.
        self._fluid_kf_btn = themed_button(row, "+ Fluid KF (K)",
                                           lambda: self.add_keyframe("fluid"), kind="primary")
        self._fluid_kf_btn.pack(side="left", pady=2)
        Tooltip(self._fluid_kf_btn, "Fluid keyframe: smoothly interpolate the layout between "
                                    "this and the neighbouring keyframes.")
        btn = themed_button(row, "+ Jump KF (Shift+K)",
                            lambda: self.add_keyframe("jump"), kind="secondary")
        btn.pack(side="left", padx=(6, 0), pady=2)
        Tooltip(btn, "Jump keyframe: hold the layout, then cut instantly to the next "
                     "keyframe.")
        btn = themed_button(row, "delete KF (del)", lambda: self.delete_keyframe(),
                            kind="danger")
        btn.pack(side="left", padx=(6, 0), pady=2)
        Tooltip(btn, "Delete the selected keyframe (or the one at the playhead).")
        # 5.5.1b: guard against accidental right-drag retimes. Global pref, not per-clip.
        self._kf_lock_var = tk.BooleanVar(
            value=bool((self.app.config.get("editor") or {}).get("lock_keyframes",
                                                                  False)))
        lock_cb = ttk.Checkbutton(row, text="Lock KFs", variable=self._kf_lock_var,
                                  command=self._on_kf_lock_toggle)
        lock_cb.pack(side="left", padx=(8, 0))
        Tooltip(lock_cb, "Stop timeline drags from moving keyframes. Adding and "
                         "deleting still work.")
        hbtn = themed_button(
            row, "?",
            lambda: HelpDialog(self, title="Keyframes", body=self._HELP_KEYFRAMES),
            kind="info", width=2)
        hbtn.pack(side="left", padx=(6, 0), pady=2)
        Tooltip(hbtn, "What keyframes do")

        self._lamp = tk.Canvas(row2, width=20, height=20, bg=PALETTE["bg"],
                               highlightthickness=0, bd=0)
        self._lamp.pack(side="left", padx=(0, 6))
        self._status = tk.Label(row2, text="No clip - load one in Import.", bg=PALETTE["bg"],
                                fg=PALETTE["text_mute"], font=ui_font(9), anchor="w")
        self._status.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Frame-accurate scrub: the value IS the frame index.
        self._scrub = tk.Scale(
            bottom, from_=0, to=1, orient="horizontal", resolution=1, showvalue=0,
            command=self._on_scrub, bg=PALETTE["panel"], troughcolor=PALETTE["deep"],
            activebackground=PALETTE["green"], highlightthickness=0, bd=0,
            sliderrelief="flat", sliderlength=20, width=12)
        self._scrub.pack(fill="x", padx=10)
        self._scrub.bind("<Key>", self._on_key)  # override Scale's own ±1 arrow steps
        self._bind_scale_jump(self._scrub)       # click anywhere = scrub there

        # Timeline lane - keyframe track on top, lower band reserved for 2.6 audio.
        self._timeline = tk.Canvas(bottom, height=TIMELINE_H, bg=PALETTE["deep"],
                                   highlightthickness=0, bd=0, takefocus=1)
        self._timeline.pack(fill="x", padx=10, pady=(4, 10))
        self._timeline.bind("<Configure>", self._on_tl_configure)
        self._timeline.bind("<ButtonPress-1>", self._on_tl_press)
        self._timeline.bind("<B1-Motion>", self._on_tl_drag)
        self._timeline.bind("<ButtonRelease-1>", self._on_tl_release)
        self._timeline.bind("<ButtonPress-3>", self._on_tl_rpress)
        self._timeline.bind("<B3-Motion>", self._on_tl_rdrag)
        self._timeline.bind("<ButtonRelease-3>", self._on_tl_rrelease)
        self._timeline.bind("<Key>", self._on_key)   # K/Delete right after timeline clicks

        # Always-present per-tab tutorial replay button (5.5.4c; repositioned in the
        # 5.5.4c correction round - placed over the whole tab's bottom-right corner it
        # sat on top of the timeline. A dedicated slim row below the timeline instead,
        # reusing TutorialReplayButton's own button+label but re-parented from its
        # default place() to a normal right-aligned pack() in this row).
        tutorial_row = tk.Frame(bottom, bg=PALETTE["bg"])
        tutorial_row.pack(fill="x", padx=10, pady=(0, 6))
        replay_btn = TutorialReplayButton(tutorial_row, self._start_walkthrough,
                                          "New here? Replay the tour.")
        replay_btn.place_forget()
        replay_btn.pack(side="right")

        # Top: source viewer (takes the slack) | 9:16 preview | inspector.
        top = tk.Frame(self, bg=PALETTE["bg"])
        top.pack(fill="both", expand=True)
        top.grid_rowconfigure(0, weight=1)
        top.grid_columnconfigure(0, weight=1)

        self._source_canvas = tk.Canvas(top, bg=PALETTE["panel"], highlightthickness=0,
                                        bd=0, takefocus=1)
        self._source_canvas.grid(row=0, column=0, sticky="nsew", padx=(10, 5), pady=(10, 0))

        self._preview_canvas = tk.Canvas(top, bg=PALETTE["deep"], width=240,
                                         highlightthickness=0, bd=0, takefocus=1)
        self._preview_canvas.grid(row=0, column=1, sticky="ns", padx=5, pady=(10, 0))

        # autohide=False: the scrollbar stays visible so "more below" is obvious (5.5.1a).
        self._inspector = ScrollableFrame(top, autohide=False)
        self._inspector.canvas.configure(width=INSPECTOR_W)
        self._inspector.grid(row=0, column=2, sticky="ns", padx=(5, 10), pady=(10, 0))
        self._build_inspector()

        # Box dragging on the source; seam dragging on the preview. Both presses also
        # take focus so the frame keys work right after interacting with a viewer.
        self._source_canvas.bind("<ButtonPress-1>", self._on_source_press)
        self._source_canvas.bind("<B1-Motion>", self._on_source_drag)
        self._source_canvas.bind("<ButtonRelease-1>", self._on_source_release)
        self._source_canvas.bind("<Motion>", self._on_source_motion)
        self._preview_canvas.bind("<ButtonPress-1>", self._on_preview_press)
        self._preview_canvas.bind("<B1-Motion>", self._on_preview_drag)
        self._preview_canvas.bind("<ButtonRelease-1>", self._on_preview_release)
        self._preview_canvas.bind("<Motion>", self._on_preview_motion)

        # Tiered keyboard nav lives on the module + both viewers.
        for w in (self, self._source_canvas, self._preview_canvas):
            w.bind("<Key>", self._on_key)

        self._set_lamp(False)

    # ── Tutorial (5.5.4c) ────────────────────────────────────────────────────────────
    def maybe_show_tutorial(self):
        """Hub hook (main.py's <<NotebookTabChanged>> dispatch, see 5.5.4a): show the
        Editor intro popup on first visit, once tutorials have been asked about and
        are on."""
        tcfg = self.app.config.get("tutorial", {})
        if not tcfg.get("asked") or not tcfg.get("enabled"):
            return
        if tcfg.get("seen", {}).get("editor"):
            return
        self._offer_tutorial()

    def _offer_tutorial(self):
        """Show the Editor's intro popup. Marks "seen" the moment it's shown - skipping
        and touring both count, so the intro never repeats either way."""
        tcfg = self.app.config.setdefault("tutorial", {})
        tcfg.setdefault("seen", {})["editor"] = True
        self.app.save_config()
        TutorialIntro(
            self, title="Welcome to the Editor",
            body=("This is where a clip becomes a Short - crop it, add keyframes, "
                  "mark highlights, cut what you don't need, mix the audio, then "
                  "render."),
            on_show_me=self._start_walkthrough)

    def _tour_show_section(self, key):
        """on_show helper: expand inspector section `key` if it's currently collapsed,
        then scroll so its header sits near the TOP of the inspector viewport, before
        its step's ring is drawn - a collapsed or scrolled-out-of-view section would
        otherwise leave the ring pointing at nothing (5.5.4c). Programmatic expand
        does NOT persist a collapsed/expanded preference - only a manual header click
        does that, in _section's toggle().

        Scrolls to the top rather than using ScrollableFrame.scroll_to's minimal
        adjustment (5.5.4c correction round 2): scroll_to only moves the view far
        enough to make the HEADER visible, which for a section near the bottom of the
        inspector (AUDIO, RENDER OPTIONS) can leave the body - the actual content the
        step is pointing at - below the fold, needing a manual scroll. Pinning the
        header to the top instead maximizes the body's visible room underneath it."""
        sec = self._sections.get(key)
        if sec is None:
            return
        header = sec["header"]
        try:
            sec["set"](True)
            canvas = self._inspector.canvas
            body = self._inspector.body
            canvas.update_idletasks()
            if not header.winfo_ismapped():
                return
            wy = header.winfo_rooty() - body.winfo_rooty()
            bbox = canvas.bbox("all")
            if not bbox:
                return
            total_h = bbox[3] - bbox[1]
            if total_h <= 0:
                return
            margin = 10
            frac = max(0.0, min(1.0, (wy - margin) / total_h))
            canvas.yview_moveto(frac)
        except (tk.TclError, AttributeError):   # a bad scroll target must never crash the tour
            pass

    def _start_walkthrough(self):
        """The editor's 11-step tour (5.5.4c correction round - expanded from the
        original 7 per beta feedback: the Cut/keyframe step was too broad to be
        useful, several section steps needed more than one sentence, and player
        options / Keybinds+Render had no step at all). Timeline orientation, Cut +
        Mark Audio, Keyframes, then one step per inspector section (SEAM, BRANDING,
        CONTENT BOXES, LAYOUTS, AUDIO, RENDER OPTIONS - auto-expanding and scrolling
        into view), then Player options and a closing Keybinds + Render step. Also
        the entry point for the replay button."""
        def sec_target(key):
            return lambda k=key: (self._sections.get(k) or {}).get("header")

        steps = [
            {"target": lambda: getattr(self, "_timeline", None),
             "title": "Timeline",
             "body": "Load a clip in Import, then scrub here or with the slider "
                     "above to move through it."},
            {"target": lambda: getattr(self, "_cut_btn", None),
             "title": "Cut & audio markers",
             "body": "Cut (V): press once to mark the start of a range at the "
                     "playhead, move to where it should end, then press Cut again "
                     "to remove everything in between - press without moving to "
                     "cancel. Mark Audio (C): drop two markers to carve out a "
                     "segment and give it its own volume, without removing frames."},
            {"target": lambda: getattr(self, "_fluid_kf_btn", None),
             "title": "Keyframes",
             "body": "Move the crop or highlight boxes in the source view to where "
                     "you want them at this point in the clip, then press +Fluid KF "
                     "(K) to save that layout - the editor smoothly interpolates "
                     "between keyframes. +Jump KF (Shift+K) holds the layout, then "
                     "cuts instantly to the next one."},
            {"target": sec_target("seam"),
             "on_show": lambda: self._tour_show_section("seam"),
             "title": "Seam",
             "body": "Crop the gameplay and camera panels and set how they split. "
                     "Choose a soft seam melt to blend the boundary, or a solid "
                     "border between them instead. Single panel mode uses one crop "
                     "in place of the stacked split."},
            {"target": sec_target("branding"),
             "on_show": lambda: self._tour_show_section("branding"),
             "title": "Branding",
             "body": "Your name plate (platform icons, left/center/right "
                     "placement), plus any number of watermark or logo images - "
                     "drag to move, use the preview's corner handles to resize, "
                     "and set opacity per image."},
            {"target": sec_target("content_boxes"),
             "on_show": lambda: self._tour_show_section("content_boxes"),
             "title": "Content boxes",
             "body": "Add picture-in-picture highlights to show off extra footage "
                     "- each can be keyframed to move, resize, or pop in and out "
                     "over time. More highlights cost extra render time. (Reset "
                     "game/cam box below just restores the default crop.)"},
            {"target": sec_target("layouts"),
             "on_show": lambda: self._tour_show_section("layouts"),
             "title": "Layouts",
             "body": "The core time-saver of the whole app: save your crop, seam, "
                     "and branding setup to a slot, then reload it instantly on "
                     "the next clip from the same game - same game, same layout, "
                     "quick export. Five slots by default, add more as you need."},
            {"target": sec_target("audio"),
             "on_show": lambda: self._tour_show_section("audio"),
             "title": "Audio",
             "body": "The waveform, a whole-clip gain, and per-segment gain for "
                     "balancing louder or quieter sections."},
            {"target": sec_target("render_options"),
             "on_show": lambda: self._tour_show_section("render_options"),
             "title": "Render options",
             "body": "Optional sharpening for the final pass - 0.3 to 0.6 is a "
                     "good starting range, but test it on your own clips since the "
                     "right amount varies by source."},
            {"target": lambda: getattr(self, "_play_trim_cb", None),
             "title": "Player options",
             "body": "Loop repeats playback, In→Out only restricts it to your trim "
                     "window, and Mute silences the preview only - the rendered "
                     "audio is unaffected. Useful for checking your edit before "
                     "you render."},
            {"target": lambda: getattr(self, "_keys_btn", None),
             "title": "Keybinds & Render",
             "body": "Keybinds lists every shortcut in one place. When you're "
                     "happy with the edit, hit RENDER - it writes to data/Shorts "
                     "for Export to pick up."},
        ]
        TutorialWalkthrough(self.app.root, steps).start()

    def _section(self, title, help=None):
        """A labeled, collapsible inspector section (5.5.1a). The header (chevron +
        title) is always built; clicking it toggles the body and persists the state in
        config editor.inspector_collapsed. With `help` (a body string or a steps list)
        a small `?` button at the right of the header opens a HelpDialog - convention
        #8 (2.10.3); the `?` does not toggle. Widgets inside a collapsed body stay
        alive, just unmapped. Returns the body frame (call sites unchanged)."""
        key = _section_key(title)
        header = tk.Frame(self._inspector.body, bg=PALETTE["bg"], cursor="hand2")
        header.pack(fill="x", padx=12, pady=(12, 2))
        chevron = tk.Label(header, text="▼", bg=PALETTE["bg"], fg=PALETTE["text_dim"],
                           font=ui_font(11), cursor="hand2")
        chevron.pack(side="left")
        title_lbl = tk.Label(header, text=title, bg=PALETTE["bg"],
                             fg=PALETTE["text_mute"], font=ui_font(9), cursor="hand2")
        title_lbl.pack(side="left", padx=(4, 0))
        if help is not None:
            steps = help if isinstance(help, (list, tuple)) else None
            body_txt = None if steps else help
            hbtn = themed_button(
                header, "?",
                lambda t=title, s=steps, b=body_txt: HelpDialog(self, title=t,
                                                                steps=s, body=b),
                kind="neutral", width=2)
            hbtn.pack(side="right")
            Tooltip(hbtn, f"What “{title.title()}” does")
            self._help_btns.append(hbtn)
        frame = tk.Frame(self._inspector.body, bg=PALETTE["bg"])
        frame.pack(fill="x", padx=12)

        sec = {"header": header, "body": frame, "chevron": chevron, "open": True}

        def set_open(open_):
            sec["open"] = bool(open_)
            if open_:
                # after= keeps the body directly under its own header regardless of
                # what else is mapped.
                frame.pack(fill="x", padx=12, after=header)
                chevron.configure(text="▼")
            else:
                frame.pack_forget()
                chevron.configure(text="▶")

        def toggle(_e=None):
            set_open(not sec["open"])
            saved = self.app.config.setdefault("editor", {}).setdefault(
                "inspector_collapsed", {})
            saved[key] = not sec["open"]
            self.app.save_config()

        sec["set"] = set_open
        self._sections[key] = sec
        for w in (header, chevron, title_lbl):
            w.bind("<Button-1>", toggle)
        saved = (self.app.config.get("editor") or {}).get("inspector_collapsed") or {}
        if _initial_collapsed(key, saved):
            set_open(False)
        return frame

    def _hscale(self, parent, frm, to, variable, command):
        """A horizontal tk.Scale styled like the scrub (on-palette), with sane mouse
        behavior (see _bind_scale_jump)."""
        scale = tk.Scale(parent, from_=frm, to=to, orient="horizontal", resolution=1,
                         showvalue=1, variable=variable, command=command,
                         bg=PALETTE["panel"], troughcolor=PALETTE["deep"],
                         activebackground=PALETTE["green"], fg=PALETTE["text_dim"],
                         highlightthickness=0, bd=0, sliderrelief="flat",
                         sliderlength=18, width=12, font=ui_font(8))
        self._bind_scale_jump(scale)
        # A slider drag is one undo step; releasing ends it (5.5.2b correction).
        scale.bind("<ButtonRelease-1>", lambda _e: self._undo_commit(), add="+")
        return scale

    def _bind_scale_jump(self, scale):
        """Sane tk.Scale mouse behavior (jump-to-click + drag-follow). The logic was
        promoted to shared.ui_helpers.bind_scale_jump so other modules' sliders
        behave identically; this thin method keeps the existing call sites."""
        bind_scale_jump(scale)

    def _build_inspector(self):
        self._help_btns = []   # 2.10.3: section `?` buttons (for the harness/assert)
        self._sections = {}    # 5.5.1a: key -> {header, body, chevron, open, set}
        # SEAM (5.5.2d): the panel division. Single-panel mode toggle (2.10.1: one 9:16
        # crop fills the whole frame vs the game+cam stack), split position, seam melt,
        # and border. Branding (the name plate) is its own section below now.
        sec = self._section("SEAM", help=self._HELP_SEAM)
        self.single_panel = tk.BooleanVar(value=False)
        single_cb = ttk.Checkbutton(sec, text="Single panel (one crop)",
                                    variable=self.single_panel,
                                    command=self._on_mode_change)
        single_cb.pack(anchor="w", pady=(0, 6))
        Tooltip(single_cb, "One crop fills the whole frame instead of the stacked "
                           "game + camera split.")
        # Split (single-panel: this slider becomes the line/plate position, 0..100).
        self.split_var = tk.IntVar(value=50)
        self._split_label = tk.Label(sec, text="Gameplay height %", bg=PALETTE["bg"],
                                     fg=PALETTE["text_dim"], font=ui_font(9))
        self._split_label.pack(anchor="w")
        self._split_scale = self._hscale(sec, 30, 80, self.split_var,
                                         self._on_split_change)
        self._split_scale.pack(fill="x")
        Tooltip(self._split_scale, "Where the gameplay panel ends and the camera panel "
                                   "begins. Single panel: positions the line and name "
                                   "plate instead. You can also drag the green line in "
                                   "the preview.")

        # Seam melt + border. 2.10.1: the melt controls live in their own frame so
        # single-panel can hide the whole group (no seam to melt); the border block
        # below stays in both modes.
        tk.Frame(sec, height=1, bg=PALETTE["border"]).pack(fill="x", pady=(10, 6))
        self._seam_melt_box = tk.Frame(sec, bg=PALETTE["bg"])
        self._seam_melt_box.pack(fill="x")
        self.melt_on = tk.BooleanVar(value=True)
        melt_cb = ttk.Checkbutton(self._seam_melt_box, text="Seam melt",
                                  variable=self.melt_on,
                                  command=self._on_layout_change)
        melt_cb.pack(anchor="w")
        Tooltip(melt_cb, "Soften the boundary between the panels with a smooth blend.")
        tk.Label(self._seam_melt_box, text="Melt width", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(9)).pack(anchor="w", pady=(4, 0))
        self.melt_px = tk.IntVar(value=50)
        melt_slider = self._hscale(self._seam_melt_box, 0, 150, self.melt_px,
                                   self._on_layout_change)
        melt_slider.pack(fill="x")
        Tooltip(melt_slider, "How far the cosine cross-blend reaches above/below the seam "
                             "(1080×1920 reference px).")
        # Border block (the solid line) - kept in single-panel too (now decorative).
        self._border_block = tk.Frame(sec, bg=PALETTE["bg"])
        self._border_block.pack(fill="x")
        self.border_on = tk.BooleanVar(value=False)
        border_cb = ttk.Checkbutton(self._border_block, text="Solid border",
                                    variable=self.border_on,
                                    command=self._on_layout_change)
        border_cb.pack(anchor="w", pady=(4, 0))
        Tooltip(border_cb, "Draw a solid line on the seam instead of a blend.")
        tk.Label(self._border_block, text="Border width", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(9)).pack(anchor="w", pady=(4, 0))
        # 2.9.5: seam-border half-thickness in 1080×1920 reference px (default 12, the
        # original fixed value). Mirrors the melt-width slider.
        self.border_px = tk.IntVar(value=12)
        border_slider = self._hscale(self._border_block, 1, 60, self.border_px,
                                     self._on_layout_change)
        border_slider.pack(fill="x")
        Tooltip(border_slider, "Half-thickness of the solid seam line "
                               "(1080×1920 reference px).")
        self._color_row(self._border_block, "Border color", "_border_hex")

        # BRANDING (5.5.2d): the name plate identity - burn-in name, size, platform
        # icons, icon layout, plate position, name/plate colors - plus (5.5.1g) any
        # number of independent branding/watermark image overlays.
        sec = self._section("BRANDING", help=self._HELP_BRANDING)
        self.wm_on = tk.BooleanVar(value=False)
        wm_cb = ttk.Checkbutton(sec, text="Burn name", variable=self.wm_on,
                                command=self._on_layout_change)
        wm_cb.pack(anchor="w")
        Tooltip(wm_cb, "Burn the name plate into the output video.")
        name_row = tk.Frame(sec, bg=PALETTE["bg"])
        name_row.pack(fill="x", pady=(4, 0))
        self._wm_entry = ttk.Entry(name_row, width=16)
        self._wm_entry.pack(side="left", fill="x", expand=True)
        self._wm_entry.bind("<KeyRelease>", lambda _e: self._on_layout_change())
        Tooltip(self._wm_entry, "The name shown on the plate.")
        tk.Label(name_row, text="size", bg=PALETTE["bg"], fg=PALETTE["text_dim"],
                 font=ui_font(9)).pack(side="left", padx=(8, 4))
        self.wm_scale_var = tk.DoubleVar(value=1.0)
        spin = ttk.Spinbox(name_row, from_=0.5, to=3.0, increment=0.1, width=5,
                           textvariable=self.wm_scale_var, command=self._on_layout_change)
        spin.pack(side="left")
        spin.bind("<KeyRelease>", lambda _e: self._on_layout_change())
        Tooltip(spin, "Name size multiplier (1.0 = default).")
        # Platform icons (drawn left of the name; PNGs supplied by the user in assets/).
        plat_row = tk.Frame(sec, bg=PALETTE["bg"])
        plat_row.pack(fill="x", pady=(6, 0))
        tk.Label(plat_row, text="Platforms", bg=PALETTE["bg"], fg=PALETTE["text_dim"],
                 font=ui_font(9)).pack(side="left")
        self.plat_twitch = tk.BooleanVar(value=True)
        self.plat_youtube = tk.BooleanVar(value=False)
        tw_cb = ttk.Checkbutton(plat_row, text="Twitch", variable=self.plat_twitch,
                                command=self._on_layout_change)
        tw_cb.pack(side="left", padx=(8, 0))
        Tooltip(tw_cb, "Show the Twitch icon next to the name.")
        yt_cb = ttk.Checkbutton(plat_row, text="YouTube", variable=self.plat_youtube,
                                command=self._on_layout_change)
        yt_cb.pack(side="left", padx=(6, 0))
        Tooltip(yt_cb, "Show the YouTube icon next to the name.")
        mode_row = tk.Frame(sec, bg=PALETTE["bg"])
        mode_row.pack(fill="x", pady=(4, 0))
        tk.Label(mode_row, text="Icon layout", bg=PALETTE["bg"], fg=PALETTE["text_dim"],
                 font=ui_font(9)).pack(side="left")
        self.icon_mode_var = tk.StringVar(value="side by side")
        mode_box = ttk.Combobox(mode_row, textvariable=self.icon_mode_var,
                                state="readonly", values=("side by side", "stacked"),
                                width=11)
        mode_box.pack(side="right")
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self._on_layout_change())
        Tooltip(mode_box, "Side by side: icons in a row. Stacked: icons in a column.")
        # Plate position on the seam (2.8.8): left / center / right.
        pos_row = tk.Frame(sec, bg=PALETTE["bg"])
        pos_row.pack(fill="x", pady=(4, 0))
        tk.Label(pos_row, text="Plate", bg=PALETTE["bg"], fg=PALETTE["text_dim"],
                 font=ui_font(9)).pack(side="left")
        self.wm_pos_var = tk.StringVar(value="center")
        for label, val in (("Left", "left"), ("Center", "center"),
                           ("Right", "right")):
            rb = ttk.Radiobutton(pos_row, text=label, value=val,
                                 variable=self.wm_pos_var,
                                 command=self._on_layout_change)
            rb.pack(side="left", padx=(8, 0))
            Tooltip(rb, "Parks the name plate left, centered, or right on the seam.")
        self._color_row(sec, "Name color", "_text_hex")
        self._color_row(sec, "Name background", "_plate_hex")
        # Branding / watermark images (5.5.1g: generalized from the single 5.5.1f avatar
        # into a list, like highlights - logo, sponsor image, avatar, or any image the
        # user wants pasted on the video, independent of the name plate).
        tk.Frame(sec, height=1, bg=PALETTE["border"]).pack(fill="x", pady=(10, 6))
        self.brand_on = tk.BooleanVar(value=False)
        brand_cb = ttk.Checkbutton(sec, text="Enable branding / watermark images",
                                   variable=self.brand_on, command=self._on_brand_toggle)
        brand_cb.pack(anchor="w")
        Tooltip(brand_cb, "Show image overlays (logo, sponsor banner, avatar, etc.) on "
                          "the video, independent of the name plate.")
        self._brand_rows = tk.Frame(sec, bg=PALETTE["bg"])
        self._brand_rows.pack(fill="x", pady=(4, 0))
        add_brand_btn = themed_button(sec, "+ Add image", self.add_branding,
                                      kind="highlight")
        add_brand_btn.pack(anchor="w", pady=(4, 0))
        Tooltip(add_brand_btn, "Add another branding/watermark image overlay.")
        # Selected-item controls: visible WHENEVER an item is selected (not only
        # mid-drag), mirroring the 5.5.1f avatar-controls convention.
        self._brand_ctrls = tk.Frame(sec, bg=PALETTE["bg"])
        brand_row = tk.Frame(self._brand_ctrls, bg=PALETTE["bg"])
        brand_row.pack(fill="x", pady=(2, 0))
        brand_btn = themed_button(brand_row, "Browse image…", self._browse_branding,
                                  kind="neutral")
        brand_btn.pack(side="left")
        Tooltip(brand_btn, "Pick the image file for the selected overlay (PNG with "
                           "transparency works best).")
        self._brand_label = tk.Label(brand_row, text="(none)", bg=PALETTE["bg"],
                                     fg=PALETTE["text_faint"], font=ui_font(8), anchor="w")
        self._brand_label.pack(side="left", padx=(8, 0), fill="x", expand=True)
        self.brand_lock_var = tk.BooleanVar(value=True)
        lock_cb = ttk.Checkbutton(self._brand_ctrls, text="Lock aspect ratio",
                                  variable=self.brand_lock_var,
                                  command=self._on_brand_lock_change)
        lock_cb.pack(anchor="w", pady=(4, 0))
        Tooltip(lock_cb, "On: the preview's resize handles keep the image's shape. "
                         "Off: drag any handle to stretch it to any shape.")
        tk.Label(self._brand_ctrls, text="Opacity (%)", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(9)).pack(anchor="w", pady=(4, 0))
        self.brand_opacity_var = tk.IntVar(value=100)
        brand_op = self._hscale(self._brand_ctrls, 0, 100, self.brand_opacity_var,
                                self._on_brand_opacity_change)
        brand_op.pack(fill="x")
        Tooltip(brand_op, "How see-through the selected overlay is.")
        tk.Label(self._brand_ctrls,
                 text="Drag to move; drag a handle in the preview to resize.",
                 bg=PALETTE["bg"], fg=PALETTE["text_faint"], font=ui_font(8),
                 anchor="w").pack(fill="x", pady=(2, 0))
        self._sync_brand_ctrls()

        # CONTENT BOXES (merged from BOXES + HIGHLIGHTS, 5.5.1a corrections): the
        # game/cam crop-box resets plus the highlight PiP boxes.
        sec = self._section("CONTENT BOXES", help=self._HELP_CONTENT_BOXES)
        btn_row = tk.Frame(sec, bg=PALETTE["bg"])
        btn_row.pack(fill="x")
        self._reset_game_btn = themed_button(
            btn_row, "Reset game box", lambda: self._reset_box("game"), kind="neutral")
        self._reset_game_btn.pack(side="left")
        Tooltip(self._reset_game_btn, "Snap the game crop box back to its default "
                                      "size and position.")
        self._reset_cam_btn = themed_button(
            btn_row, "Reset cam box", lambda: self._reset_box("cam"), kind="neutral")
        self._reset_cam_btn.pack(side="left", padx=(8, 0))
        Tooltip(self._reset_cam_btn, "Snap the camera crop box back to its default "
                                     "size and position.")
        # Highlights (2.5: PiP boxes - crop on the source, place on the preview).
        tk.Frame(sec, height=1, bg=PALETTE["border"]).pack(fill="x", pady=(10, 6))
        self.hl_on = tk.BooleanVar(value=False)
        hl_cb = ttk.Checkbutton(sec, text="Enable highlights", variable=self.hl_on,
                                command=self._on_hl_toggle)
        hl_cb.pack(anchor="w")
        Tooltip(hl_cb, "Show picture-in-picture highlight boxes in the output.")
        self.hl_below = tk.BooleanVar(value=False)
        hl_below_cb = ttk.Checkbutton(sec, text="Allow below the seam",
                                      variable=self.hl_below,
                                      command=self._on_hl_below_toggle)
        hl_below_cb.pack(anchor="w")
        Tooltip(hl_below_cb, "By default a PiP is confined above the seam; enable this to "
                             "place it anywhere in the frame.")
        self._hl_rows = tk.Frame(sec, bg=PALETTE["bg"])
        self._hl_rows.pack(fill="x", pady=(4, 0))
        add_hl_btn = themed_button(sec, "+ Add highlight", self.add_highlight,
                                   kind="highlight")
        add_hl_btn.pack(anchor="w", pady=(4, 0))
        Tooltip(add_hl_btn, "Add another picture-in-picture box.")
        self._hl_note = tk.Label(
            sec, text="Most clips need one highlight -\nextra PiPs add render cost "
                      "and clutter.",
            bg=PALETTE["bg"], fg=PALETTE["yellow"], font=ui_font(8), justify="left")
        # (packed/unpacked by _refresh_hl_rows when the list grows past one)

        # LAYOUTS (5.5.2e: extendable slots, min 5 + last-used autosave)
        sec = self._section("LAYOUTS")
        name_row = tk.Frame(sec, bg=PALETTE["bg"])
        name_row.pack(fill="x", pady=(0, 4))
        tk.Label(name_row, text="name", bg=PALETTE["bg"], fg=PALETTE["text_dim"],
                 font=ui_font(9)).pack(side="left", padx=(0, 6))
        self._slot_name = ttk.Entry(name_row, width=16)
        self._slot_name.pack(side="left", fill="x", expand=True)
        Tooltip(self._slot_name, "Name for the slot you save next.")
        # Slot rows live in their own frame so add/delete can rebuild them (same
        # destroy-and-rebuild pattern as the exporter's snippet rows).
        self._slots_frame = tk.Frame(sec, bg=PALETTE["bg"])
        self._slots_frame.pack(fill="x")
        self._slot_btns = []
        self._rebuild_slot_rows()
        # Add / delete row: two buttons sharing the width 50/50, delete LEFT, add RIGHT.
        ad_row = tk.Frame(sec, bg=PALETTE["bg"])
        ad_row.pack(fill="x", pady=(6, 0))
        ad_row.grid_columnconfigure(0, weight=1, uniform="slotad")
        ad_row.grid_columnconfigure(1, weight=1, uniform="slotad")
        del_btn = themed_button(ad_row, "Delete last slot", self._delete_last_slot,
                                kind="danger")
        del_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        Tooltip(del_btn, "Remove the last slot (minimum five). Asks first if it holds "
                         "a saved layout.")
        add_btn = themed_button(ad_row, "Add slot", self._add_slot, kind="neutral")
        add_btn.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        Tooltip(add_btn, "Append an empty layout slot.")

        # (Trim + Cut controls live on the action bar above the timeline; the timeline
        # itself is the readout - shaded outside In/Out, red bands on removed ranges.)

        # AUDIO (2.6: waveform toggle + whole-clip render gain; no audible playback yet)
        sec = self._section("AUDIO", help=self._HELP_AUDIO)
        self.wave_on = tk.BooleanVar(value=True)
        wave_cb = ttk.Checkbutton(sec, text="Show audio", variable=self.wave_on,
                                  command=self._redraw_timeline)
        wave_cb.pack(anchor="w")
        Tooltip(wave_cb, "Show the audio waveform on the timeline (display only).")
        grow = tk.Frame(sec, bg=PALETTE["bg"])
        grow.pack(fill="x", pady=(4, 0))
        tk.Label(grow, text="Gain (dB, at render)", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(9)).pack(side="left")
        self.gain_var = tk.DoubleVar(value=0.0)
        gain_spin = ttk.Spinbox(grow, from_=-30.0, to=30.0, increment=1.0, width=6,
                                textvariable=self.gain_var)
        gain_spin.pack(side="right")
        Tooltip(gain_spin, "Volume change for the whole clip, applied at render and "
                           "previewed live. 0 = unchanged.")
        # Spinbox commands don't fire on typed edits - a write-trace catches both
        # arrows and typing (per-clip block 2.8.2 + live preview gain 2.8.6).
        self.gain_var.trace_add("write", self._on_gain_write)
        # Per-segment gain (5.5.2b): offset for the timeline-selected kept segment,
        # additive with the whole-clip gain. Disabled without a selection.
        sgrow = tk.Frame(sec, bg=PALETTE["bg"])
        sgrow.pack(fill="x", pady=(4, 0))
        tk.Label(sgrow, text="Segment gain (dB)", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(9)).pack(side="left")
        self.seg_gain_var = tk.DoubleVar(value=0.0)
        self._seg_gain_spin = ttk.Spinbox(sgrow, from_=-30.0, to=30.0, increment=1.0,
                                          width=6, textvariable=self.seg_gain_var)
        self._seg_gain_spin.pack(side="right")
        Tooltip(self._seg_gain_spin, "Volume change for the selected segment only, "
                                     "added on top of the whole-clip gain.")
        self._seg_ctx = tk.Label(sec, text="no segment selected", bg=PALETTE["bg"],
                                 fg=PALETTE["text_faint"], font=ui_font(8), anchor="w")
        self._seg_ctx.pack(fill="x")
        self.seg_gain_var.trace_add("write", self._on_seg_gain_write)
        self._refresh_seg_gain_row()

        # RENDER OPTIONS (2.6: composite In..Out → single H.264 encode + audio mux;
        # 5.5.1a: the Render button itself lives on the toolbar now)
        sec = self._section("RENDER OPTIONS", help=self._HELP_RENDER)
        # Sharpen-at-render (3.1): an unsharp mask applied to every output frame INSIDE the one
        # existing encode (no second pass, no extra generation loss). Render-only by design -
        # the live preview/playback path stays unsharpened so the editor keeps responsive
        # (Prime Directive #2). Off by default; both values persist in the per-clip block (2.8.2).
        self.sharpen_on = tk.BooleanVar(value=False)
        self.sharpen_strength = tk.DoubleVar(value=1.0)
        sharpen_cb = ttk.Checkbutton(sec, text="Sharpen at render",
                                     variable=self.sharpen_on,
                                     command=self._mark_clip_dirty)
        sharpen_cb.pack(anchor="w")
        Tooltip(sharpen_cb, "Sharpen every frame while rendering (see the ? below).")
        srow = tk.Frame(sec, bg=PALETTE["bg"])
        srow.pack(fill="x", pady=(2, 0))
        tk.Label(srow, text="Strength", bg=PALETTE["bg"], fg=PALETTE["text_dim"],
                 font=ui_font(9)).pack(side="left")
        sharp_spin = ttk.Spinbox(srow, from_=0.0, to=3.0, increment=0.1, width=6,
                                 textvariable=self.sharpen_strength)
        sharp_spin.pack(side="right")
        Tooltip(sharp_spin, "Unsharp-mask amount. 0.3 to 0.6 is a good starting range; "
                            "0 = off. Test on your own clips - it varies by source.")
        # Yellow caution row + a ? (convention #8): sharpening every frame costs render time.
        warn = tk.Frame(sec, bg=PALETTE["bg"])
        warn.pack(fill="x", pady=(4, 0))
        tk.Label(warn, text="⚠ Adds render time (sharpens every frame).",
                 bg=PALETTE["bg"], fg=PALETTE["yellow"], font=ui_font(8)).pack(side="left")
        themed_button(warn, "?",
                      lambda: HelpDialog(self, title="Sharpen at render",
                                         body=self._HELP_SHARPEN),
                      kind="neutral", width=2).pack(side="right")
        # Spinbox commands don't fire on typed edits - a write-trace catches arrows AND typing
        # (per-clip block 2.8.2; _mark_clip_dirty early-returns under _applying / no clip).
        self.sharpen_strength.trace_add("write", lambda *_: self._mark_clip_dirty())
        self._render_prog = ttk.Progressbar(sec, mode="determinate", maximum=100)
        self._render_cancel_btn = themed_button(sec, "Cancel", self._cancel_render,
                                                kind="danger")
        Tooltip(self._render_cancel_btn, "Stop the render. The partial file is removed.")
        # (progress bar + cancel button pack only while a render runs)

    def _color_row(self, parent, label, attr):
        """A labeled color swatch row; clicking the swatch opens the system picker."""
        row = tk.Frame(parent, bg=PALETTE["bg"])
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text=label, bg=PALETTE["bg"], fg=PALETTE["text_dim"],
                 font=ui_font(9)).pack(side="left")
        swatch = tk.Button(row, width=3, bg=getattr(self, attr), relief="flat", bd=0,
                           cursor="hand2", activebackground=getattr(self, attr),
                           highlightthickness=1,
                           highlightbackground=PALETTE["border"],
                           command=lambda: self._pick_color(attr))
        swatch.pack(side="right")
        Tooltip(swatch, f"Pick the {label.lower()}.")
        self._swatches[attr] = swatch

    def _pick_color(self, attr):
        _rgb, hex_str = colorchooser.askcolor(initialcolor=getattr(self, attr),
                                              parent=self)
        if hex_str:
            setattr(self, attr, hex_str)
            self._swatches[attr].configure(bg=hex_str, activebackground=hex_str)
            self._on_layout_change()

    def _refresh_swatches(self):
        for attr, swatch in self._swatches.items():
            color = getattr(self, attr)
            swatch.configure(bg=color, activebackground=color)

    # ── Timeline (2.3): frame↔x mapping, drawing, navigation ─────────────────────────
    def _tl_x_of(self, frame):
        c = self._timeline
        left, right = TL_PAD, c.winfo_width() - TL_PAD
        return left + (frame / max(1, self.total_frames - 1)) * (right - left)

    def _tl_frame_of(self, x):
        c = self._timeline
        left, right = TL_PAD, c.winfo_width() - TL_PAD
        frac = (x - left) / max(1, right - left)
        return max(0, min(self.total_frames - 1,
                          round(frac * (self.total_frames - 1))))

    def _on_tl_configure(self, _e):
        if self._tl_configure_job is not None:
            self.after_cancel(self._tl_configure_job)
        self._tl_configure_job = self.after(60, self._tl_configure_cb)

    def _tl_configure_cb(self):
        self._tl_configure_job = None
        self._redraw_timeline()

    def _redraw_timeline(self):
        """Full timeline redraw: ruler + ticks, markers, playhead. Cheap (a few dozen
        canvas items), called after every seek/edit - the gotcha is the same as the
        boxes: after a clear, markers AND playhead must both come back."""
        c = self._timeline
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < TL_PAD * 2 + 20 or h < 20:
            return
        if self._path is None or self.total_frames <= 1:
            c.create_text(w / 2, h / 2, text="Timeline - load a clip",
                          fill=PALETTE["text_faint"], font=ui_font(9))
            return
        left, right = TL_PAD, w - TL_PAD

        # Ruler + second ticks (step grows so ticks stay ≥ 8px apart).
        c.create_line(left, TL_RULER_Y, right, TL_RULER_Y, fill=PALETTE["border"])
        dur_s = self.total_frames / self.fps
        px_per_s = (right - left) / max(0.001, dur_s)
        self._tl_tick_step = max(1, int(np.ceil(8 / max(0.001, px_per_s))))
        for s in range(0, int(dur_s) + 1, self._tl_tick_step):
            x = self._tl_x_of(int(s * self.fps))
            c.create_line(x, TL_RULER_Y - 3, x, TL_RULER_Y + 3,
                          fill=PALETTE["text_faint"])

        # Lower band: the audio waveform lane (2.6). One filled polygon (a single
        # canvas item - full redraws stay cheap) sharing the frame↔x mapping above.
        c.create_line(left, TL_TRACK_H + 2, right, TL_TRACK_H + 2, fill=PALETTE["panel"])
        band_top, band_bot = TL_TRACK_H + 5, h - 5
        if self.wave_on.get() and band_bot - band_top > 8:
            mid = (band_top + band_bot) / 2
            half = (band_bot - band_top) / 2
            if self._wave_state == "ready" and self._wave_peaks is not None:
                c.create_line(left, mid, right, mid, fill=PALETTE["panel"])
                n = len(self._wave_peaks)
                # 5.5.2b correction: the drawn amplitude reflects the gains the
                # render will apply (whole-clip x per-segment), clipped to the band,
                # so a volume change is VISIBLE before rendering.
                master = self._gain_lin
                seg_facs = [(seg[0], seg[1],
                             10.0 ** ((float(seg[2]) if len(seg) > 2 else 0.0) / 20.0))
                            for seg in self.segments]
                flat = master == 1.0 and all(f == 1.0 for _s, _e, f in seg_facs)
                coords, bottom = [], []
                for x in range(int(left), int(right) + 1, 3):
                    b = int((x - left) / max(1, right - left) * (n - 1))
                    p = max(0.5, float(self._wave_peaks[b]) * half)
                    if not flat:
                        f = self._tl_frame_of(x)
                        fac = next((g for s, e, g in seg_facs if s <= f <= e), 1.0)
                        p = max(0.5, min(half, p * master * fac))
                    coords.extend((x, mid - p))
                    bottom.append((x, mid + p))
                for bx, by in reversed(bottom):
                    coords.extend((bx, by))
                c.create_polygon(coords, fill=PALETTE["blue"], outline="",
                                 tags="waveform")
            elif self._wave_state == "loading":
                c.create_text((left + right) / 2, (band_top + band_bot) / 2,
                              text="analyzing audio…", fill=PALETTE["text_faint"],
                              font=ui_font(8))
            elif self._wave_state == "noaudio":
                c.create_text((left + right) / 2, (band_top + band_bot) / 2,
                              text="no audio", fill=PALETTE["text_faint"],
                              font=ui_font(8))

        # 5.5.2b: selected segment tint on the waveform band + a dB tag on every
        # segment with a nonzero offset (independent of selection). Cheap rects/text.
        if self._sel_seg is not None and self._sel_seg < len(self.segments):
            seg = self.segments[self._sel_seg]
            c.create_rectangle(self._tl_x_of(seg[0]), band_top - 2,
                               self._tl_x_of(seg[1]), band_bot + 2,
                               fill=PALETTE["green"], stipple="gray25",
                               outline=PALETTE["green"], tags="seg_sel")
        for seg in self.segments:
            db = float(seg[2]) if len(seg) > 2 else 0.0
            if db:
                x = (self._tl_x_of(seg[0]) + self._tl_x_of(seg[1])) / 2
                txt = f"{db:+.0f}" if db == int(db) else f"{db:+.1f}"
                c.create_text(x, band_top, text=txt, fill=PALETTE["yellow"],
                              font=mono_font(8), anchor="n", tags="seg_db")

        # Trim shading (2.6): dim outside [In, Out]; green In / orange Out ticks.
        if self.in_frame > 0:
            c.create_rectangle(left, 4, self._tl_x_of(self.in_frame), h - 4,
                               fill="#000000", stipple="gray50", outline="")
        if self.out_frame < self.total_frames - 1:
            c.create_rectangle(self._tl_x_of(self.out_frame), 4, right, h - 4,
                               fill="#000000", stipple="gray50", outline="")
        if self.in_frame > 0 or self.out_frame < self.total_frames - 1:
            for f, color in ((self.in_frame, PALETTE["green"]),
                             (self.out_frame, PALETTE["orange"])):
                x = self._tl_x_of(f)
                c.create_line(x, 4, x, h - 4, fill=color, width=2)

        # Removed-region markers (2.10.2d): one red stipple band (tag "cut_gap") + two thin
        # red edge lines (tag "cut_edge") per internal GAP between kept segments - a distinct
        # style from the black outer-trim shade so cuts read as cuts. Cheap; before markers.
        for seg0, seg1 in zip(self.segments, self.segments[1:]):
            gx0, gx1 = self._tl_x_of(seg0[1] + 1), self._tl_x_of(seg1[0] - 1)
            c.create_rectangle(gx0, 4, gx1, h - 4, fill=PALETTE["red"],
                               stipple="gray25", outline="", tags="cut_gap")
            for gx in (gx0, gx1):
                c.create_line(gx, 4, gx, h - 4, fill=PALETTE["red"], width=1,
                              tags="cut_edge")

        # Pending one-button cut (2.10.2d): a yellow band from the marked start to the live
        # playhead so the selection is visible BEFORE it's committed. _seek_to full-redraws,
        # so this band tracks the playhead as the user moves it.
        if self._cut_start is not None:
            px0, px1 = self._tl_x_of(self._cut_start), self._tl_x_of(self.frame_idx)
            c.create_rectangle(min(px0, px1), 4, max(px0, px1), h - 4,
                               fill=PALETTE["yellow"], stipple="gray25", outline="",
                               tags="cut_pending")
            c.create_line(px0, 4, px0, h - 4, fill=PALETTE["yellow"], width=2,
                          tags="cut_pending")

        # Selected PiP's visible range (2.8.7): one thin orange band at the top of
        # the track. Cheap - a single rectangle, only for the selected highlight.
        if (self.hl_on.get() and self.sel_hl is not None
                and self.sel_hl < len(self.highlights)):
            vis = self.highlights[self.sel_hl].get("visible")
            if vis:
                c.create_rectangle(self._tl_x_of(vis[0]), 4,
                                   self._tl_x_of(vis[1]), 7,
                                   fill=PALETTE["orange"], outline="",
                                   tags="hl_vis_band")

        # Markers: ● fluid (green) / ■ jump (yellow); white outline = selected.
        for kf in self.keyframes:
            x = self._tl_x_of(kf["frame"])
            outline = PALETTE["text"] if kf is self.selected_kf else ""
            if kf["type"] == "fluid":
                c.create_oval(x - 5, TL_MARKER_Y - 5, x + 5, TL_MARKER_Y + 5,
                              fill=PALETTE["green"], outline=outline, width=2)
            else:
                c.create_rectangle(x - 5, TL_MARKER_Y - 5, x + 5, TL_MARKER_Y + 5,
                                   fill=PALETTE["yellow"], outline=outline, width=2)

        self._move_timeline_playhead()

    def _move_timeline_playhead(self):
        """Redraw ONLY the playhead (red line + top triangle) at the current frame -
        the cheap per-tick path for playback; _redraw_timeline ends here too."""
        c = self._timeline
        c.delete("playhead")
        if self._path is None or self.total_frames <= 1:
            return
        h = c.winfo_height()
        x = self._tl_x_of(self.frame_idx)
        c.create_line(x, 4, x, h - 4, fill=PALETTE["red"], width=2, tags="playhead")
        c.create_polygon(x - 5, 2, x + 5, 2, x, 10, fill=PALETTE["red"], outline="",
                         tags="playhead")

    def _tl_kf_near(self, x, y):
        """Marker within TL_HIT_PX of canvas x (track band only), or None."""
        if y > TL_TRACK_H + 4:
            return None   # lower band: always scrub, never grab markers
        best, best_d = None, TL_HIT_PX + 1
        for kf in self.keyframes:
            d = abs(self._tl_x_of(kf["frame"]) - x)
            if d < best_d:
                best, best_d = kf, d
        return best

    def _on_tl_press(self, event):
        self._timeline.focus_set()
        if self._path is None:
            return
        kf = self._tl_kf_near(event.x, event.y)
        if kf is not None:
            # Left-click a marker: select + seek to its frame. When the playhead is
            # already parked there _seek_to early-returns, so re-apply explicitly:
            # selecting a keyframe must always show its stored geometry, even after
            # an unrecorded box edit on that frame.
            parked = kf["frame"] == self.frame_idx
            self.selected_kf = kf
            self._seek_to(kf["frame"])
            if parked:
                self._apply_interpolated(kf["frame"])
            self._redraw_timeline()
        else:
            # 5.5.2b: a click in the LOWER (waveform) band also selects the kept
            # segment under it (a removed range or empty space clears the selection);
            # the seek itself is unchanged, so scrub-anywhere keeps working.
            if event.y > TL_TRACK_H + 4:
                f = self._tl_frame_of(event.x)
                self._sel_seg = next(
                    (i for i, seg in enumerate(self.segments)
                     if seg[0] <= f <= seg[1]), None)
                self._refresh_seg_gain_row()
            self._tl_scrubbing = True
            self._seek_to(self._tl_frame_of(event.x))
            if event.y > TL_TRACK_H + 4:
                self._redraw_timeline()   # _seek_to skips the redraw on a same-frame click

    def _on_tl_drag(self, event):
        if self._tl_scrubbing:
            self._schedule_seek(self._tl_frame_of(event.x))

    def _on_tl_release(self, _event):
        self._tl_scrubbing = False
        self._undo_commit()   # no-op unless the press edited something

    def _on_tl_rpress(self, event):
        if self._path is None:
            return
        kf = self._tl_kf_near(event.x, event.y)
        if kf is not None:
            # 5.5.1b: locked keyframes never arm the retime drag (rdrag/rrelease then
            # no-op naturally). Return here so the press can't fall through and restore
            # a cut hiding under the marker. Add/delete/select/seek stay available.
            if self._kf_lock_var.get():
                self._say("Keyframes are locked - untick Lock KFs to move them.",
                          "yellow")
                return
            self._tl_drag_kf = kf
            self.selected_kf = kf
            self._redraw_timeline()
            return
        # 2.10.2d: right-click inside a removed gap restores it (keyframes win above, via
        # _tl_kf_near's top-band check, so keyframe right-drag is unaffected).
        gap = self._gap_at_x(event.x)
        if gap is not None:
            self._restore_gap(gap[0], gap[1])
            return
        # 5.5.2f: right-click a Mark Audio boundary deletes it (merges the two segments
        # it separates back into one). Mutually exclusive with the gap case above - a
        # gap needs a real span between segments, a marker boundary needs none.
        marker_f = self._marker_at_x(event.x)
        if marker_f is not None:
            self._remove_marker(marker_f)

    def _on_tl_rdrag(self, event):
        kf = self._tl_drag_kf
        if kf is None:
            return
        target = self._tl_frame_of(event.x)
        if not (event.state & 0x0001):   # Shift held = free placement, no snapping
            ph_x = self._tl_x_of(self.frame_idx)
            if abs(event.x - ph_x) <= TL_HIT_PX:
                target = self.frame_idx          # snap to playhead
            else:
                tick = self._tl_nearest_tick_frame(event.x)
                if tick is not None and abs(event.x - self._tl_x_of(tick)) <= TL_HIT_PX:
                    target = tick                # snap to a second tick
        self.move_keyframe(kf, target)

    def _on_tl_rrelease(self, _event):
        self._tl_drag_kf = None
        self._undo_commit()   # keyframe retime drag over - close its undo step

    def _tl_nearest_tick_frame(self, x):
        """Frame of the ruler tick nearest to canvas x (ticks every _tl_tick_step s)."""
        sec = self._tl_frame_of(x) / self.fps
        tick_sec = round(sec / self._tl_tick_step) * self._tl_tick_step
        if tick_sec < 0 or tick_sec > self.total_frames / self.fps:
            return None
        return max(0, min(self.total_frames - 1, int(round(tick_sec * self.fps))))

    # ── Keyframes (2.3): store, ops, interpolation ───────────────────────────────────
    def _kf_at(self, frame):
        return next((k for k in self.keyframes if k["frame"] == frame), None)

    def add_keyframe(self, kind):
        """Capture the CURRENT layout geometry as a keyframe at the playhead. An
        existing keyframe on that frame is replaced (that's also how geometry on a
        keyframe is re-recorded: edit the boxes, press K again)."""
        if self._path is None or self.game_box is None:
            self._say("Load a clip first - nothing to keyframe.")
            return
        kf = {
            "frame": int(self.frame_idx),
            "type": kind,
            "game_box": list(self.game_box),
            "cam_box": list(self.cam_box),
            "split_ratio": int(self.split_var.get()),
            # Deep-copied geometry regardless of the enable flag - the flag gates
            # rendering, the choreography travels with the keyframe.
            "highlights": [{"src_box": list(h["src_box"]),
                            "dest_box": list(h["dest_box"])}
                           for h in self.highlights],
        }
        old = self._kf_at(kf["frame"])
        if old is not None:
            self.keyframes.remove(old)
        self.keyframes.append(kf)
        self.keyframes.sort(key=lambda k: k["frame"])
        self.selected_kf = kf
        self._redraw_timeline()
        self._mark_clip_dirty()
        self._say(f"{kind} keyframe {'replaced' if old else 'set'} at F:{kf['frame']}",
                  "green")

    def delete_keyframe(self, at=None):
        """Remove the keyframe at `at` (default: playhead), else the selected one."""
        kf = self._kf_at(self.frame_idx if at is None else int(at)) or self.selected_kf
        if kf is None:
            self._say("No keyframe at the playhead (or selected) to delete.")
            return
        self.keyframes.remove(kf)
        if self.selected_kf is kf:
            self.selected_kf = None
        self._apply_interpolated(self.frame_idx)   # layout may change without it
        self._redraw_timeline()
        self._mark_clip_dirty()
        self._say(f"Keyframe at F:{kf['frame']} deleted.")

    def move_keyframe(self, kf, new_frame):
        """Retime a keyframe; never lets two share a frame (nudges to the nearest free)."""
        new_frame = max(0, min(self.total_frames - 1, int(new_frame)))
        if new_frame == kf["frame"]:
            return
        taken = {k["frame"] for k in self.keyframes if k is not kf}
        if new_frame in taken:
            free = next((c for d in range(1, self.total_frames)
                         for c in (new_frame - d, new_frame + d)
                         if 0 <= c < self.total_frames and c not in taken), None)
            if free is None:
                return
            new_frame = free
        kf["frame"] = new_frame
        self.keyframes.sort(key=lambda k: k["frame"])
        self._apply_interpolated(self.frame_idx)
        self._redraw_timeline()
        self._mark_clip_dirty()

    def interpolate_layout_at(self, frame):
        """Layout dict at `frame` from the live keyframe track (thin wrapper over the
        pure module-level `interpolate_layout` - refactored in 2.6 so the render
        worker can call the same math with a SNAPSHOTTED keyframe list, no self/widget
        reads on the thread)."""
        return interpolate_layout(self.keyframes, frame)

    def _apply_interpolated(self, frame):
        """Write the interpolated layout onto the live boxes/split and refresh. With no
        keyframes this is a no-op and the static layout holds (2.2 behavior)."""
        layout = self.interpolate_layout_at(frame)
        if layout is None:
            return False
        self.game_box = list(layout["game_box"])
        self.cam_box = list(layout["cam_box"])
        # Interpolated highlights replace the keyframed indices' GEOMETRY only; the
        # rest of each model dict (visible range 2.8.7, any future keys) is kept -
        # rebuilding the dicts from bare geometry would silently strip them on every
        # seek. Live extras beyond the keyframed count are HELD, not wiped (a
        # highlight added after choreographing must survive a scrub). Count changes
        # clamp the selection + rebuild the list.
        prev_n = len(self.highlights)
        merged = []
        for i, g in enumerate(layout.get("highlights") or []):
            base = dict(self.highlights[i]) if i < len(self.highlights) else {}
            base["src_box"] = list(g["src_box"])
            base["dest_box"] = list(g["dest_box"])
            merged.append(base)
        merged.extend(self.highlights[len(merged):])
        self.highlights = merged
        if len(self.highlights) != prev_n:
            if not self.highlights:
                self.sel_hl = None
            elif self.sel_hl is not None:
                self.sel_hl = min(self.sel_hl, len(self.highlights) - 1)
            self._refresh_hl_rows()
        # Set the split WITHOUT triggering recalc (the keyframe carries exact geometry;
        # recalculate_box_sizes would re-lock heights and fight the interpolation).
        self._applying = True
        try:
            self.split_var.set(int(layout["split_ratio"]))
        finally:
            self._applying = False
        self._draw_boxes()
        self._schedule_preview_update()
        return True

    def _show_empty(self):
        """Dim hint text in both viewers while nothing is loaded."""
        for c, msg in ((self._source_canvas, "Source - load a clip in Import,\n"
                                             "then open this tab"),
                       (self._preview_canvas, "Preview\n(appears when a clip loads)")):
            c.delete("all")
            c.create_text(c.winfo_width() / 2, c.winfo_height() / 2, text=msg,
                          fill=PALETTE["text_faint"], font=ui_font(10), justify="center")
        # This wipe invalidates update_live_preview's persistent image item (its
        # c.type() check would already catch a stale id, but reset here too so a
        # fresh clip load starts from a clean, unambiguous state).
        self._preview_image_item = None
        self._preview_photo_wh = (0, 0)

    def _set_lamp(self, on, message=None, error=False):
        self._lamp.delete("all")
        draw_lamp(self._lamp, 10, 10, on)
        if message is not None:
            self._status.configure(
                text=message,
                fg=PALETTE["red_soft"] if error else PALETTE["text_mute"])

    def _say(self, message, color_key="text_mute"):
        self._status.configure(text=message, fg=PALETTE[color_key])

    def _set_render_state(self, state):
        """Single writer for the render state (5.5.1c): tracks whether the CURRENT
        clip's edits have been rendered and mirrors it on the action-bar label.
        none = no clip, unrendered = never rendered, stale = edited since the last
        render, rendered = up to date."""
        self._render_state = state
        text, color = {
            "unrendered": ("Not rendered", "red_soft"),
            "stale": ("Edits not rendered", "red_soft"),
            "rendered": ("Rendered", "green"),
        }.get(state, ("", "text_dim"))
        self._render_state_label.configure(text=text, fg=PALETTE[color])

    # ── Bus / lifecycle ──────────────────────────────────────────────────────────────
    def _on_visible(self, _e):
        # Pull the newest import whenever it differs from the loaded clip, not only
        # on first load: a newly imported/dropped file must replace the current one
        # without a restart. Edits persist per clip (2.8.2), so nothing is lost.
        latest = self.bus.latest("raw_clip")
        if latest and latest != self._path:
            self.load_clip(latest)
        # Focus a viewer so frame keys work immediately (otherwise the notebook keeps
        # focus and Left/Right would switch tabs instead of stepping frames).
        self._source_canvas.focus_set()

    def _reload_from_import(self):
        latest = self.bus.latest("raw_clip")
        if latest:
            self.load_clip(latest)
        else:
            self._set_lamp(False, "Nothing in import yet - load a clip in the Import tab.")

    def receive_handoff(self, _data_type, path):
        """Hub push path (Import's 'Send to Editor' routes here)."""
        if path:
            self.load_clip(path)

    def has_unsaved(self):
        """Hub close-guard hook (2.8.3): only in-flight work matters - edits persist
        through the 2.8.2 per-clip block."""
        return "render in progress" if self._rendering else None

    def on_close(self):
        """Hub calls this on shutdown: flush the layout autosave, cancel pending after
        jobs (a debounced preview firing post-destroy throws a Tcl error), release the
        capture."""
        self._closing = True    # workers' marshalled after() calls bail from here on
        self._playing = False   # a tick firing mid-teardown must bail immediately
        self._stop_audio()      # close the output stream (no callback may outlive us)
        if self._render_cancel is not None:
            self._render_cancel.set()
        writer = self._render_writer
        if writer is not None:
            writer.abort()      # kill ffmpeg + remove the partial (no orphan process)
        try:
            self._save_clip_edits()   # flush the per-clip block (cancels its own job)
        except Exception as ex:  # noqa: BLE001 - a failed flush must not block shutdown
            print(f"[editor] clip-edit flush failed: {ex}")
        for attr in ("_autosave_job", "_clipsave_job", "_preview_job", "_settle_job",
                     "_box_draw_job", "_seek_job", "_resize_job", "_render_retry",
                     "_tl_configure_job", "_play_job", "_ui_pump_job",
                     "_undo_coalesce_job"):
            job = getattr(self, attr)
            if job is not None:
                self.after_cancel(job)
                setattr(self, attr, None)
        self._autosave_last()
        self._cancel_warm()
        self._release_cap()
        self._cache.clear()

    # ── Persistent capture + cache ───────────────────────────────────────────────────
    def _release_cap(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001 - releasing must never crash shutdown
                pass
            self._cap = None
        self._cap_pos = -1

    def _open_video(self, path):
        """Open a new clip: release any previous capture, clear the cache."""
        self._cancel_warm()
        self._release_cap()
        self._cache.clear()
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            cap.release()
            return False
        self._cap = cap
        self._cap_pos = 0
        return True

    def _read_frame(self, idx):
        """BGR frame at `idx` - the hot path for scrubbing. LRU hit, else decode on the
        persistent capture. Skips the seek when the capture is already positioned there
        (sequential reads - cache warm-up, forward stepping - avoid a re-seek per frame)."""
        frame = self._cache.get(idx)
        if frame is not None:
            return frame
        if self._cap is None:
            return None
        if idx != self._cap_pos:
            gap = idx - self._cap_pos
            if self._cap_pos >= 0 and 0 < gap <= SEQ_GRAB_MAX:
                # Small forward gap (true-speed frame drops, ±5 stepping): grab()
                # through it - decode-and-discard is far cheaper than cap.set, which
                # re-seeks to the previous H.264 keyframe and re-decodes everything
                # since (tens of frames for a 1-frame hop).
                for _ in range(gap):
                    if not self._cap.grab():
                        self._cap_pos = -1
                        return None
            else:
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = self._cap.read()
        if not ok or frame is None:
            self._cap_pos = -1  # position unknown after a failed read; force a re-seek
            return None
        self._cap_pos = idx + 1
        self._cache.put(idx, frame)
        return frame

    def _warm_cache(self, center, radius=WARM_RADIUS):
        """Pre-decode frames around `center` so first scrubs hit the cache. Chunked over
        `after` ticks - stays on the UI thread (no tkinter-from-thread risk) without
        freezing it for the whole window."""
        if self._cap is None:
            return
        lo = max(0, center - radius)
        hi = min(self.total_frames - 1, center + radius)
        self._warm_queue = list(range(lo, hi + 1))  # ascending = sequential = cheap reads
        self._warm_step()

    def _warm_step(self):
        self._warm_job = None
        if self._cap is None or not self._warm_queue:
            return
        chunk, self._warm_queue = self._warm_queue[:5], self._warm_queue[5:]
        for idx in chunk:
            self._read_frame(idx)
        if self._warm_queue:
            self._warm_job = self.after(15, self._warm_step)

    def _cancel_warm(self):
        self._warm_queue = []
        if self._warm_job is not None:
            self.after_cancel(self._warm_job)
            self._warm_job = None

    # ── Loading ──────────────────────────────────────────────────────────────────────
    def load_clip(self, path):
        self._stop_play(refresh=False)   # a new clip refreshes everything below anyway
        self._save_clip_edits()          # flush the OUTGOING clip's block (no-op if none)
        if not path or not os.path.exists(path):
            self._set_lamp(False, "Clip not found on disk.", error=True)
            return
        if not self._open_video(path):
            self._path = None
            self._show_empty()
            self._set_render_state("none")
            self._set_lamp(False, "Couldn't open the clip (unsupported or corrupt).",
                           error=True)
            return
        meta = video_utils.probe_video(path)
        if meta is None:
            self._release_cap()
            self._path = None
            self._show_empty()
            self._set_render_state("none")
            self._set_lamp(False, "Couldn't read clip metadata.", error=True)
            return

        w, h, fps, n = meta
        self._path = path
        self.orig_w, self.orig_h = w, h
        self.fps = fps or 30.0   # container didn't report fps - assume 30 for time math
        self.total_frames = max(1, n)
        self.frame_idx = 0
        self.keyframes = []          # keyframes are per-clip; a new load starts clean
        self._pending_kf_norm = None  # a stale pending restore must not leak across clips
        self.selected_kf = None
        self._tl_drag_kf = None
        # 2.10.2a: a new clip starts as one whole-clip kept segment (the per-clip block,
        # restored just below, may override it).
        self.segments = self._normalize_segments(
            [[0, self.total_frames - 1]], self.total_frames)
        self._sel_seg = None         # segment selection is per clip (5.5.2b)
        self._refresh_seg_gain_row()
        self._applying = True        # gain is per-clip now (2.8.2): reset; block may override
        try:
            self.gain_var.set(0.0)
            self.sharpen_on.set(False)     # 3.1: sharpen is per-clip too; block restore overrides
            self.sharpen_strength.set(1.0)
        finally:
            self._applying = False
        self._load_waveform()        # off-thread peak extraction for the new clip

        self._scrub.configure(to=self.total_frames - 1)
        self._sync_scrub(0)

        # Restore this clip's saved edit block (2.8.2) when one exists; else the
        # last-used layout (or built-in defaults) seeds the session as before.
        block = ((self.app.config.get("editor") or {}).get("clip_edits") or {}).get(
            self._clip_key(path))
        if block:
            self._apply_clip_edits(block)
        else:
            self._apply_layout(
                (self.app.config.get("editor") or {}).get("last_layout") or {})
        self._render_source_frame()
        self._update_counter()
        self._redraw_timeline()
        # A freshly loaded clip is never rendered THIS session (render products are
        # session-scoped, like the bus). Set after the block restore so programmatic
        # applies can't flip it to stale.
        self._set_render_state("unrendered")
        # Undo history is per clip (5.5.2a): drop the old clip's, seed the new
        # baseline. Boxes can still be pending on a never-mapped tab; _undo_settle
        # then seeds the baseline on the first settled edit instead.
        if self._undo_coalesce_job is not None:
            self.after_cancel(self._undo_coalesce_job)
            self._undo_coalesce_job = None
        self._undo_stack.reset(None)
        self._undo_current = (self._snapshot_clip_edits()
                              if self.game_box is not None and self._disp_w > 0
                              else None)
        self._refresh_undo_buttons()
        self._set_lamp(True, f"{os.path.basename(path)} - {w}×{h} · {self.fps:.1f}fps "
                             f"· {self.total_frames} frames")
        # Store the handle so on_close's _cancel_warm can cancel THIS pending kickoff too
        # (a close within 50ms of a load otherwise fires _warm_cache post-destroy → Tcl
        # "invalid command name" noise). _warm_step clears _warm_job when it runs.
        self._warm_job = self.after(50, self._warm_cache, 0)
        self._maybe_autosave_toast()

    def _maybe_autosave_toast(self):
        """One-shot, discreet notice on the first-ever clip load telling the user edits
        autosave per clip (5.5.5). Pure discoverability - the persistence already exists
        (2.8.2); this just tells them. Flag is set even if show_toast returns None (root
        already gone) so it never re-arms."""
        nudges = self.app.config.setdefault("nudges", {})
        if nudges.get("editor_autosave_toast_shown"):
            return
        nudges["editor_autosave_toast_shown"] = True
        root = getattr(self.app, "root", None)
        if root is not None:
            show_toast(root, "Your edits save automatically",
                       "Every change to this clip is saved as you work, per clip. Open it "
                       "again anytime and your edits are still here.",
                       timeout_ms=7000)
        self.app.save_config()

    # ── Navigation ───────────────────────────────────────────────────────────────────
    def _sync_scrub(self, idx):
        """Set the scrub thumb programmatically. tk.Scale invokes its command via an
        idle callback - AFTER any synchronous guard flag is back to False - so the
        value is also remembered and its echo dropped in _on_scrub. (Found in 2.4:
        the echo otherwise reads as user input and kills playback.)"""
        self._scrub_echo = idx
        self._syncing_scrub = True
        try:
            self._scrub.set(idx)
        finally:
            self._syncing_scrub = False

    def _on_scrub(self, val):
        if self._syncing_scrub:
            return
        idx = int(float(val))
        if idx == self._scrub_echo:
            self._scrub_echo = None   # deferred echo of our own .set - not user input
            return
        self._schedule_seek(idx, from_scrub=True)

    def _schedule_seek(self, idx, from_scrub=False, delay_ms=30):
        """Throttles _seek_to the same way _schedule_preview_update/_schedule_box_draw
        throttle the drag paths: found while chasing a "several seconds to jump to a
        new section" report on long clips. Both the Scale scrub bar's command and the
        timeline's drag-to-scrub (_on_tl_drag) fire this on every single mouse-move
        event, unthrottled; each cache-miss frame decode costs tens to 100+ms on a
        long clip (profiled: a 10-minute 1920x1080/60fps clip with a 5s GOP averaged
        ~70ms/seek), so a fast drag across a long timeline queued dozens of them
        back-to-back, which is exactly what several-second backlog looks like. Only
        the LAST requested position is ever actually rendered - _seek_to reads it
        fresh when the job fires. Outside SPEC_5.5.3a's named scope (a different UI
        surface entirely - the timeline/scrub bar, not the preview canvas)."""
        self._pending_seek_idx = idx
        self._pending_seek_from_scrub = self._pending_seek_from_scrub or from_scrub
        if self._seek_job is not None:
            return
        self._seek_job = self.after(delay_ms, self._fire_seek)

    def _fire_seek(self):
        self._seek_job = None
        idx, self._pending_seek_idx = self._pending_seek_idx, None
        from_scrub, self._pending_seek_from_scrub = self._pending_seek_from_scrub, False
        if idx is not None:
            self._seek_to(idx, from_scrub=from_scrub)

    # The ? cheat-sheet (2.8.8). UPDATE THIS TEXT whenever _on_key (directly below)
    # gains/loses a binding - the two are maintained side by side on purpose.
    # ── Section help bodies (2.10.3). Terse + factual; opened by the section `?` button. ──
    _HELP_SEAM = (
        "The panel division and its boundary.\n"
        "Single panel = ONE 9:16 crop fills the whole frame (no camera box, no seam); "
        "off = two stacked crops, gameplay on top and camera below.\n"
        "Split: where the gameplay panel ends and the camera panel begins, as a % of "
        "frame height (single-panel mode: it positions the decorative line + name plate "
        "instead, free 0-100%). You can also drag the green line in the preview.\n"
        "Melt softens the boundary (cosine cross-blend); Solid border draws a hard line "
        "there instead. Both width sliders are in 1080×1920 reference px.")
    _HELP_BRANDING = (
        "The name plate burned into the output, plus any image overlays.\n"
        "Burn name puts a name plate on the seam; size scales it. Platform icons come "
        "from PNGs you drop in assets/ (twitch.png / youtube.png), laid out side by side "
        "or stacked. Plate L/C/R parks the plate left, centered, or right. Name and "
        "background colors are pickable.\n"
        "Branding / watermark images are separate from the name plate: add as many as "
        "you like (logo, sponsor banner, your avatar, anything), then drag each one in "
        "the preview to move it and drag a corner or edge to resize it. Lock aspect "
        "ratio keeps the image's shape while resizing; off lets you stretch it to any "
        "shape. Opacity fades it.")
    _HELP_CONTENT_BOXES = (
        "The boxes that pick WHAT is shown.\n"
        "Game and cam boxes are dragged/resized directly on the source viewer (left); "
        "the reset buttons snap a lost box back to its default.\n"
        "Highlights are picture-in-picture: drag the ORANGE box on the source to pick "
        "what to show, then place/size it on the preview (right). Appear/Disappear set "
        "the frames it's visible. 'Allow below the seam' lets you place it past the "
        "seam line.")
    _HELP_TRIM = (
        "Set In / Set Out mark the first and last kept frame at the playhead - the outer "
        "bounds of the render. The timeline shades everything outside them.\n\n"
        "Reset clears In/Out and every cut, back to the whole clip as one piece.")
    _HELP_CUT_MARK = (
        "Cut removes a range from the MIDDLE of the clip: press once to mark the start "
        "(a yellow band tracks the playhead), move to the end, press again to remove "
        "that range. It becomes a red band; press without moving to cancel; right-click "
        "a red band to restore it. Render and playback skip everything removed.\n\n"
        "Mark Audio drops a boundary at the playhead without removing any frames - press "
        "it once on each side of a moment to carve out its own segment, then set that "
        "segment's volume in AUDIO. Marking an already-marked frame is a no-op; "
        "right-click a marker on the timeline to delete it.")
    _HELP_KEYFRAMES = (
        "A keyframe is a snapshot of the whole layout (boxes, split, highlights) at one "
        "frame; the editor animates between keyframes.\n"
        "Fluid: the layout blends smoothly into this keyframe from the previous one.\n"
        "Jump: the layout holds, then cuts instantly on the next keyframe's frame.\n"
        "Add with the + Fluid KF / + Jump KF buttons; the keyframe lands at the "
        "playhead as a marker on the timeline. Adding on an occupied frame replaces "
        "it - that is also how you re-record geometry: move the boxes, add again.\n"
        "Move: drag a marker with the RIGHT mouse button to move it in time. It snaps "
        "to seconds and the playhead; hold Shift to place it freely. Left-click a "
        "marker to select it and seek there.\n"
        "Delete: the delete KF button removes the selected keyframe (or the one at the "
        "playhead).\n"
        "Lock KFs stops timeline drags from moving keyframes; adding and deleting "
        "still work.")
    _HELP_AUDIO = (
        "Gain (dB) is applied at render and previewed live during playback.\n"
        "Segment gain: click a kept segment on the timeline's waveform band to select "
        "it, then set an offset that applies to that segment only, on top of the "
        "whole-clip gain. Playback approximates it at segment edges; the render is "
        "exact.\n"
        "Mark Audio (C): drop a boundary at the playhead, independent of cuts - a second "
        "marker nearby carves the piece between them into its own segment you can give "
        "a different volume, without removing any frames. Right-click a marker to "
        "delete it.\n"
        "The waveform is navigation only - it doesn't change the audio.")
    _HELP_RENDER = (
        "Composites the In→Out window into a vertical 1080×1920 Short in data/Shorts/.\n"
        "Eco vs Performance (encode speed vs CPU use) is set on the Home tab.")
    _HELP_SHARPEN = (
        "Sharpens each frame as the Short is rendered, in the same single encode, so it adds "
        "no extra quality loss.\n"
        "It does make the render slower, more so in Performance mode (which composites several "
        "frames at once). Off by default.\n"
        "0.3 to 0.6 is a good starting range - test on your own clips, since the right amount "
        "varies by source quality.\n"
        "Preview and playback don't show sharpening; use the Export tab's Preview to watch the "
        "finished file.")

    _KEYS_CHEATSHEET = (
        "PLAYBACK\n"
        "Space - play / pause (with edits)\n"
        "Shift+Space - play the raw source\n"
        "Ctrl+Space - play raw, looping\n"
        "Ctrl+Shift+Space - play edits, looping\n"
        "(the Loop toggle makes Play / Space loop too)\n"
        "\n"
        "EDITING\n"
        "{undo} - undo the last edit\n"
        "X - redo\n"
        "\n"
        "KEYFRAMES\n"
        "K - fluid keyframe at the playhead (replaces an existing one)\n"
        "Shift+K - jump keyframe\n"
        "Delete - delete the keyframe at the playhead (or selected)\n"
        "right-drag a marker - move it in time (snaps to seconds and the\n"
        "playhead; hold Shift for free placement; Lock KFs disables this)\n"
        "\n"
        "TRIM AND CUTS\n"
        "I - Set In at the playhead\n"
        "O - Set Out at the playhead\n"
        "V - cut: press to mark the start, press again to remove [start ... playhead]\n"
        "(right-click a removed band on the timeline to restore it)\n"
        "C - mark audio: drop a boundary at the playhead; a second marker nearby "
        "encloses a segment - set its volume in AUDIO\n"
        "(right-click a marker on the timeline to delete it)\n"
        "\n"
        "NAVIGATION\n"
        "← → or A / D - step 5 frames\n"
        "Shift+step - 1 frame   ·   Ctrl+step - 10 frames\n"
        "\n"
        "HELP\n"
        "? - this cheat-sheet"
    )

    def _undo_key(self):
        """The undo key for the configured keyboard layout (5.5.2b correction):
        Y on QWERTZ (the app's home layout), Z on QWERTY - the same physical key
        beside X. Read fresh from config so a Home change applies immediately."""
        return "z" if self.app.config.get("keyboard_layout") == "qwerty" else "y"

    def _show_cheatsheet(self):
        HelpDialog(self, title="Editor keys",
                   body=self._KEYS_CHEATSHEET.format(undo=self._undo_key().upper()))

    def _on_key(self, event):
        # Don't steal keys while the user is typing in a field (ttk Entry/Spinbox/
        # Combobox subclass tk.Entry, so this covers both flavors).
        if isinstance(self.focus_get(), (tk.Entry, tk.Spinbox, tk.Text)):
            return None
        sym = event.keysym
        if sym == "question":
            self._show_cheatsheet()
            return "break"
        if sym == "space":
            # The Space matrix: plain=edits · Shift=raw · Ctrl=raw+loop ·
            # Ctrl+Shift=edits+loop. So loop follows Ctrl; edits whenever the two
            # modifiers agree (both off or both on). The Loop toggle also forces loop on.
            shift = bool(event.state & 0x0001)
            ctrl = bool(event.state & 0x0004)
            self._toggle_play(edits=(shift == ctrl), loop=(ctrl or self._loop_var.get()))
            return "break"
        if sym in ("k", "K"):
            # Stop first: during playback the live boxes are stale (ticks interpolate
            # purely) - the stop refresh re-applies the layout under the playhead, so
            # the keyframe captures what's actually on screen.
            self._stop_play()
            self.add_keyframe("jump" if event.state & 0x0001 else "fluid")
            return "break"
        if sym == "Delete":
            self._stop_play()
            self.delete_keyframe()
            return "break"
        if sym in ("i", "I"):
            self.set_in_point()
            return "break"
        if sym in ("o", "O"):
            self.set_out_point()
            return "break"
        if sym in ("v", "V"):           # 2.10.2d: one-button cut - mark start, then remove
            self._stop_play()
            self._toggle_cut()
            return "break"
        if sym in ("c", "C"):           # 5.5.2f: drop a single audio marker at the playhead
            self._stop_play()
            self._mark_audio()
            return "break"
        if sym in ("y", "Y", "z", "Z"):
            # 5.5.2a undo, layout-aware (5.5.2b correction): Y on QWERTZ, Z on
            # QWERTY - the same physical key next to X. The other letter stays free.
            if sym.lower() == self._undo_key():
                self._stop_play()
                self._do_undo()
                return "break"
            return None
        if sym in ("x", "X"):           # 5.5.2a: redo (same position on both layouts)
            self._stop_play()
            self._do_redo()
            return "break"
        if sym in ("Left", "a", "A"):
            direction = -1
        elif sym in ("Right", "d", "D"):
            direction = 1
        else:
            return None
        if event.state & 0x0004:      # Ctrl
            step = 10
        elif event.state & 0x0001:    # Shift
            step = 1
        else:
            step = 5
        self._nudge(direction * step)
        return "break"  # keep the keys from also scrolling/refocusing widgets

    def _nudge(self, delta):
        if self._path is None:
            return
        self._seek_to(self.frame_idx + delta)

    def _seek_to(self, idx, from_scrub=False):
        """Move the playhead to frame `idx` (clamped) and refresh viewers + counter.
        Any seek during playback is user input (ticks never come through here) - it
        stops playback; the seek itself then does the full static refresh."""
        if self._path is None or self.total_frames <= 0:
            return
        idx = max(0, min(self.total_frames - 1, int(idx)))
        was_playing = self._playing
        if was_playing:
            self._stop_play(refresh=False)
        if idx == self.frame_idx and self._source_disp is not None and not was_playing:
            return
        self.frame_idx = idx
        if not from_scrub:
            self._sync_scrub(idx)
        self._apply_interpolated(idx)   # keyframes drive the layout; no-op without any
        self._render_source_frame()
        self._update_counter()
        self._redraw_timeline()         # playhead moved

    def _update_counter(self):
        t = self.frame_idx / self.fps
        dur = self.total_frames / self.fps
        self._counter.configure(text=f"{t:.2f}s / {dur:.2f}s   F:{self.frame_idx}")

    # ── Playback engine (2.4): `after`-clock loop, Space matrix, frame-drop ──────────
    def _toggle_play(self, edits, loop):
        """Transport/Space entry. Same combo while playing → stop (toggle); a different
        combo → restart from the current frame with the new flags; stopped → start."""
        if self._playing:
            if edits == self._play_edits and loop == self._play_loop:
                self._stop_play()
                return
            self._stop_play(refresh=False)   # restarting right away - skip the refresh
        self._start_play(edits, loop)

    def _start_play(self, edits, loop):
        if self._path is None or self.total_frames <= 1:
            return
        # 2.9.3: snapshot the trim toggle for the whole play (a mid-play flip waits for
        # the next start - predictable).
        self._play_trim = bool(self._play_trim_var.get())
        # 2.10.2c: edits playback across MULTIPLE kept segments runs on the output timeline
        # (cuts skipped). One segment OR raw playback (cuts don't apply to raw) → the exact
        # 2.9.3 path, provably identical.
        self._play_multiseg = edits and len(self.segments) > 1
        if self._play_multiseg:
            segs, kt = self.segments, self._kept_total(self.segments)
            out0 = self._output_of_source(segs, self.frame_idx)
            if out0 is None:                       # playhead on a removed frame → forward
                out0 = self._next_output_of_source(segs, self.frame_idx)
            if self._play_trim:
                if out0 >= kt - 1:                 # outside/at-end → start at the first kept
                    out0 = 0
            elif out0 >= kt - 1 and not loop:
                out0 = 0                           # parked at the end, no loop → from the top
            self._play_output0 = out0
            self.frame_idx = self._source_of_output(segs, out0)
        else:
            # K==1 / raw: clamp into [In, Out] for trim, or rewind from the clip end.
            if self._play_trim and self.out_frame > self.in_frame:
                if not (self.in_frame <= self.frame_idx < self.out_frame):
                    self._seek_to(self.in_frame)
            elif self.frame_idx >= self.total_frames - 1 and not loop:
                self._seek_to(0)   # play pressed parked on the last frame: run from the top
        self._playing = True
        self._play_edits = edits
        self._play_loop = loop
        self._play_frame0 = self.frame_idx
        self._play_clock0 = perf_counter()
        self._play_btn.configure(text="⏸")
        self._say("Playing - " + ("edits" if edits else "raw")
                  + (" · loop" if loop else ""))
        # 5.5.2b: seed the segment factor BEFORE the stream opens (the callback reads
        # it immediately). Multiseg ticks keep it current across boundaries.
        self._update_seg_gain(self.frame_idx, edits)
        self._start_audio()   # may stay silent - the tick falls back to the wall clock
        self._play_tick()

    def _stop_play(self, refresh=True):
        """Stop playback. `refresh` restores the static view (interpolated layout under
        the playhead, source boxes, full-res preview); callers that refresh themselves
        right after (seek, combo restart, load) pass False."""
        self._stop_audio()
        if self._play_job is not None:
            self.after_cancel(self._play_job)
            self._play_job = None
        if not self._playing:
            return
        self._playing = False
        self._play_btn.configure(text="▶")
        if refresh:
            self._sync_scrub(self.frame_idx)
            self._apply_interpolated(self.frame_idx)
            self._render_source_frame()
            self._update_counter()
            self._redraw_timeline()
            self._say(f"Stopped at F:{self.frame_idx}.")

    def _play_tick(self):
        self._play_job = None
        if not self._playing:
            return
        if self._play_multiseg:            # 2.10.2c: clocks run on the kept/output timeline
            self._play_tick_multiseg()
            return
        # 2.9.3: the playback window. "In→Out only" caps the end at Out and loops/wraps
        # to In; off = the whole clip (loop returns to where play started). A degenerate
        # window (Out ≤ In) falls back to full-clip so playback never traps.
        if self._play_trim and self.out_frame > self.in_frame:
            lo, hi = self.in_frame, self.out_frame
        else:
            lo, hi = self._play_frame0, self.total_frames - 1
        # Always true-speed (2.8.6, user decision): jump to wherever the clock says
        # playback should be - frames in between are dropped. The clock is the audio
        # stream's sample cursor when sound is playing (video chases audio = A/V
        # sync for free), else the perf_counter wall clock (silent fallback).
        if self._stream is not None:
            target = self._play_frame0 + int(round(
                (self._audio_pos - self._audio_start_sample)
                / self._pcm_rate * self.fps))
        else:
            elapsed = perf_counter() - self._play_clock0
            target = self._play_frame0 + int(round(elapsed * self.fps))
        if target > hi:
            if self._play_loop:
                # Wrap to the loop point (In in trim mode, the start frame otherwise).
                # Reset the frame/audio zero to `lo` so the clocks count from there - a
                # no-op in full-clip mode (lo == _play_frame0 already).
                self._play_clock0 = perf_counter()
                self._play_frame0 = lo
                if self._stream is not None:   # audio jump-cut back to the loop point
                    sample = int(lo / self.fps * self._pcm_rate)
                    self._audio_seek = sample
                    self._audio_start_sample = sample
                target = lo
            else:
                self.frame_idx = hi       # stop's refresh lands on the window's end
                self._stop_play()
                return
        if target != self.frame_idx:      # true-speed: clock may not have advanced yet
            self.frame_idx = target
            self._render_play_frame(target, self._play_edits)
            self._update_counter()
            self._move_timeline_playhead()
            self._sync_scrub(target)      # cheap thumb follow (echo-filtered)
        self._play_job = self.after(max(1, int(1000 / self.fps)), self._play_tick)

    def _play_tick_multiseg(self):
        """2.10.2c: edits playback over the KEPT (output) timeline. The clock yields an
        OUTPUT frame; the 2.10.2a mapper turns it into the SOURCE frame to composite, so
        removed ranges are skipped and the playhead always holds a real source frame. Audio
        plays the kept-PCM (the output timeline), so video chases it across cuts. Window in
        OUTPUT space: trim ON = the whole kept span [0, kt-1] looping to 0; OFF = from the
        start position to the end. Frame-drop/true-speed logic mirrors the K==1 tick."""
        segs = self.segments
        kt = self._kept_total(segs)
        if self._play_trim:
            lo_out, hi_out = 0, kt - 1
        else:
            lo_out, hi_out = self._play_output0, kt - 1
        if self._stream is not None:
            target_out = self._play_output0 + int(round(
                (self._audio_pos - self._audio_start_sample)
                / self._pcm_rate * self.fps))
        else:
            elapsed = perf_counter() - self._play_clock0
            target_out = self._play_output0 + int(round(elapsed * self.fps))
        if target_out > hi_out:
            if self._play_loop:
                self._play_clock0 = perf_counter()
                self._play_output0 = lo_out
                if self._stream is not None:   # jump the kept-PCM cursor back to the loop pt
                    sample = int(lo_out / self.fps * self._pcm_rate)
                    self._audio_seek = sample
                    self._audio_start_sample = sample
                target_out = lo_out
            else:
                self.frame_idx = self._source_of_output(segs, hi_out)
                self._stop_play()
                return
        target_src = self._source_of_output(segs, target_out)
        self._update_seg_gain(target_src, True)   # 5.5.2b: follow segment boundaries
        if target_src != self.frame_idx:   # true-speed: clock may not have advanced yet
            self.frame_idx = target_src
            self._render_play_frame(target_src, self._play_edits)
            self._update_counter()
            self._move_timeline_playhead()
            self._sync_scrub(target_src)
        self._play_job = self.after(max(1, int(1000 / self.fps)), self._play_tick)

    def _render_play_frame(self, frame_idx, edits):
        """One playback frame into the preview canvas - composited at PREVIEW
        resolution (edits) or the bare source frame scaled to fit (raw). Touches
        nothing but the preview canvas; static boxes/inspector are left alone."""
        src = self._read_frame(frame_idx)
        if src is None:
            return
        c = self._preview_canvas
        pw, ph = c.winfo_width(), c.winfo_height()
        if pw < 20 or ph < 20:
            return
        if edits and self.game_box is not None:
            layout = self.interpolate_layout_at(frame_idx)
            if layout is None:   # no keyframes: the live static layout plays
                game_box, cam_box = self.game_box, self.cam_box
                split = int(self.split_var.get())
                hls = self.highlights
            else:
                game_box, cam_box = layout["game_box"], layout["cam_box"]
                split = layout["split_ratio"]
                hls = layout.get("highlights") or []
            fit = min(pw / OUT_W, ph / OUT_H)
            out_w, out_h = max(2, int(OUT_W * fit)), max(2, int(OUT_H * fit))
            shown = composite_frame(
                src,
                out_w=out_w, out_h=out_h,
                scale_x=self.scale_x, scale_y=self.scale_y,
                game_box=game_box, cam_box=cam_box,
                split_ratio=split,
                melt=self.melt_on.get(),
                melt_px=int(self.melt_px.get()),
                solid_border=self.border_on.get(),
                border_color=_hex_to_bgr(self._border_hex, (15, 15, 15)),
                border_px=int(self.border_px.get()),
                watermark=self._snapshot_watermark(),
                branding=self._snapshot_branding(),
                highlights=(filter_visible(hls, self.highlights, frame_idx)
                            if self.hl_on.get() else ()),
                single_panel=self.single_panel.get(),
            )
        else:
            sh, sw = src.shape[:2]
            fit = min(pw / sw, ph / sh)
            out_w, out_h = max(2, int(sw * fit)), max(2, int(sh * fit))
            shown = cv2.resize(src, (out_w, out_h), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(shown, cv2.COLOR_BGR2RGB)
        self._preview_photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        c.delete("all")
        c.create_image((pw - out_w) // 2, (ph - out_h) // 2,
                       image=self._preview_photo, anchor="nw")

    # ── Audible playback (2.8.6) ─────────────────────────────────────────────────────
    def _on_mute_toggle(self):
        self._muted = bool(self._mute_var.get())

    def _on_play_trim_toggle(self):
        """Persist the "In→Out only" pref (a global editor setting). Takes effect on the
        next play start - _start_play snapshots it into self._play_trim."""
        self.app.config.setdefault("editor", {})["play_in_out_only"] = bool(
            self._play_trim_var.get())
        self.app.save_config()

    def _on_kf_lock_toggle(self):
        """Persist the "Lock KFs" pref (5.5.1b, a global editor setting). Enforcement
        lives in _on_tl_rpress: a locked marker never arms the retime drag."""
        self.app.config.setdefault("editor", {})["lock_keyframes"] = bool(
            self._kf_lock_var.get())
        self.app.save_config()

    def _on_loop_toggle(self):
        """Persist the "Loop" pref (a global editor setting). Like In→Out only it takes
        effect on the next play start - _start_play snapshots loop into self._play_loop."""
        self.app.config.setdefault("editor", {})["play_loop"] = bool(self._loop_var.get())
        self.app.save_config()

    def _on_gain_write(self, *_):
        """gain_var write-trace: live preview gain (the callback reads _gain_lin as a
        plain float) + the 2.8.2 per-clip dirty mark. Mid-typing garbage reads as 0dB
        until the number completes."""
        try:
            db = float(self.gain_var.get())
        except (tk.TclError, ValueError):
            db = 0.0
        self._gain_lin = 10.0 ** (max(-30.0, min(30.0, db)) / 20.0)
        self._mark_clip_dirty()
        self._redraw_timeline()   # the waveform amplitude tracks the gain (5.5.2b)

    def _refresh_seg_gain_row(self):
        """Sync the segment-gain spinbox + context label to the current selection
        (5.5.2b). Programmatic writes run under _applying so the trace stays quiet."""
        if not hasattr(self, "_seg_gain_spin"):
            return   # early segments change during construction
        sel = self._sel_seg
        valid = sel is not None and sel < len(self.segments)
        was_applying, self._applying = self._applying, True
        try:
            self.seg_gain_var.set(
                float(self.segments[sel][2]) if valid else 0.0)
        finally:
            self._applying = was_applying
        self._seg_gain_spin.configure(state="normal" if valid else "disabled")
        self._seg_ctx.configure(
            text=(f"segment {sel + 1}/{len(self.segments)}" if valid
                  else "no segment selected"))

    def _on_seg_gain_write(self, *_):
        """seg_gain_var write-trace (5.5.2b): clamp, write the selected segment's
        offset, funnel (persistence + undo + stale render), refresh the dB tag and
        the live playback factor."""
        if (self._applying or self._sel_seg is None
                or self._sel_seg >= len(self.segments)):
            return
        try:
            db = float(self.seg_gain_var.get())
        except (tk.TclError, ValueError):
            db = 0.0
        db = max(-30.0, min(30.0, db))
        seg = self.segments[self._sel_seg]
        if float(seg[2]) != db:
            seg[2] = db
            self._mark_clip_dirty()
            self._redraw_timeline()
            if self._playing and self._play_edits:
                self._update_seg_gain(self.frame_idx, True)

    def _update_seg_gain(self, src_idx, edits):
        """The playback approximation factor (5.5.2b): the gain of the segment under
        `src_idx` as a plain float for the audio callback (same lock-free pattern as
        _gain_lin/_muted). Raw playback ignores segment gains - they are edits.
        Boundary accuracy is the tick interval; the render is exact."""
        if edits:
            for seg in self.segments:
                if seg[0] <= src_idx <= seg[1]:
                    db = float(seg[2]) if len(seg) > 2 else 0.0
                    self._seg_gain_lin = 10.0 ** (db / 20.0)
                    return
        self._seg_gain_lin = 1.0

    def _build_play_pcm(self):
        """The kept-PCM for cut-aware audio (2.10.2c): the kept sample ranges of `_pcm`
        concatenated, so it IS the output timeline. Cached, keyed on the current segments
        (rebuilt when they change; clips are short). None when there's no source audio.
        A single segment yields exactly `_pcm[in_sample:out_sample]` - but multiseg playback
        is the only caller (K==1 plays `_pcm` directly)."""
        if self._pcm is None or self._pcm_rate <= 0:
            return None
        # Keyed on the RANGES only: a segment-gain change (5.5.2b) does not alter the
        # samples (gain is applied in the callback), so it must not force a rebuild.
        key = tuple((seg[0], seg[1]) for seg in self.segments)
        if self._play_pcm is not None and self._play_pcm_key == key:
            return self._play_pcm
        rate = self._pcm_rate
        parts = [self._pcm[int(round(s / self.fps * rate)):
                           int(round((e + 1) / self.fps * rate))]
                 for s, e, *_ in self.segments]
        self._play_pcm = np.vstack(parts) if parts else None
        self._play_pcm_key = key
        return self._play_pcm

    def _start_audio(self):
        """Open a fresh OutputStream at the playhead (one per play). ANY failure -
        no sounddevice, no device, bad rate - degrades to silent playback (stream
        stays None, the tick uses the wall clock); a sound failure can never break
        the editor. 2.10.2c: multiseg edits playback streams the KEPT-PCM (the output
        timeline) and indexes the cursor by OUTPUT frame; K==1/raw streams `_pcm`."""
        self._stop_audio()
        if sd is None or self._pcm is None or self._pcm_rate <= 0:
            return
        rate = self._pcm_rate
        if self._play_multiseg:
            pcm = self._build_play_pcm()
            if pcm is None:
                return
            start = min(int(self._play_output0 / self.fps * rate), len(pcm))
        else:
            pcm = self._pcm
            start = min(int(self.frame_idx / self.fps * rate), len(pcm))
        self._active_pcm = pcm
        self._audio_pos = start
        self._audio_start_sample = start
        self._audio_seek = None
        # finished_callback arrives LATE (PortAudio thread → _marshal → 50ms pump) -
        # by then a NEXT play may own a fresh stream. The token identifies WHICH
        # stream died so a stale death can't kill its successor (found empirically:
        # without it, every second consecutive play went silent).
        token = object()
        try:
            stream = sd.OutputStream(
                samplerate=rate, channels=pcm.shape[1], dtype="float32",
                callback=self._audio_cb,
                finished_callback=lambda: self._marshal(self._audio_died, token))
            stream.start()
        except Exception as ex:  # noqa: BLE001 - device/PortAudio failures of any kind
            print(f"[editor] audio unavailable: {ex}")
            self._say("No audio device - playing silent.")
            return
        self._stream = stream
        self._audio_token = token

    def _stop_audio(self):
        """Stop+close the playback stream. Idempotent, never raises (runs in stop/
        load/unmap/close paths where nothing may crash). Nulls _stream FIRST so the
        finished_callback's _audio_died sees a normal stop and bails."""
        self._audio_token = None   # any finished_callback from this stream is now stale
        self._seg_gain_lin = 1.0   # segment factor resets with the stream (5.5.2b)
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _audio_cb(self, outdata, frames, _time_info, _status):
        """PortAudio's own thread: feed float32 from the int16 PCM at the cursor.
        Reads numpy slices + plain attributes ONLY - threads never touch tkinter,
        and plain attribute reads/writes are GIL-atomic, so no lock. Past the clip
        end it pads silence and KEEPS advancing the cursor: the video clock must
        run on so the tick can reach the end-stop. 2.10.2c: `_active_pcm` is the buffer
        the current stream plays - `_pcm` (raw/K==1) or the kept-PCM (multiseg edits)."""
        pcm = self._active_pcm
        if pcm is None:
            outdata.fill(0.0)
            return
        pos = self._audio_pos
        seek = self._audio_seek          # one-slot seek from the UI thread (loop wrap)
        if seek is not None:
            self._audio_seek = None
            pos = seek
        chunk = pcm[pos:pos + frames]
        k = (0.0 if self._muted
             else self._gain_lin * self._seg_gain_lin) / 32768.0
        out = chunk.astype(np.float32) * k
        if len(out) < frames:            # past clip end: pad, keep the clock running
            out = np.vstack([out, np.zeros((frames - len(out), pcm.shape[1]),
                                           np.float32)])
        outdata[:] = out
        self._audio_pos = pos + frames

    def _audio_died(self, token):
        """Marshalled finished_callback: a stream ended. Normal stops invalidate the
        token first and land here as a no-op - as does a PREVIOUS play's late death
        arriving after a new stream opened (the token names which stream died). An
        UNEXPECTED death of the CURRENT stream (device unplugged or claimed
        mid-play) falls back to the wall clock - rebased at the current frame so
        the tempo doesn't jump - and playback continues silent."""
        if token is not self._audio_token or self._stream is None:
            return
        self._stop_audio()
        if self._playing:
            # Rebase the wall clock at the current position so the tempo doesn't jump. In
            # multiseg the clock counts OUTPUT frames from _play_output0 (2.10.2c).
            if self._play_multiseg:
                cur = self._output_of_source(self.segments, self.frame_idx)
                if cur is None:
                    cur = self._play_output0
                self._play_clock0 = perf_counter() - (cur - self._play_output0) / self.fps
            else:
                self._play_clock0 = perf_counter() - (
                    (self.frame_idx - self._play_frame0) / self.fps)
            self._say("Audio device lost - continuing silent.")

    # ── Trim / segments (2.10.2a: ordered KEPT source-frame ranges) ─────────────────
    # `segments` is the single source of truth. in_frame/out_frame are the OUTER bounds of
    # the kept set, derived read-only so the heavy existing readers (timeline shading, the
    # play window, the render window) keep working unchanged. Every mutation routes through
    # _normalize_segments - the one home of the segment arithmetic (cf. _norm_box for boxes).
    @property
    def in_frame(self):
        """Outer In bound = first kept source frame (derived from segments)."""
        return self.segments[0][0]

    @property
    def out_frame(self):
        """Outer Out bound = last kept source frame (derived from segments)."""
        return self.segments[-1][1]

    @staticmethod
    def _normalize_segments(raw, total, merge=True):
        """Coerce a raw segment list into the invariant form: [start, end, gain_db]
        (ints + a float dB offset, 5.5.2b), clamped to [0, total-1] / ±30dB, drop e<s,
        sort by start. When merge=True (the default), overlapping and adjacent ranges
        MERGE ONLY when their gains are EQUAL (differing gains are distinct audio
        regions; a merge keeps the LEFT gain). merge=False keeps every input range
        separate (still clipping a true overlap to the earlier segment's end, so
        ranges never overlap) - used to restore an undo/redo snapshot exactly, since
        a snapshot was already valid when captured and a merge pass would silently
        erase a Mark Audio boundary (5.5.2f) left at 0 dB. Legacy 2-length entries
        read as 0.0; output is ALWAYS 3-length. Empty result → the whole clip (never
        zero kept frames). Static + total-driven so render (off-thread) and tests can
        call it."""
        hi = max(0, int(total) - 1)
        cleaned = []
        for seg in raw or []:
            try:
                s, e = int(seg[0]), int(seg[1])
                g = float(seg[2]) if len(seg) > 2 else 0.0
            except (TypeError, ValueError, IndexError):
                continue
            s = max(0, min(s, hi))
            e = max(0, min(e, hi))
            g = max(-30.0, min(30.0, g))
            if e >= s:
                cleaned.append([s, e, g])
        cleaned.sort(key=lambda seg: seg[0])
        merged = []
        for s, e, g in cleaned:
            if merge and merged and s <= merged[-1][1] + 1 and merged[-1][2] == g:
                merged[-1][1] = max(merged[-1][1], e)   # overlap/touch + equal gain
            elif merged and s <= merged[-1][1]:
                # True overlap (hand-edited block, edge input; or merge=False holding
                # a boundary a caller never expects to overlap): clip to keep the
                # no-overlap invariant, drop if nothing survives.
                s = merged[-1][1] + 1
                if s <= e:
                    merged.append([s, e, g])
            else:
                merged.append([s, e, g])
        return merged or [[0, hi, 0.0]]

    @staticmethod
    def _kept_frames(segments):
        """Yield source frame indices in OUTPUT order: every kept frame, segment by segment."""
        for s, e, *_ in segments:
            yield from range(s, e + 1)

    @staticmethod
    def _kept_total(segments):
        """Number of output frames = sum of segment lengths."""
        return sum(e - s + 1 for s, e, *_ in segments)

    @staticmethod
    def _output_of_source(segments, src_idx):
        """Output frame index of a kept source frame, or None if src_idx is in a removed gap."""
        base = 0
        for s, e, *_ in segments:
            if s <= src_idx <= e:
                return base + (src_idx - s)
            base += e - s + 1
        return None

    @staticmethod
    def _source_of_output(segments, out_idx):
        """Source frame index for an output frame index, clamped into the kept set."""
        if out_idx < 0:
            return segments[0][0]
        base = 0
        for s, e, *_ in segments:
            length = e - s + 1
            if out_idx < base + length:
                return s + (out_idx - base)
            base += length
        return segments[-1][1]               # past the end → last kept frame

    @staticmethod
    def _next_output_of_source(segments, src_idx):
        """OUTPUT index of the first kept source frame >= src_idx (2.10.2c). A src inside a
        kept range maps exactly; a src in a removed gap clamps FORWARD to the next kept
        frame; past the last segment → the last output index. Used to start playback when
        the playhead sits in a cut (never play a removed frame)."""
        base = 0
        for s, e, *_ in segments:
            if src_idx <= e:
                return base + max(0, src_idx - s)
            base += e - s + 1
        return base - 1                      # past every segment → last output frame

    @staticmethod
    def _remove_range(segments, a, b):
        """Subtract the inclusive SOURCE range [a,b] from the kept segments - splitting any
        overlapping segment into its surviving left/right pieces, dropping fully-covered
        ones (2.10.2d). REFUSES to empty the kept set: if nothing would remain, returns the
        input unchanged (a copy). Pure list → list; the caller normalizes."""
        a, b = (int(a), int(b)) if a <= b else (int(b), int(a))
        result = []
        for seg in segments:
            s, e = seg[0], seg[1]
            g = float(seg[2]) if len(seg) > 2 else 0.0   # splits inherit the gain
            if e < a or s > b:               # no overlap → keep whole
                result.append([s, e, g])
            else:
                if s < a:                    # surviving left piece
                    result.append([s, a - 1, g])
                if e > b:                    # surviving right piece
                    result.append([b + 1, e, g])
        return result or [list(seg) for seg in segments]   # never zero kept frames

    @staticmethod
    def _insert_marker(segments, f):
        """Insert a single audio-region boundary right after SOURCE frame f, splitting
        whichever kept segment contains it into two independent segments [s, f, g] and
        [f+1, e, g] (both inherit the parent gain), WITHOUT removing any frames (5.5.2f).
        A second marker elsewhere then carves the piece between the two into its own
        segment - the same result as marking a range, one boundary at a time. A marker
        that falls inside a removed gap, or lands where a boundary already exists
        (f == a segment's last kept frame), is a no-op. Pure list -> list."""
        f = int(f)
        result = []
        for seg in segments:
            s, e = seg[0], seg[1]
            g = float(seg[2]) if len(seg) > 2 else 0.0
            if s <= f < e:                   # a real new boundary (f==e is already one)
                result.append([s, f, g])
                result.append([f + 1, e, g])
            else:
                result.append([s, e, g])
        return result

    @staticmethod
    def _delete_marker(segments, f):
        """Merge the two segments meeting at boundary f (the last kept frame of the
        LEFT one) back into one, deleting a Mark Audio boundary (5.5.2f). Regardless
        of gain - a deliberate override, unlike normalize's equal-gain-only merge; the
        merged piece keeps the LEFT segment's gain (same convention as _restore_gap).
        A no-op (unchanged copy) if no gap-free boundary actually sits at f."""
        result = [list(seg) for seg in segments]
        for i in range(len(result) - 1):
            if result[i][1] == f and result[i + 1][0] == f + 1:
                result[i][1] = result[i + 1][1]
                del result[i + 1]
                break
        return result

    @staticmethod
    def _add_range(segments, a, b):
        """Union the inclusive SOURCE range [a,b] back into the kept segments (restore a
        cut). Pure list → list; the caller normalizes (which merges overlaps/adjacency)."""
        a, b = (int(a), int(b)) if a <= b else (int(b), int(a))
        return [list(seg) for seg in segments] + [[a, b]]

    def set_in_point(self):
        if self._path is None:
            return
        new_in = min(self.frame_idx, self.total_frames - 2)
        segs = [list(s) for s in self.segments]
        segs[0][0] = new_in
        # Mirror the old guard: In meeting/passing Out pushes Out to the clip end (so a
        # single segment never collapses). Normalize handles every other case.
        if segs[-1][1] <= new_in:
            segs[-1][1] = self.total_frames - 1
        # merge=False: moving In/Out only ever touches the outer segments, so a global
        # equal-gain merge here could only ever erase an unrelated Mark Audio boundary
        # elsewhere in the clip, never anything this operation itself created.
        self.segments = self._normalize_segments(segs, self.total_frames, merge=False)
        self._after_segments_changed()
        self._undo_commit()   # a discrete keyboard/button edit is its own undo step
        self._say(f"In point set at F:{self.in_frame}.", "green")

    def set_out_point(self):
        if self._path is None:
            return
        new_out = max(self.frame_idx, self.in_frame + 1)
        segs = [list(s) for s in self.segments]
        segs[-1][1] = new_out
        self.segments = self._normalize_segments(segs, self.total_frames, merge=False)
        self._after_segments_changed()
        self._undo_commit()
        self._say(f"Out point set at F:{self.out_frame}.", "green")

    def reset_trim(self):
        if self._path is None:
            return
        self.segments = self._normalize_segments(
            [[0, self.total_frames - 1]], self.total_frames)
        self._after_segments_changed()

    def _after_segments_changed(self):
        """Common tail for every segment mutation: refresh the timeline (the trim/cut
        readout since the 5.5.1a corrections - shaded ends, red bands) and mark the
        per-clip block dirty. 2.10.2c: invalidate the cached kept-PCM so the next play
        rebuilds it."""
        self._play_pcm = None
        self._play_pcm_key = None
        # 5.5.2b: a cut/restore/trim can shift or drop the selected segment index -
        # clamp it away rather than let it point at a different range.
        if self._sel_seg is not None and self._sel_seg >= len(self.segments):
            self._sel_seg = None
        self._refresh_seg_gain_row()
        self._redraw_timeline()
        self._mark_clip_dirty()

    # ── Cuts (2.10.2d: remove/restore internal segments via the timeline) ────────────
    def _toggle_cut(self):
        """One-button cut. First press marks the start at the playhead - a yellow band then
        tracks the playhead so the selection is VISIBLE. Second press removes
        [start .. playhead] (it becomes a red band, skipped by playback + render). Pressing
        again at the same frame cancels. The normalizer refuses to empty the kept set."""
        if self._path is None:
            return
        if self._cut_start is None:
            self._cut_start = self.frame_idx
            self._refresh_cut_btn()
            self._redraw_timeline()        # show the pending start + tracking band
            self._say(f"Cut started at F:{self.frame_idx} - move the playhead, then press "
                      f"Cut (V) again to remove.", "yellow")
            return
        a, b = sorted((self._cut_start, self.frame_idx))
        self._cut_start = None
        self._refresh_cut_btn()
        if a == b:                         # no movement → treat the second press as cancel
            self._redraw_timeline()
            self._say("Cut cancelled.", "yellow")
            return
        # merge=False: _remove_range only ever splits a segment into a left/right pair
        # with a real gap between them (never two touching pieces), so the equal-gain
        # merge here never serves the cut itself - it only ever erodes an unrelated
        # Mark Audio boundary (5.5.2f) sitting somewhere else in the clip.
        new = self._normalize_segments(
            self._remove_range(self.segments, a, b), self.total_frames, merge=False)
        if new == [list(s) for s in self.segments]:
            self._redraw_timeline()
            self._say("Nothing to remove there - that range is already cut (or it would "
                      "empty the clip).", "red_soft")
            return
        self.segments = new
        self._after_segments_changed()
        self._undo_commit()   # a discrete keyboard/button edit is its own undo step
        self._say(f"Removed F:{a}-{b}.", "green")

    def _refresh_cut_btn(self):
        """Reflect the armed cut state on the Cut button. Text only: themed_button's
        hover bindings restore the creation-time colors on <Leave>, so a color mutation
        would not survive a hover."""
        armed = self._cut_start is not None
        self._cut_btn.configure(text="End Cut (V)" if armed else "Cut (V)")

    # ── Audio markers (5.5.2f: mark arbitrary boundaries for their own volume) ───────
    def _mark_audio(self):
        """Drop a single audio-region boundary at the playhead (5.5.2f), splitting the
        segment it falls in without removing any frames. A second marker elsewhere then
        encloses the piece between the two as its own segment - simpler than the Cut
        gesture on purpose: no start/end pairing, one press per marker. Selects the
        newly-bounded segment (the piece ending at the marker) and focuses the
        segment-gain field so its volume can be set right away."""
        if self._path is None:
            return
        f = max(0, min(self.frame_idx, self.total_frames - 1))
        before = [list(s) for s in self.segments]
        new = self._insert_marker(self.segments, f)
        if new == before:
            in_gap = not any(s <= f <= e for s, e, *_ in self.segments)
            self._say("That frame is cut - nothing to mark there." if in_gap
                      else "Already a boundary there.", "red_soft")
            return
        # Bypass _normalize_segments here on purpose: the fresh pieces share the parent
        # gain, and normalize merges adjacent equal-gain segments back into one - that
        # would erase the marker the instant it's dropped. Once the user sets a
        # different gain the pieces stop matching and stay split; if a piece is left at
        # 0 dB the next normalize pass (any later cut/trim/restore, or a reload) merges
        # it away on its own. See _insert_marker.
        self.segments = new
        self._sel_seg = next((i for i, seg in enumerate(new) if seg[1] == f), None)
        self._after_segments_changed()
        self._undo_commit()   # a discrete keyboard/button edit is its own undo step
        self._focus_seg_gain()
        self._say(f"Marked F:{f} - set the volume for the segment on either side.", "green")

    def _focus_seg_gain(self):
        """Focus the segment-gain spinbox with its text selected, so the user can type
        a dB value immediately after marking or selecting a region. Expands the AUDIO
        section first if it's collapsed (it starts collapsed on first run, per
        _COLLAPSED_DEFAULT) - focus_set on an unmapped widget is silently ignored."""
        sec = self._sections.get("audio")
        if sec is not None and not sec["open"]:
            sec["set"](True)
        self._seg_gain_spin.focus_set()
        self._seg_gain_spin.selection_range(0, tk.END)

    def _restore_gap(self, a, b):
        """Restore a removed gap [a,b] (the right-click action). 5.5.2b: the LEFT
        neighboring segment extends through the restored range (the right one when
        the gap has no left neighbor), so the restored frames inherit a real gain;
        normalize then merges equal-gain neighbors back to one segment (today's
        behavior) while a differing-gain right neighbor stays its own region."""
        segs = [list(seg) for seg in self.segments]
        left = next((seg for seg in reversed(segs) if seg[1] < a), None)
        if left is not None:
            left[1] = int(b)
        else:
            right = next((seg for seg in segs if seg[0] > b), None)
            if right is not None:
                right[0] = int(a)
        self.segments = self._normalize_segments(segs, self.total_frames)
        self._after_segments_changed()
        self._say(f"Restored F:{a}-{b}.", "green")

    def _gap_at_x(self, x):
        """The removed gap (a, b) under canvas x on the timeline, or None. Gaps live
        BETWEEN consecutive kept segments."""
        frame = self._tl_frame_of(x)
        for seg0, seg1 in zip(self.segments, self.segments[1:]):
            if seg0[1] < frame < seg1[0]:
                return (seg0[1] + 1, seg1[0] - 1)
        return None

    def _marker_at_x(self, x):
        """The Mark Audio boundary (source frame) nearest canvas x, within TL_HIT_PX, or
        None (5.5.2f). A boundary is where two kept segments TOUCH with no gap between
        them - a real cut gap is a different, wider case that _gap_at_x owns."""
        best, best_d = None, TL_HIT_PX + 1
        for seg0, seg1 in zip(self.segments, self.segments[1:]):
            if seg1[0] != seg0[1] + 1:
                continue                     # a cut gap, not a marker boundary
            d = abs(self._tl_x_of(seg0[1]) - x)
            if d < best_d:
                best, best_d = seg0[1], d
        return best

    def _remove_marker(self, f):
        """Right-click a Mark Audio boundary to delete it (5.5.2f) - merges the two
        segments it separates back into one, keeping the left segment's gain."""
        new = self._delete_marker(self.segments, f)
        if new == [list(s) for s in self.segments]:
            return
        self.segments = new
        self._sel_seg = None
        self._after_segments_changed()
        self._say(f"Marker at F:{f} removed.", "green")

    # ── Audio waveform (2.6: visual navigation only; peaks computed off-thread) ─────
    def _load_waveform(self):
        """Kick the off-thread peak + PCM extraction for the current clip. A token
        guards against a stale thread (older clip) delivering after a newer load."""
        self._wave_peaks = None
        self._pcm = None          # previous clip's audio must never play under this one
        self._pcm_rate = 0
        self._wave_state = "loading"
        token = self._wave_token = object()
        threading.Thread(target=self._wave_worker, args=(token, self._path),
                         daemon=True).start()

    def _wave_worker(self, token, src):
        """Daemon thread: extract audio → temp WAV read ONCE → per-bucket peaks AND
        the full int16 PCM (2.8.6 keeps it for audible playback). NO tkinter here;
        the results marshal back together."""
        peaks = pcm = None
        rate = 0
        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="sc_wave_")
        os.close(fd)
        try:
            if video_utils.extract_audio(src, tmp) is not None:
                decoded = self._read_wav(tmp)
                if decoded is not None:
                    pcm, rate = decoded
                    peaks = self._peaks_from_pcm(pcm)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self._marshal(self._wave_ready, token, peaks, pcm, rate)

    @staticmethod
    def _read_wav(wav_path):
        """16-bit PCM WAV → (int16 array (n, ch), rate), or None (no/empty/corrupt
        audio). >2 channels downmix to stereo here (even→L, odd→R means) - output
        devices choke on >2ch; mono stays mono."""
        try:
            with wave.open(wav_path, "rb") as wf:
                n = wf.getnframes()
                ch = wf.getnchannels()
                rate = wf.getframerate()
                if n == 0 or ch == 0:
                    return None
                raw = wf.readframes(n)
        except (wave.Error, OSError, EOFError):
            return None
        pcm = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch)
        if ch > 2:
            pcm = np.stack([pcm[:, 0::2].mean(axis=1), pcm[:, 1::2].mean(axis=1)],
                           axis=1).astype(np.int16)
        return pcm, rate

    @staticmethod
    def _peaks_from_pcm(pcm, buckets=2000):
        """Per-bucket |peak| amplitudes (float32 0..1) - same math as the 2.6
        WAV-path version (peak across channels, bucket max), now fed from the kept
        PCM. Fixed bucket count - the draw maps buckets to the timeline width."""
        samples = np.abs(pcm.astype(np.int32)).max(axis=1)
        buckets = min(buckets, len(samples))
        usable = (len(samples) // buckets) * buckets
        if usable == 0:
            return None
        peaks = samples[:usable].reshape(buckets, -1).max(axis=1)
        return (peaks / 32767.0).astype(np.float32)

    def _wave_ready(self, token, peaks, pcm, rate):
        if token is not self._wave_token:
            return   # a newer clip superseded this extraction
        self._wave_peaks = peaks
        self._pcm = pcm
        self._pcm_rate = int(rate or 0)
        self._wave_state = "ready" if peaks is not None else "noaudio"
        self._redraw_timeline()

    # ── Render (2.6: daemon thread, OWN capture, single H.264 encode + audio mux) ───
    def _start_render(self):
        """UI thread: validate, SNAPSHOT every parameter into a plain dict (zero
        widget references cross the thread boundary), spawn the worker."""
        if self._rendering:
            self._say("A render is already running.", "yellow")
            return
        if self._path is None or self.game_box is None:
            self._set_lamp(False, "Load a clip first - nothing to render.", error=True)
            return
        if video_utils.find_ffmpeg() is None:
            self._set_lamp(False, "ffmpeg not found - install it on PATH or drop "
                                  "ffmpeg.exe into assets/.", error=True)
            return
        if self.out_frame <= self.in_frame:
            self._set_lamp(False, "Out must be after In - fix the trim points.",
                           error=True)
            return
        try:
            gain_db = float(self.gain_var.get())
        except (tk.TclError, ValueError):
            gain_db = 0.0
        try:
            sharp_str = float(self.sharpen_strength.get())
        except (tk.TclError, ValueError):
            sharp_str = 1.0
        sharp_str = max(0.0, min(3.0, sharp_str))   # clamp to the spinbox range
        stem = os.path.splitext(os.path.basename(self._path))[0]
        out_path = self.app.paths.unique_path(os.path.join(
            self.app.paths.get_path("Shorts"),
            f"{stem}_vertical_{time.strftime('%Y%m%d_%H%M%S')}.mp4"))
        params = {
            "src": self._path,
            "out": out_path,
            "fps": float(self.fps),
            "in_f": int(self.in_frame),
            "out_f": int(self.out_frame),
            # 2.10.2b: the kept source-frame ranges (outer bounds == in_f/out_f). One
            # segment → today's render; the feed paths skip removed frames, audio follows.
            "segments": [list(s) for s in self.segments],
            "scale_x": float(self.scale_x),
            "scale_y": float(self.scale_y),
            "game_box": list(self.game_box),
            "cam_box": list(self.cam_box),
            "split": int(self.split_var.get()),
            "single_panel": bool(self.single_panel.get()),
            "melt": bool(self.melt_on.get()),
            "melt_px": int(self.melt_px.get()),
            "border": bool(self.border_on.get()),
            "border_color": _hex_to_bgr(self._border_hex, (15, 15, 15)),
            "border_px": int(self.border_px.get()),
            "watermark": self._snapshot_watermark(),
            "branding": self._snapshot_branding(),   # 5.5.1g independent overlays
            "hl_on": bool(self.hl_on.get()),
            "highlights": copy.deepcopy(self.highlights),
            "keyframes": copy.deepcopy(self.keyframes),
            "gain_db": gain_db,
            # 3.1: render-only sharpen (unsharp mask applied per OUTPUT frame inside the one
            # encode, after composite_frame). Off / strength 0 → guaranteed no-op.
            "sharpen": bool(self.sharpen_on.get()),
            "sharpen_strength": sharp_str,
            # Read fresh per render (2.8.4): a Home toggle affects the NEXT render.
            "mode": ("performance"
                     if self.app.config.get("performance_mode") == "performance"
                     else "eco"),
        }
        # Codec decision (2.8.5): auto = hw only in Performance mode; on = both
        # modes (NVENC runs on a dedicated ASIC - least CPU while gaming); off =
        # always libx264. detect_hw_encoder() probes lazily, once per process.
        cfg_hw = self.app.config.get("hw_encode", "auto")
        want_hw = cfg_hw == "on" or (cfg_hw == "auto"
                                     and params["mode"] == "performance")
        params["codec"] = ((video_utils.detect_hw_encoder() if want_hw else None)
                           or "libx264")
        self._rendering = True
        self._render_cancel = threading.Event()
        self._render_btn.configure(state="disabled")
        # A collapsed RENDER OPTIONS section would hide the progress bar; expand it.
        self._sections["render_options"]["set"](True)
        self._render_prog.configure(value=0)
        self._render_prog.pack(fill="x", pady=(6, 0))
        self._render_cancel_btn.pack(anchor="w", pady=(6, 0))
        self._say("Rendering… 0%", "yellow")
        threading.Thread(target=self._render_worker, args=(params,),
                         name="sc-render-feed", daemon=True).start()

    def _render_worker(self, params):
        """Daemon thread (orchestrator): own capture → ONE encode attempt
        (`_render_encode` - serial or parallel per the snapshotted mode) → report.
        Performance mode also boosts process priority for the duration; the finally
        restores NORMAL on EVERY exit (success, cancel, error). ZERO tkinter except
        the _marshal'd calls."""
        boosted = params["mode"] == "performance"
        cap = cv2.VideoCapture(params["src"])
        try:
            if not cap.isOpened():
                self._marshal(self._render_done, False,
                              "Couldn't open the source clip.")
                return
            if boosted:
                _set_process_priority(True)
            cancel = self._render_cancel
            ok, info, retryable = self._render_encode(cap, params, cancel)
            if (not ok and retryable and params["codec"] != "libx264"
                    and not cancel.is_set()):
                # One-shot fallback (2.8.5): a hw-encode failure must never cost
                # the render. The encode attempt is pure/repeatable - _render_encode
                # re-seeks the capture and rebuilds the pipeline itself.
                self._marshal(self._say,
                              "Hardware encode failed - retrying with libx264...",
                              "yellow")
                ok, info, _ = self._render_encode(
                    cap, dict(params, codec="libx264"), cancel)
            self._marshal(self._render_done, ok, info)
        except Exception as ex:  # noqa: BLE001 - the worker must never die silently
            writer = self._render_writer
            if writer is not None:
                writer.abort()
            self._marshal(self._render_done, False, f"Render error: {ex}")
        finally:
            if boosted:
                _set_process_priority(False)
            self._render_writer = None
            cap.release()

    def _render_encode(self, cap, params, cancel):
        """ONE encode attempt: open the writer, feed every frame In..Out (serial or
        parallel), close. Returns (ok, info, retryable) - info is the output path or
        the error text; retryable=True only for WRITER failures (the encoder died /
        close() not ok), the cases where the 2.8.5 libx264 fallback can help.
        Cancels and composite errors are deterministic - never retried. Pure and
        repeatable: re-seeks the capture and rebuilds pipeline state itself."""
        in_f, out_f, fps = params["in_f"], params["out_f"], params["fps"]
        segments = params["segments"]
        # 2.10.2b: ONE segment → the exact 2.6/2.8.5 single -ss/-to audio path (byte-for-
        # byte). MULTIPLE → the atrim+concat filtergraph (cut-aware audio; apad fixes the
        # 2.8.5 final-frame drop). The feed paths skip removed frames either way.
        # 5.5.2b: segment gains travel as the tuples' third element (multi) or fold
        # into the one -af volume (single) - all-zero offsets leave both paths
        # byte-identical to before.
        gain_db = params["gain_db"]
        if len(segments) > 1:
            writer_kw = {"segments": [
                (seg[0] / fps, (seg[1] + 1) / fps,
                 float(seg[2]) if len(seg) > 2 else 0.0) for seg in segments]}
        else:
            writer_kw = {"start": in_f / fps, "end": (out_f + 1) / fps}
            seg0 = segments[0]
            if len(seg0) > 2 and seg0[2]:
                gain_db = gain_db + float(seg0[2])
        try:
            writer = video_utils.H264PipeWriter(
                params["out"], OUT_W, OUT_H, fps,
                audio_src=params["src"], gain_db=gain_db,
                codec=params.get("codec", "libx264"), **writer_kw)
        except RuntimeError as ex:
            return False, str(ex), False   # no ffmpeg at all - no codec can help
        self._render_writer = writer
        cap.set(cv2.CAP_PROP_POS_FRAMES, in_f)
        static = {"game_box": params["game_box"], "cam_box": params["cam_box"],
                  "split_ratio": params["split"],
                  "highlights": params["highlights"]}
        if params["mode"] == "performance":
            fed, err = self._feed_parallel(cap, writer, params, static, cancel)
        else:
            fed, err = self._feed_serial(cap, writer, params, static, cancel)
        if not fed:
            writer.abort()   # kill-first + partial removed (BUILDER_NOTES semantics)
            return False, err, False
        ok, err = writer.close()
        if ok:
            return True, params["out"], False
        try:
            if os.path.exists(params["out"]):
                os.remove(params["out"])   # never leave a broken partial
        except OSError:
            pass
        return False, err or "ffmpeg failed with no error output.", True

    @staticmethod
    def _compose_render_frame(frame, idx, params, static):
        """The per-frame composite both feed paths share (so they cannot drift):
        pure interpolate_layout on the snapshotted keyframes (static layout when
        none), then pure composite_frame. Safe from any thread."""
        lay = interpolate_layout(params["keyframes"], idx) or static
        hls = lay.get("highlights") or []
        out = composite_frame(
            frame,
            scale_x=params["scale_x"], scale_y=params["scale_y"],
            game_box=lay["game_box"], cam_box=lay["cam_box"],
            split_ratio=lay["split_ratio"],
            melt=params["melt"], melt_px=params["melt_px"],
            solid_border=params["border"],
            border_color=params["border_color"],
            border_px=params["border_px"],
            watermark=params["watermark"],
            branding=params.get("branding"),   # 5.5.1g independent overlays
            # Model = the snapshotted live highlights (visible ranges ride in the
            # dicts); geometry = the interpolated list. Zero self/widget reads.
            highlights=(filter_visible(hls, params["highlights"], idx)
                        if params["hl_on"] else ()),
            single_panel=params["single_panel"],
        )
        # 3.1: render-only sharpen, applied AFTER the composite so both feed paths sharpen
        # identically (the 2.9.1 bit-identical invariant). video_utils.sharpen no-ops at
        # strength <= 0, so off / 0 returns the composite unchanged.
        if params.get("sharpen"):
            out = video_utils.sharpen(out, params.get("sharpen_strength", 1.0))
        return out

    @staticmethod
    def _render_walk(segments):
        """(in_f, out_f, kept_set, total) for a render feed: the sequential decode span
        and the SOURCE indices to keep. Shared by both feed paths (2.10.2b) so the
        kept/skip/sequence logic can't drift - like _compose_render_frame for the composite.
        One segment → every frame in in_f..out_f is kept (today's behavior)."""
        in_f, out_f = segments[0][0], segments[-1][1]
        return (in_f, out_f, set(EditorModule._kept_frames(segments)),
                EditorModule._kept_total(segments))

    def _feed_serial(self, cap, writer, params, static, cancel):
        """Eco path: the proven 2.6 serial loop (decode → composite → write, one
        frame at a time). 2.10.2b: decode walks in_f..out_f sequentially but only
        composites+writes KEPT frames; removed frames are read (to advance the capture
        - never cap.set per gap) and discarded. Returns (fed, err): False = abort the
        writer with `err`; True = all frames fed, caller close()s."""
        in_f, out_f, kept, total = self._render_walk(params["segments"])
        n = 0   # OUTPUT sequence number - advances only for kept frames
        for idx in range(in_f, out_f + 1):
            if cancel.is_set():
                return False, "Render cancelled."
            ok, frame = cap.read()
            if not ok or frame is None:
                break   # container shorter than reported - keep what we encoded
            if idx not in kept:
                continue   # removed frame: decoded to advance, then dropped
            out_frame = self._compose_render_frame(frame, idx, params, static)
            try:
                writer.write(out_frame)
            except OSError:
                break   # ffmpeg died mid-pipe - close() surfaces stderr
            if n % 10 == 0:
                self._marshal(self._render_progress, n, total)
            n += 1
        return True, None

    def _feed_parallel(self, cap, writer, params, static, cancel):
        """Performance path (2.8.4): decode (this thread) → bounded compositor pool
        → strictly ordered writes on a writer thread. Same return contract as
        _feed_serial.

        Memory bound: in-flight frames ≤ in_q(4) + pool(≤4) + out_q(4) + pending
        (≤ pool+4) ≈ 20 frames ≈ ~125MB transient at 1080p-in / 1080×1920-out.

        Cancel/error (non-negotiable): `aborting()` = the cancel Event OR the first
        pool error OR a dead ffmpeg pipe - EVERY stage loop checks it, and every
        queue put/get uses timeout=0.1 with a re-check, so a full/empty queue can
        never deadlock a cancel. Decode end pushes one sentinel (None) per pool
        thread; each pool thread forwards its sentinel and exits; the writer exits
        on the full sentinel count (per-thread queue FIFO ⇒ every frame precedes
        its thread's sentinel ⇒ nothing is lost). A dead pipe sets `pipe_dead` and
        returns fed=True so close() surfaces ffmpeg's stderr - exactly the serial
        path's break semantics.

        2.10.2b: the decode loop assigns the contiguous OUTPUT index `n` only to KEPT
        frames and pushes (n, src_idx, frame); the pool composites with src_idx; the writer
        still orders by `n`. Removed frames are read-and-discarded (never cap.set per gap).
        Single segment → every frame kept → identical to today."""
        in_f, out_f, kept, total = self._render_walk(params["segments"])
        n_pool = min(4, max(2, (os.cpu_count() or 4) // 2))
        in_q = queue.Queue(maxsize=4)
        out_q = queue.Queue(maxsize=4)
        flags = {"err": None, "pipe_dead": False}   # first error wins

        def aborting():
            return (cancel.is_set() or flags["err"] is not None
                    or flags["pipe_dead"])

        def fail(msg):
            if flags["err"] is None:
                flags["err"] = msg

        def put_checked(q, item):
            """Bounded put that re-checks abort. False = aborted, drop the item."""
            while not aborting():
                try:
                    q.put(item, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        def pool_run():
            while True:
                if aborting():
                    return
                try:
                    item = in_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:               # decode finished: forward + exit
                    put_checked(out_q, None)
                    return
                n, src_idx, frame = item
                try:
                    out = self._compose_render_frame(frame, src_idx, params, static)
                except Exception as ex:  # noqa: BLE001 - surface, never die silent
                    fail(f"Composite failed at frame {src_idx}: {ex}")
                    return
                if not put_checked(out_q, (n, out)):
                    return

        def writer_run():
            pending = {}
            next_n = 0
            finished = 0
            while finished < n_pool:
                if aborting():
                    return
                try:
                    item = out_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    finished += 1
                    continue
                n, out = item
                pending[n] = out
                while next_n in pending:       # strict frame order, no gaps
                    try:
                        writer.write(pending.pop(next_n))
                    except OSError:
                        flags["pipe_dead"] = True   # close() surfaces stderr
                        return
                    if next_n % 10 == 0:       # written count = truthful progress
                        self._marshal(self._render_progress, next_n, total)
                    next_n += 1

        pool = [threading.Thread(target=pool_run, name=f"sc-render-pool-{i}",
                                 daemon=True) for i in range(n_pool)]
        wthread = threading.Thread(target=writer_run, name="sc-render-write",
                                   daemon=True)
        for t in pool:
            t.start()
        wthread.start()

        n = 0                                  # OUTPUT index - advances only for kept frames
        for idx in range(in_f, out_f + 1):     # decode, strictly sequential
            if aborting():
                break
            ok, frame = cap.read()
            if not ok or frame is None:
                break   # container shorter than reported - keep what we encoded
            if idx not in kept:
                continue   # removed frame: read to advance the capture, then dropped
            if not put_checked(in_q, (n, idx, frame)):
                break
            n += 1
        for _ in range(n_pool):                # one shutdown sentinel per thread
            if not put_checked(in_q, None):
                break
        for t in pool:
            t.join(timeout=5)
        wthread.join(timeout=5)

        if cancel.is_set():
            return False, "Render cancelled."
        if flags["err"] is not None:
            return False, flags["err"]
        return True, None   # incl. pipe_dead: close() reports ffmpeg's error

    def _marshal(self, fn, *args):
        """Queue a worker result for the UI thread. Workers never touch tkinter - not
        even after(): a cross-thread after() raises RuntimeError unless the main
        thread is inside mainloop() (it is in the real app, but never in an
        update()-driven test harness, and not during teardown). The 50ms _ui_pump
        drains the queue on the UI thread under both."""
        if not self._closing:
            self._ui_queue.put((fn, args))

    def _ui_pump(self):
        """UI-thread poller for worker results. Reschedules FIRST so an exception in
        a drained callback (reported by Tk) can't kill the pump. An empty poll is
        sub-microsecond - always-on beats start/stop bookkeeping."""
        self._ui_pump_job = self.after(50, self._ui_pump)
        while not self._closing:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            fn(*args)

    def _render_progress(self, n, total):
        if self._closing or not self._rendering:
            return
        pct = int(n / max(1, total) * 100)
        self._render_prog.configure(value=pct)
        self._say(f"Rendering… {pct}%  ({n}/{total} frames)", "yellow")

    def _render_done(self, ok, info):
        if self._closing:
            return
        self._rendering = False
        self._render_cancel = None
        self._render_btn.configure(state="normal")
        self._render_prog.pack_forget()
        self._render_cancel_btn.pack_forget()
        if ok:
            self.bus.publish("edited_clip", info)
            self._set_render_state("rendered")
            self._set_lamp(True,
                           f"Rendered → {os.path.basename(info)} (in data/Shorts)")
        else:
            self._set_lamp(False, str(info), error=True)

    def _cancel_render(self):
        if self._render_cancel is not None:
            self._render_cancel.set()
            self._say("Cancelling…", "yellow")

    # ── Source viewer rendering + crop boxes ─────────────────────────────────────────
    def _render_source_frame(self):
        frame = self._read_frame(self.frame_idx)
        if frame is None:
            return
        c = self._source_canvas
        cw, ch = c.winfo_width(), c.winfo_height()
        if cw < 20 or ch < 20:
            # Not laid out yet (load arrived before the tab was ever mapped) - retry once
            # things have a size; the <Configure> debounce also covers this.
            if self._render_retry is None:
                self._render_retry = self.after(60, self._render_retry_cb)
            return

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        img.thumbnail((cw, ch))   # fit, preserving aspect
        self._source_disp = img
        iw, ih = img.size

        # Display size changed (window resize): rescale the boxes so they keep covering
        # the same source content - the old app had a fixed canvas, ours doesn't.
        disp_changed = (iw, ih) != (self._disp_w, self._disp_h)
        if disp_changed and self.game_box is not None and self._disp_w > 0:
            rx, ry = iw / self._disp_w, ih / self._disp_h
            # Keyframes live in the same canvas space as the live boxes - rescale them
            # together or every keyframe goes stale after a window resize. Highlight
            # src boxes too (live + keyframed); dest boxes are OUTPUT space, untouched.
            kf_boxes = [b for kf in self.keyframes
                        for b in (kf["game_box"], kf["cam_box"])]
            hl_boxes = [h["src_box"] for h in self.highlights]
            hl_boxes += [h["src_box"] for kf in self.keyframes
                         for h in (kf.get("highlights") or [])]
            for box in [self.game_box, self.cam_box] + kf_boxes + hl_boxes:
                box[0] *= rx
                box[1] *= ry
                box[2] *= rx
                box[3] *= ry
        self._disp_w, self._disp_h = iw, ih
        self.off_x = (cw - iw) // 2   # the frame is centered in the viewer
        self.off_y = (ch - ih) // 2

        # Canvas→source mapping for the compositor and every later spec: image-local
        # canvas coords × scale = source pixels. Recomputed on every (re)render.
        self.scale_x = self.orig_w / iw
        self.scale_y = self.orig_h / ih

        self._source_photo = ImageTk.PhotoImage(img)
        c.delete("all")
        c.create_image(self.off_x, self.off_y, image=self._source_photo, anchor="nw")

        # Guidance-5 gotcha: after clearing the canvas the boxes MUST be redrawn.
        if self.game_box is None or self._pending_norm is not None:
            self._materialize_boxes()
        elif disp_changed:
            # With keyframes the track owns the layout: re-apply the (just rescaled)
            # keyframe geometry rather than recalculate_box_sizes, whose aspect
            # re-lock drifts the live boxes away from the keyframe under the
            # playhead. Static layout (no keyframes) keeps the re-lock.
            if not self._apply_interpolated(self.frame_idx):
                self.recalculate_box_sizes()
        else:
            self._draw_boxes()
        self._schedule_preview_update()

    def _render_retry_cb(self):
        self._render_retry = None
        if self._path is not None:
            self._render_source_frame()

    @staticmethod
    def _draw_corner_ticks(c, x0, y0, x1, y1, color, tags, width=2):
        """Corner resize affordance (2.8.8): an "L" of two ~9px lines along the box
        edges at each corner, in the box's accent color. Pure viewer overlay -
        derived from the rect each redraw, hit-testing (`_corner_hit`) unchanged,
        zero per-frame playback/render cost (boxes don't redraw during ticks)."""
        t = 9
        for cx, sx in ((x0, 1), (x1, -1)):
            for cy, sy in ((y0, 1), (y1, -1)):
                c.create_line(cx, cy, cx + sx * t, cy, fill=color, width=width,
                              tags=tags)
                c.create_line(cx, cy, cx, cy + sy * t, fill=color, width=width,
                              tags=tags)

    def _draw_boxes(self):
        """(Re)draw both crop rectangles at their current coords (image-local + offset)."""
        c = self._source_canvas
        c.delete("boxes")
        if self.game_box is None:
            return
        rects = [(self.game_box, PALETTE["green"], "game_rect")]
        if not self.single_panel.get():            # 2.10.1: no cam panel in single mode
            rects.append((self.cam_box, PALETTE["blue"], "cam_rect"))
        for box, color, tag in rects:
            x0, y0 = self.off_x + box[0], self.off_y + box[1]
            x1, y1 = x0 + box[2], y0 + box[3]
            c.create_rectangle(x0, y0, x1, y1,
                               outline=color, width=3, tags=("boxes", tag))
            self._draw_corner_ticks(c, x0, y0, x1, y1, color, ("boxes", tag))
        # Highlight src crops (2.5): orange; the selected one solid + thick, the rest
        # dashed + thin. Same "boxes" tag so every clear/redraw cycle brings them back.
        if self.hl_on.get():
            for i, hl in enumerate(self.highlights):
                b = hl["src_box"]
                sel = i == self.sel_hl
                x0, y0 = self.off_x + b[0], self.off_y + b[1]
                x1, y1 = x0 + b[2], y0 + b[3]
                c.create_rectangle(x0, y0, x1, y1,
                                   outline=PALETTE["orange"], width=3 if sel else 1,
                                   dash=() if sel else (4, 3),
                                   tags=("boxes", f"hl_src_{i}"))
                self._draw_corner_ticks(c, x0, y0, x1, y1, PALETTE["orange"],
                                        ("boxes", f"hl_src_{i}"),
                                        width=2 if sel else 1)

    def _materialize_boxes(self):
        """Create the boxes once the display size is known: from a pending (normalized)
        layout if one is waiting, else at the built-in default positions. Pending
        highlights denormalize here too (src needs the display size; dest is OUTPUT
        space and denormalizes against the fixed 1080×1920)."""
        if self._disp_w <= 0:
            return  # the first successful render calls back in
        if self._pending_hl_norm is not None:
            pend_hl, self._pending_hl_norm = self._pending_hl_norm, None
            dw, dh = self._disp_w, self._disp_h
            self.highlights = [
                {**h,
                 "src_box": self._denorm_box(h["src_box"], dw, dh),
                 "dest_box": self._denorm_box(h["dest_box"], OUT_W, OUT_H),
                 # visible frames are absolute - clamp to THIS file's length
                 "visible": self._clamp_visible(h.get("visible"))}
                for h in pend_hl if h.get("src_box") and h.get("dest_box")]
            self._clamp_all_dests()   # older layouts may predate the seam confinement
            self.sel_hl = 0 if self.highlights else None
            self._refresh_hl_rows()
        pend, self._pending_norm = self._pending_norm, None
        if pend:
            dw, dh = self._disp_w, self._disp_h
            g, cb = pend
            self.game_box = self._denorm_box(g, dw, dh)
            self.cam_box = self._denorm_box(cb, dw, dh)
            self.recalculate_box_sizes(reset_positions=False)
        else:
            self.recalculate_box_sizes(reset_positions=self.game_box is None)
        # Persisted keyframes (2.8.2) re-materialize LAST: _apply_interpolated below
        # overwrites the live boxes with the choreography under the playhead, so the
        # static layout above must already be in place when there are no keyframes.
        if self._pending_kf_norm is not None:
            pend_kf, self._pending_kf_norm = self._pending_kf_norm, None
            dw, dh = self._disp_w, self._disp_h
            kfs = []
            for kf in pend_kf:
                if not (kf.get("game_box") and kf.get("cam_box")):
                    continue
                kfs.append({
                    "frame": int(kf.get("frame", 0)),
                    "type": kf.get("type", "fluid"),
                    "game_box": self._denorm_box(kf["game_box"], dw, dh),
                    "cam_box": self._denorm_box(kf["cam_box"], dw, dh),
                    "split_ratio": int(kf.get("split_ratio", 50)),
                    # {**h, ...} keeps unknown keys (2.8.7 extends highlight dicts -
                    # the block must pass them through unharmed, spec rule).
                    "highlights": [
                        {**h,
                         "src_box": self._denorm_box(h["src_box"], dw, dh),
                         "dest_box": self._denorm_box(h["dest_box"], OUT_W, OUT_H)}
                        for h in (kf.get("highlights") or [])
                        if h.get("src_box") and h.get("dest_box")],
                })
            kfs.sort(key=lambda k: k["frame"])
            self.keyframes = kfs
            if kfs:
                self._apply_interpolated(self.frame_idx)
                self._redraw_timeline()

    def _panel_aspect(self, which):
        """Canvas-space height-per-width that locks a box to its output panel's aspect
        (a free aspect would distort the crop when resized into the panel). In
        single-panel mode (2.10.1) the one content box is full-frame 9:16, independent
        of the split."""
        if self.single_panel.get():
            panel_h = OUT_H
        else:
            split = int(self.split_var.get())
            game_h = int(OUT_H * split / 100)
            panel_h = game_h if which == "game" else OUT_H - game_h
        return (panel_h / self.scale_y) / (OUT_W / self.scale_x)

    def _default_box_size(self, which):
        """The old-app default size for a box: panel-aspect width scaled so both boxes
        together stand ~75% of the displayed frame tall."""
        w_c = OUT_W / self.scale_x
        total_h = w_c * (self._panel_aspect("game") + self._panel_aspect("cam"))
        w = w_c * min(1.0, (self._disp_h * 0.75) / max(1.0, total_h))
        return w, w * self._panel_aspect(which)

    def recalculate_box_sizes(self, reset_positions=False):
        """Re-lock both boxes to their output panels' aspect at the current split. The
        user sizes boxes freely by corner-drag (the width is theirs); a split change
        keeps each box's width and recomputes its height. reset_positions=True (or no
        boxes yet) re-derives the default ~75%-fit sizes and corner positions."""
        if self._disp_w <= 0:
            return
        if self.game_box is None:
            reset_positions = True

        if reset_positions:
            g_w, g_h = self._default_box_size("game")
            c_w, c_h = self._default_box_size("cam")
            self.game_box = [20.0, 20.0, g_w, g_h]
            self.cam_box = [self._disp_w - c_w - 20.0, self._disp_h - c_h - 20.0, c_w, c_h]
        else:
            # Single-panel: re-lock ONLY the content (game) box; leave cam_box untouched
            # so it survives a round-trip back to split (2.10.1).
            relock = ((("game", self.game_box),) if self.single_panel.get()
                      else (("game", self.game_box), ("cam", self.cam_box)))
            for which, box in relock:
                aspect = self._panel_aspect(which)
                box[3] = box[2] * aspect
                if box[3] > self._disp_h:      # keep the aspect: shrink width to fit
                    box[2] = self._disp_h / max(aspect, 1e-6)
                    box[3] = self._disp_h
                if box[2] > self._disp_w:
                    box[2] = self._disp_w
                    box[3] = box[2] * aspect

        self._sanitize_box(self.game_box)
        self._sanitize_box(self.cam_box)
        self._draw_boxes()
        self._schedule_preview_update()

    def _sanitize_box(self, box):
        """Clamp a box inside the displayed image (image-local canvas space)."""
        box[2] = min(box[2], self._disp_w)
        box[3] = min(box[3], self._disp_h)
        box[0] = max(0.0, min(box[0], self._disp_w - box[2]))
        box[1] = max(0.0, min(box[1], self._disp_h - box[3]))

    def _reset_box(self, which):
        """Reset one box to its default size AND position (sizes are user-editable now,
        so a reset restores both)."""
        if self.game_box is None:
            return
        w, h = self._default_box_size(which)
        if which == "game":
            self.game_box = [20.0, 20.0, w, h]
        else:
            self.cam_box = [self._disp_w - w - 20.0, self._disp_h - h - 20.0, w, h]
        self._sanitize_box(self.game_box if which == "game" else self.cam_box)
        self._draw_boxes()
        self._schedule_preview_update()
        self._mark_dirty()

    # ── Source-canvas mouse interaction (box move + corner resize) ───────────────────
    @staticmethod
    def _corner_hit(box, x, y):
        """Which corner of `box` is within CORNER_TOL of image-local (x, y), if any."""
        bx, by, bw, bh = box
        for cx, cy, name in ((bx, by, "nw"), (bx + bw, by, "ne"),
                             (bx, by + bh, "sw"), (bx + bw, by + bh, "se")):
            if abs(x - cx) <= CORNER_TOL and abs(y - cy) <= CORNER_TOL:
                return name
        return None

    @staticmethod
    def _side_hit(box, x, y):
        """Which side band ("n"/"s"/"e"/"w") of `box` is within CORNER_TOL of (x, y),
        the corner zones excluded (corners keep priority; callers test them first)."""
        bx, by, bw, bh = box
        in_x = bx + CORNER_TOL < x < bx + bw - CORNER_TOL
        in_y = by + CORNER_TOL < y < by + bh - CORNER_TOL
        if in_y:
            if abs(x - bx) <= CORNER_TOL:
                return "w"
            if abs(x - (bx + bw)) <= CORNER_TOL:
                return "e"
        if in_x:
            if abs(y - by) <= CORNER_TOL:
                return "n"
            if abs(y - (by + bh)) <= CORNER_TOL:
                return "s"
        return None

    def _hit_test(self, x, y):
        """(which_box, corner|side|None) at image-local (x, y). Corners beat sides
        beat interiors (5.5.1d); cam beats game on overlap (old-app priority)."""
        for which, box in (("cam", self.cam_box), ("game", self.game_box)):
            corner = self._corner_hit(box, x, y)
            if corner:
                return which, corner
        for which, box in (("cam", self.cam_box), ("game", self.game_box)):
            side = self._side_hit(box, x, y)
            if side:
                return which, side
        for which, box in (("cam", self.cam_box), ("game", self.game_box)):
            if box[0] <= x <= box[0] + box[2] and box[1] <= y <= box[1] + box[3]:
                return which, None
        return None, None

    def _on_source_press(self, event):
        self._source_canvas.focus_set()
        if self.game_box is None:
            return
        x, y = event.x - self.off_x, event.y - self.off_y   # → image-local coords
        # Highlights win over game/cam (smaller, drawn on top); pressing a
        # non-selected one selects it and starts the drag in the same gesture.
        i, corner = self._hl_hit(x, y)
        if i is not None:
            self.select_highlight(i)
            self._active_box = "hl"
            self._resize_corner = corner
        else:
            self._active_box, self._resize_corner = self._hit_test(x, y)
        self._drag_x, self._drag_y = x, y

    def _on_source_drag(self, event):
        if not self._active_box:
            return
        x, y = event.x - self.off_x, event.y - self.off_y
        if self._active_box == "hl":
            if self.sel_hl is None or self.sel_hl >= len(self.highlights):
                return
            hl = self.highlights[self.sel_hl]
            box = hl["src_box"]
            if self._resize_corner in _SIDE_CURSORS:
                self._resize_src_hl_side(box, self._resize_corner, x, y)
                self._sync_dest_aspect(hl)
            elif self._resize_corner:
                self._resize_src_hl(box, self._resize_corner, x, y)
                self._sync_dest_aspect(hl)
            else:
                dx, dy = x - self._drag_x, y - self._drag_y
                box[0] = max(0.0, min(box[0] + dx, self._disp_w - box[2]))
                box[1] = max(0.0, min(box[1] + dy, self._disp_h - box[3]))
        else:
            box = self.game_box if self._active_box == "game" else self.cam_box
            if self._resize_corner in _SIDE_CURSORS:
                self._resize_box_side(box, self._active_box, self._resize_corner, x, y)
            elif self._resize_corner:
                self._resize_box_to(box, self._active_box, self._resize_corner, x, y)
            else:
                dx, dy = x - self._drag_x, y - self._drag_y
                box[0] = max(0.0, min(box[0] + dx, self._disp_w - box[2]))
                box[1] = max(0.0, min(box[1] + dy, self._disp_h - box[3]))
        self._drag_x, self._drag_y = x, y
        self._schedule_box_draw()
        self._schedule_preview_update(30, fast=True)   # follow the mouse; settle after
        self._mark_dirty()

    def _resize_box_side(self, box, which, side, x, y):
        """Side-drag resize (5.5.1d): the dragged edge follows the cursor, the
        opposite edge stays anchored, the panel aspect holds, and the cross axis
        stays centered (so an E/W drag grows the box evenly up and down)."""
        aspect = max(self._panel_aspect(which), 1e-6)
        if side in ("e", "w"):
            anchor_x = box[0] + box[2] if side == "w" else box[0]
            cy = box[1] + box[3] / 2
            want_w = (anchor_x - x) if side == "w" else (x - anchor_x)
            avail_w = anchor_x if side == "w" else self._disp_w - anchor_x
            max_h = 2 * min(cy, self._disp_h - cy)
            w = min(max(want_w, MIN_BOX_W), avail_w, max_h / aspect)
            h = w * aspect
            box[0] = anchor_x - w if side == "w" else anchor_x
            box[1] = cy - h / 2
        else:
            anchor_y = box[1] + box[3] if side == "n" else box[1]
            cx = box[0] + box[2] / 2
            want_h = (anchor_y - y) if side == "n" else (y - anchor_y)
            avail_h = anchor_y if side == "n" else self._disp_h - anchor_y
            max_w = 2 * min(cx, self._disp_w - cx)
            h = min(max(want_h, MIN_BOX_W * aspect), avail_h, max_w * aspect)
            w = h / aspect
            box[0] = cx - w / 2
            box[1] = anchor_y - h if side == "n" else anchor_y
        box[2], box[3] = w, h

    def _resize_box_to(self, box, which, corner, x, y):
        """Resize from `corner` toward (x, y): the opposite corner stays anchored and
        the box keeps its output panel's aspect (width leads, height follows)."""
        anchor_x = box[0] + box[2] if "w" in corner else box[0]
        anchor_y = box[1] + box[3] if "n" in corner else box[1]
        aspect = max(self._panel_aspect(which), 1e-6)
        want_w = (anchor_x - x) if "w" in corner else (x - anchor_x)
        avail_w = anchor_x if "w" in corner else self._disp_w - anchor_x
        avail_h = anchor_y if "n" in corner else self._disp_h - anchor_y
        w = min(max(want_w, MIN_BOX_W), avail_w, avail_h / aspect)
        h = w * aspect
        box[0] = anchor_x - w if "w" in corner else anchor_x
        box[1] = anchor_y - h if "n" in corner else anchor_y
        box[2], box[3] = w, h

    def _on_source_release(self, _event):
        self._active_box = None
        self._resize_corner = None
        self._undo_commit()   # a drag is one undo step; the NEXT edit starts fresh

    def _on_source_motion(self, event):
        """Hover feedback: resize cursor near a corner, move cursor inside a box
        (highlights first, mirroring the press priority)."""
        if self.game_box is None or self._active_box:
            return
        x, y = event.x - self.off_x, event.y - self.off_y
        which, corner = self._hl_hit(x, y)
        if which is None:
            which, corner = self._hit_test(x, y)
        if corner:
            cursor = _RESIZE_CURSORS[corner]
        elif which is not None:
            cursor = "fleur"
        else:
            cursor = ""
        self._source_canvas.configure(cursor=cursor)

    # ── Source-canvas box guides: same throttle as the preview (see below) ───────────
    def _schedule_box_draw(self, delay_ms=30):
        """Throttles _draw_boxes the same way _schedule_preview_update throttles the
        composite. _on_source_drag used to call _draw_boxes() directly on every
        <B1-Motion> event (a full canvas.delete("boxes") + rect/corner-tick rebuild
        per event, unthrottled) - found while chasing a residual choppiness report
        after the preview throttle fix; this path was outside SPEC_5.5.3a's named
        scope (_on_preview_drag/_schedule_preview_update/update_live_preview only),
        so it kept the same starvation-shaped bug on the source canvas."""
        if self._box_draw_job is not None:
            return
        self._box_draw_job = self.after(delay_ms, self._fire_box_draw)

    def _fire_box_draw(self):
        self._box_draw_job = None
        self._draw_boxes()

    # ── Preview rendering (the live composite) + seam dragging ───────────────────────
    def _schedule_preview_update(self, delay_ms=80, fast=False):
        """Throttle the expensive composite so rapid drags don't pile up (guidance 5).
        `fast=True` (drags) composites at PREVIEW resolution so the image keeps up
        with the mouse. No-op during playback; the play ticks own the preview
        canvas, and the stop refresh brings the static composite back.

        This leaves a pending render job alone instead of cancelling and re-arming
        it: Tk fires <B1-Motion> on nearly every mouse-move sample, often faster
        than delay_ms apart, so a cancel-and-reschedule debounce can get pushed into
        the future forever and never fire during a real drag. update_live_preview
        reads all state fresh when it runs, so whichever call happened last before
        the pending job fires is what gets rendered; nothing is lost by not
        re-arming.

        A SEPARATE full-quality settle pass is debounced (cancel + re-arm, on
        purpose) 250ms out on every fast request: unlike the render throttle above,
        "wait until the drag actually goes quiet" is exactly what a debounce should
        do. An earlier version folded the settle into the same job slot as the
        throttled render (rescheduling itself for +250ms from inside
        update_live_preview) - that silently dropped the render cadence from ~30ms
        to ~250ms for the whole rest of a continuous drag, since the throttle above
        no-ops while ANY job (including that 250ms settle) is still pending. Found
        from a report that the preview canvas lagged in "very consistent" ~250-450ms
        steps - the settle interval, not the throttle's."""
        if self._playing:
            return
        self._preview_fast = self._preview_fast or fast
        if self._preview_job is None:
            self._preview_job = self.after(delay_ms, self.update_live_preview)
        if fast:
            if self._settle_job is not None:
                self.after_cancel(self._settle_job)
            self._settle_job = self.after(250, self._fire_settle)

    def _fire_settle(self):
        """The one-time full-quality render once a fast drag actually stops (see
        _schedule_preview_update). Pre-empts any still-pending throttled fast render
        so the crisp pass isn't immediately followed by a stale fast one."""
        self._settle_job = None
        if self._playing:
            return
        if self._preview_job is not None:
            self.after_cancel(self._preview_job)
            self._preview_job = None
        self._preview_fast = False
        self.update_live_preview()

    def _snapshot_watermark(self):
        """The compositor's watermark dict from current inspector state (UI-thread
        widget reads - cheap), or None while the burn is off. Shared by the static
        preview and the playback ticks."""
        if not self.wm_on.get():
            return None
        try:
            wm_scale = float(self.wm_scale_var.get())
        except (tk.TclError, ValueError):
            wm_scale = 1.0
        assets = self.app.paths.get_path("assets", create=False)
        icons = []
        if self.plat_twitch.get():
            icons.append(_platform_icon(assets, "twitch"))
        if self.plat_youtube.get():
            icons.append(_platform_icon(assets, "youtube"))
        return {
            "text": self._wm_entry.get(),
            "scale": wm_scale,
            "icons": icons,
            "icon_mode": "stack" if self.icon_mode_var.get() == "stacked" else "row",
            "text_bgr": _hex_to_bgr(self._text_hex, (255, 255, 255)),
            "plate_bgr": _hex_to_bgr(self._plate_hex, (12, 12, 12)),
            "position": self.wm_pos_var.get(),
        }

    def _snapshot_branding(self):
        """The compositor's branding/watermark overlay list from current model state
        (5.5.1g), or () when off. Independent of the name plate. Items without a loaded
        image are skipped. `_cache` is the SAME dict object as the live item's - a
        reference, not a copy - so the compositor's resize memoization actually
        persists across calls (see compositor._draw_branding_item)."""
        if not self.brand_on.get():
            return ()
        return [{
            "bgra": item["bgra"],
            "box": list(item["box"]),
            "opacity": float(item.get("opacity", 1.0)),
            "_cache": item.setdefault("_cache", {}),
        } for item in self.branding_items if item.get("bgra") is not None]

    def update_live_preview(self):
        """Snapshot widget values (UI thread), run the pure compositor, show the result.
        Fast mode (set by _schedule_preview_update during drags) composites straight at
        preview size - ~10× less work than full-res + LANCZOS - then schedules itself
        once more for the crisp full-quality settle."""
        self._preview_job = None
        fast = self._preview_fast
        self._preview_fast = False
        if self._path is None or self.game_box is None or self._playing:
            return
        frame = self._read_frame(self.frame_idx)
        if frame is None:
            return

        c = self._preview_canvas
        pw, ph = c.winfo_width(), c.winfo_height()
        if pw < 20 or ph < 20:
            return
        fit = min(pw / OUT_W, ph / OUT_H)
        self._pv_w, self._pv_h = max(1, int(OUT_W * fit)), max(1, int(OUT_H * fit))
        self._pv_off_x = (pw - self._pv_w) // 2
        self._pv_off_y = (ph - self._pv_h) // 2

        wm = self._snapshot_watermark()
        kwargs = dict(
            scale_x=self.scale_x, scale_y=self.scale_y,
            game_box=self.game_box, cam_box=self.cam_box,
            split_ratio=int(self.split_var.get()),
            melt=self.melt_on.get(), melt_px=int(self.melt_px.get()),
            solid_border=self.border_on.get(),
            border_color=_hex_to_bgr(self._border_hex, (15, 15, 15)),
            border_px=int(self.border_px.get()),
            watermark=wm,
            branding=self._snapshot_branding(),
            highlights=(filter_visible(self.highlights, self.highlights,
                                       self.frame_idx)
                        if self.hl_on.get() else ()),
            single_panel=self.single_panel.get(),
        )
        if fast:
            # The compositor scales all reference-space params (melt, watermark,
            # dest boxes) to the smaller out size itself (2.5c).
            composed = composite_frame(frame, out_w=self._pv_w, out_h=self._pv_h,
                                       **kwargs)
            pil = Image.fromarray(cv2.cvtColor(composed, cv2.COLOR_BGR2RGB))
        else:
            composed = composite_frame(frame, **kwargs)
            pil = Image.fromarray(
                cv2.cvtColor(composed, cv2.COLOR_BGR2RGB)).resize(
                    (self._pv_w, self._pv_h), Image.Resampling.LANCZOS)
        # Reuse the PhotoImage + canvas image item across calls instead of tearing
        # both down every render (measured ~40% of a realistic-size render at 1920x
        # 1080/portrait-preview scale: ~5ms of ~12ms was delete("all")+create_image
        # alone). .paste() only when the pixel size hasn't changed (e.g. a window
        # resize still reallocates); c.type() re-validates the item id because other
        # code paths (_render_play_frame, the no-clip placeholder) still wipe this
        # canvas with delete("all") and don't know about our persistent item.
        same_size = self._preview_photo_wh == (self._pv_w, self._pv_h)
        if same_size and self._preview_photo is not None:
            self._preview_photo.paste(pil)
        else:
            self._preview_photo = ImageTk.PhotoImage(pil)
            self._preview_photo_wh = (self._pv_w, self._pv_h)
        if self._preview_image_item is not None and c.type(self._preview_image_item) == "image":
            c.delete("guide")
            c.coords(self._preview_image_item, self._pv_off_x, self._pv_off_y)
            c.itemconfig(self._preview_image_item, image=self._preview_photo)
        else:
            c.delete("all")
            self._preview_image_item = c.create_image(
                self._pv_off_x, self._pv_off_y, image=self._preview_photo, anchor="nw")

        # Guides: the seam line always shows (it's the drag handle); melt guides follow
        # the melt toggle. Ported from the old app's dashed overlays. Tagged "guide" so
        # they clear without tearing down the persistent image item above.
        mid_y = self._seam_y()
        c.create_line(self._pv_off_x, mid_y, self._pv_off_x + self._pv_w, mid_y,
                      fill=PALETTE["green"], width=2, dash=(6, 2), tags="guide")
        # Melt guides are meaningless without a seam - hide them in single-panel (2.10.1).
        if (not self.single_panel.get() and self.melt_on.get()
                and int(self.melt_px.get()) > 0):
            zone = int(self.melt_px.get()) * (self._pv_h / OUT_H)
            y_top = max(self._pv_off_y, mid_y - zone)
            y_bot = min(self._pv_off_y + self._pv_h, mid_y + zone)
            for gy in (y_top, y_bot):
                c.create_line(self._pv_off_x, gy, self._pv_off_x + self._pv_w, gy,
                              fill=PALETTE["yellow"], width=1, dash=(3, 3), tags="guide")
        # Dest-box guides (2.5): preview-only orange outlines (NOT burned into the
        # output - the PiP itself is already inside the composite). A selected PiP
        # hidden at this frame (outside its 2.8.7 visible range) draws DASHED -
        # still selectable/editable, the composite simply omits it.
        if self.hl_on.get():
            for i, hl in enumerate(self.highlights):
                b = self._dest_in_canvas(hl["dest_box"])
                sel = i == self.sel_hl
                vis = hl.get("visible")
                shown_now = vis is None or vis[0] <= self.frame_idx <= vis[1]
                c.create_rectangle(b[0], b[1], b[0] + b[2], b[1] + b[3],
                                   outline=PALETTE["orange"], width=2 if sel else 1,
                                   dash=() if (sel and shown_now) else (4, 3),
                                   tags="guide")
                self._draw_corner_ticks(c, b[0], b[1], b[0] + b[2], b[1] + b[3],
                                        PALETTE["orange"], ("guide",),
                                        width=2 if sel else 1)
        # Branding placement outlines (5.5.1g): a blue rect per overlay showing the
        # draggable/resizable box. Preview-only affordance; the image itself is
        # already composited in.
        if self.brand_on.get():
            for i, item in enumerate(self.branding_items):
                b = self._dest_in_canvas(item["box"])
                sel = i == self.sel_brand
                c.create_rectangle(b[0], b[1], b[0] + b[2], b[1] + b[3],
                                   outline=PALETTE["blue"], width=2 if sel else 1,
                                   dash=() if sel else (4, 3), tags="guide")
                self._draw_corner_ticks(c, b[0], b[1], b[0] + b[2], b[1] + b[3],
                                        PALETTE["blue"], ("guide",),
                                        width=2 if sel else 1)
        # The full-quality settle pass is scheduled by _schedule_preview_update
        # itself (a debounce, separate from this render's own throttle job) - not
        # from here, so a continuous fast drag keeps rendering at the throttle's
        # cadence instead of falling back to the settle's slower one.

    def _seam_y(self):
        """Canvas y of the split seam inside the preview."""
        return self._pv_off_y + self._pv_h * int(self.split_var.get()) / 100

    def _on_preview_press(self, event):
        self._preview_canvas.focus_set()
        if self._path is None or self.game_box is None:
            return
        # Dest boxes win over the seam (the seam stays draggable along the rest of
        # the line); pressing a non-selected highlight selects it mid-gesture.
        i, corner = self._hl_dest_hit(event.x, event.y)
        if i is not None:
            self.select_highlight(i)
            self._pv_hl_drag = True
            self._pv_hl_corner = corner
            self._pv_drag_x, self._pv_drag_y = event.x, event.y
            return
        # 5.5.1g: drag a branding overlay to move/resize it (wins over the seam
        # underneath it - highlights still win over branding on overlap).
        i, corner = self._brand_dest_hit(event.x, event.y)
        if i is not None:
            self.select_branding(i)
            self._pv_brand_drag = True
            self._pv_brand_corner = corner
            self._pv_drag_x, self._pv_drag_y = event.x, event.y
            return
        if abs(event.y - self._seam_y()) <= 15:
            self._pv_seam_drag = True

    def _on_preview_drag(self, event):
        if self._pv_brand_drag:
            if self.sel_brand is None or self.sel_brand >= len(self.branding_items):
                return
            item = self.branding_items[self.sel_brand]
            if self._pv_brand_corner:
                ox = (event.x - self._pv_off_x) * OUT_W / max(1, self._pv_w)
                oy = (event.y - self._pv_off_y) * OUT_H / max(1, self._pv_h)
                locked = item.get("lock_aspect", True)
                if self._pv_brand_corner in _SIDE_CURSORS:
                    (self._resize_dest_brand_side if locked
                     else self._resize_dest_brand_free_side)(
                        item, self._pv_brand_corner, ox, oy)
                else:
                    (self._resize_dest_brand if locked
                     else self._resize_dest_brand_free)(
                        item, self._pv_brand_corner, ox, oy)
            else:
                d = item["box"]
                d[0] += (event.x - self._pv_drag_x) * OUT_W / max(1, self._pv_w)
                d[1] += (event.y - self._pv_drag_y) * OUT_H / max(1, self._pv_h)
                self._clamp_brand(item)
            self._pv_drag_x, self._pv_drag_y = event.x, event.y
            self._schedule_preview_update(30, fast=True)
            self._mark_dirty()
            return
        if self._pv_hl_drag:
            if self.sel_hl is None or self.sel_hl >= len(self.highlights):
                return
            hl = self.highlights[self.sel_hl]
            if self._pv_hl_corner:
                ox = (event.x - self._pv_off_x) * OUT_W / max(1, self._pv_w)
                oy = (event.y - self._pv_off_y) * OUT_H / max(1, self._pv_h)
                if self._pv_hl_corner in _SIDE_CURSORS:
                    self._resize_dest_hl_side(hl, self._pv_hl_corner, ox, oy)
                else:
                    self._resize_dest_hl(hl, self._pv_hl_corner, ox, oy)
            else:
                d = hl["dest_box"]
                d[0] += (event.x - self._pv_drag_x) * OUT_W / max(1, self._pv_w)
                d[1] += (event.y - self._pv_drag_y) * OUT_H / max(1, self._pv_h)
                self._clamp_dest(hl)
            self._pv_drag_x, self._pv_drag_y = event.x, event.y
            self._schedule_preview_update(30, fast=True)
            self._mark_dirty()
            return
        if not self._pv_seam_drag:
            return
        pct = (event.y - self._pv_off_y) / max(1, self._pv_h)
        # Single-panel: the "seam" is just the decorative line/plate - let it slide the
        # full 0..100% (matching the slider). Split: keep the 30..80 panel-division limits.
        lo, hi = (0.0, 1.0) if self.single_panel.get() else (0.30, 0.80)
        pct = max(lo, min(hi, pct))
        self.split_var.set(int(pct * 100))
        # A programmatic variable write does NOT fire the Scale's command - recalc here.
        self._on_split_change()
        self._schedule_preview_update(30, fast=True)   # override recalc's quality pass

    def _on_preview_release(self, _event):
        self._pv_seam_drag = False
        self._pv_hl_drag = False
        self._pv_hl_corner = None
        self._pv_brand_drag = False
        self._pv_brand_corner = None
        self._undo_commit()   # seam/PiP/branding drag over - close its undo step

    def _on_preview_motion(self, event):
        if self._path is None or self.game_box is None:
            return
        which, corner = self._hl_dest_hit(event.x, event.y)
        if which is None and corner is None:
            which, corner = self._brand_dest_hit(event.x, event.y)
        if corner:
            cursor = _RESIZE_CURSORS[corner]
        elif which is not None:
            cursor = "fleur"
        elif abs(event.y - self._seam_y()) <= 15:
            cursor = "sb_v_double_arrow"
        else:
            cursor = ""
        self._preview_canvas.configure(cursor=cursor)

    # ── Highlights (2.5): model ops, geometry, hit-testing ───────────────────────────
    def _hl_aspect(self, src_box):
        """Dest height-per-width that keeps the PiP undistorted: the aspect of the
        cropped SOURCE region (output px are square, so dest h/w must equal the crop's
        h/w in source pixels). Invariant under window resizes (uniform rescale)."""
        w_px = max(1e-6, src_box[2] * self.scale_x)
        return (src_box[3] * self.scale_y) / w_px

    def _hl_max_y(self):
        """Bottom limit for dest boxes: the seam (PiPs live on the gameplay panel -
        user rule, 2.5b) unless "Allow below the seam" is on, then the full frame."""
        if self.hl_below.get():
            return float(OUT_H)
        return OUT_H * int(self.split_var.get()) / 100

    def _clamp_dest(self, hl):
        """Keep a dest box inside its allowed output region, shrinking (aspect kept)
        if it outgrows the region."""
        d = hl["dest_box"]
        ar = max(1e-6, self._hl_aspect(hl["src_box"]))
        max_y = self._hl_max_y()
        if d[2] > OUT_W:
            d[2] = OUT_W
            d[3] = d[2] * ar
        if d[3] > max_y:
            d[3] = max_y
            d[2] = d[3] / ar
        d[0] = max(0.0, min(d[0], OUT_W - d[2]))
        d[1] = max(0.0, min(d[1], max_y - d[3]))

    def _clamp_all_dests(self):
        for hl in self.highlights:
            self._clamp_dest(hl)

    def _sync_dest_aspect(self, hl):
        """Re-lock the dest box to a (changed) src crop aspect: keep its width,
        re-derive height, clamp."""
        hl["dest_box"][3] = hl["dest_box"][2] * self._hl_aspect(hl["src_box"])
        self._clamp_dest(hl)

    def add_highlight(self):
        """Append a highlight (centered defaults, top of the output) and select it.
        The first is frictionless; past that a gentle non-blocking note about render
        cost appears (user's explicit ask: inform, never block)."""
        if self._path is None or self._disp_w <= 0:
            self._say("Load a clip first - nothing to highlight.")
            return
        if not self.hl_on.get():
            self.hl_on.set(True)   # +Add implies enabling (var.set won't re-fire command)
        sw, sh = self._disp_w * 0.30, self._disp_h * 0.18
        hl = {"src_box": [self._disp_w / 2 - sw / 2, self._disp_h * 0.08, sw, sh],
              "dest_box": [0.0, OUT_H * 0.05, OUT_W * 0.42, 0.0],
              "visible": None}   # None = whole clip (2.8.7 pop-in/out range)
        hl["dest_box"][0] = (OUT_W - hl["dest_box"][2]) / 2
        hl["dest_box"][3] = hl["dest_box"][2] * self._hl_aspect(hl["src_box"])
        self._clamp_dest(hl)
        self.highlights.append(hl)
        self.sel_hl = len(self.highlights) - 1
        if len(self.highlights) >= 2:
            self._say("Most clips need one highlight - extra PiPs add render cost "
                      "and clutter.", "yellow")
        else:
            self._say("Highlight added - crop it on the source, place it on the "
                      "preview.", "orange")
        self._refresh_hl_rows()
        self._draw_boxes()
        self._schedule_preview_update()
        self._mark_dirty()

    def remove_highlight(self, i):
        if not (0 <= i < len(self.highlights)):
            return
        self.highlights.pop(i)
        if not self.highlights:
            self.sel_hl = None
        elif self.sel_hl is not None:
            if self.sel_hl > i:
                self.sel_hl -= 1
            if self.sel_hl >= len(self.highlights):
                self.sel_hl = len(self.highlights) - 1
        self._refresh_hl_rows()
        self._draw_boxes()
        self._schedule_preview_update()
        self._mark_dirty()

    def select_highlight(self, i):
        if 0 <= i < len(self.highlights) and i != self.sel_hl:
            self.sel_hl = i
            self._refresh_hl_rows()
            self._draw_boxes()
            self._redraw_timeline()           # the visibility band follows selection
            self._schedule_preview_update()   # guide emphasis follows the selection

    # ── Highlight visibility range (2.8.7: pop-in / pop-out) ─────────────────────────
    def _sel_hl_dict(self):
        """The selected highlight dict, or None (guards every visibility action)."""
        if (self._path is not None and self.sel_hl is not None
                and self.sel_hl < len(self.highlights)):
            return self.highlights[self.sel_hl]
        return None

    def _hl_appear_here(self):
        """from = playhead. A `to` left of it goes to the clip end (the trim set-In
        rule); span stays ≥1 frame, never inverted."""
        hl = self._sel_hl_dict()
        if hl is None:
            return
        f, last = int(self.frame_idx), self.total_frames - 1
        vis = hl.get("visible")
        to = last if (vis is None or vis[1] < f) else vis[1]
        hl["visible"] = [f, to]
        self._after_visibility_change(f"PiP appears at F:{f}.")

    def _hl_disappear_here(self):
        """to = playhead. A `from` right of it goes to frame 0 (other-end adjust)."""
        hl = self._sel_hl_dict()
        if hl is None:
            return
        f = int(self.frame_idx)
        vis = hl.get("visible")
        frm = 0 if (vis is None or vis[0] > f) else vis[0]
        hl["visible"] = [frm, f]
        self._after_visibility_change(f"PiP disappears after F:{f}.")

    def _hl_reset_visible(self):
        hl = self._sel_hl_dict()
        if hl is None:
            return
        hl["visible"] = None
        self._after_visibility_change("PiP visible for the whole clip.")

    def _after_visibility_change(self, msg):
        self._refresh_hl_rows()
        self._redraw_timeline()
        self._schedule_preview_update()
        self._mark_dirty()        # autosave + per-clip block (chains _mark_clip_dirty)
        self._say(msg, "orange")

    def _clamp_visible(self, vis):
        """A persisted [from, to] clamped to the actual clip (the file may differ
        from what was saved); anything malformed → None (whole clip)."""
        try:
            f0, f1 = int(vis[0]), int(vis[1])
        except (TypeError, ValueError, IndexError):
            return None
        last = max(0, self.total_frames - 1)
        f0 = max(0, min(f0, last))
        return [f0, max(f0, min(f1, last))]

    def _on_hl_below_toggle(self):
        if self._applying:
            return
        if not self.hl_below.get():
            self._clamp_all_dests()   # back above the seam, visibly
        self._schedule_preview_update()
        self._mark_dirty()

    def _on_hl_toggle(self):
        if self._applying:
            return
        if self.hl_on.get() and not self.highlights:
            self.add_highlight()   # enabling with none yet: one click does it all
            return                 # (add_highlight refreshes + marks dirty)
        self._refresh_hl_rows()
        self._draw_boxes()
        self._schedule_preview_update()
        self._mark_dirty()

    def _refresh_hl_rows(self):
        """Rebuild the inspector's highlight list (select + ✕ per row). The extra-PiP
        note shows from the 2nd highlight on."""
        for w in self._hl_rows.winfo_children():
            w.destroy()
        if self.hl_on.get():
            for i in range(len(self.highlights)):
                row = tk.Frame(self._hl_rows, bg=PALETTE["bg"])
                row.pack(fill="x", pady=1)
                sel = i == self.sel_hl
                sel_btn = tk.Button(row, text=f"Highlight {i + 1}",
                                    command=lambda i=i: self.select_highlight(i),
                                    bg=PALETTE["card_hover"] if sel else PALETTE["card"],
                                    fg=PALETTE["orange"] if sel else PALETTE["text_dim"],
                                    activebackground=PALETTE["card_hover"],
                                    activeforeground=PALETTE["orange"], relief="flat",
                                    bd=0, anchor="w", padx=8, pady=2, cursor="hand2",
                                    font=ui_font(9))
                sel_btn.pack(side="left", fill="x", expand=True)
                Tooltip(sel_btn, "Select this highlight for editing.")
                rm_btn = tk.Button(row, text="✕",
                                   command=lambda i=i: self.remove_highlight(i),
                                   bg=PALETTE["panel"], fg=PALETTE["red_soft"],
                                   activebackground=PALETTE["card_hover"],
                                   activeforeground=PALETTE["red"], relief="flat", bd=0,
                                   padx=6, pady=2, cursor="hand2", font=ui_font(9))
                rm_btn.pack(side="left", padx=(4, 0))
                Tooltip(rm_btn, "Remove this highlight.")
        # Visibility range of the SELECTED PiP (2.8.7): label + Appear/Disappear/✕.
        self._hl_vis_label = None
        if (self.hl_on.get() and self.sel_hl is not None
                and self.sel_hl < len(self.highlights)):
            vis = self.highlights[self.sel_hl].get("visible")
            text = ("Visible: whole clip" if vis is None
                    else f"Visible: F:{vis[0]} - F:{vis[1]}")
            self._hl_vis_label = tk.Label(self._hl_rows, text=text, bg=PALETTE["bg"],
                                          fg=PALETTE["orange"], font=ui_font(8),
                                          anchor="w")
            self._hl_vis_label.pack(fill="x", pady=(4, 0))
            vrow = tk.Frame(self._hl_rows, bg=PALETTE["bg"])
            vrow.pack(fill="x", pady=(2, 0))
            for label, cmd, tip in (
                    ("Appear here", self._hl_appear_here,
                     "The highlight shows from the current frame on."),
                    ("Disappear here", self._hl_disappear_here,
                     "The highlight hides from the current frame on."),
                    ("✕", self._hl_reset_visible,
                     "Show the highlight for the whole clip again.")):
                vbtn = tk.Button(vrow, text=label, command=cmd, bg=PALETTE["panel"],
                                 fg=PALETTE["text_dim"],
                                 activebackground=PALETTE["card_hover"],
                                 activeforeground=PALETTE["text"], relief="flat", bd=0,
                                 padx=7, pady=2, cursor="hand2", font=ui_font(8))
                vbtn.pack(side="left", padx=(0, 5))
                Tooltip(vbtn, tip)
        if len(self.highlights) >= 2 and self.hl_on.get():
            self._hl_note.pack(anchor="w", pady=(4, 0))
        else:
            self._hl_note.pack_forget()

    def _hl_order(self):
        """Hit-test order: the selected highlight first (it wins overlaps)."""
        order = []
        if self.sel_hl is not None and self.sel_hl < len(self.highlights):
            order.append(self.sel_hl)
        order.extend(i for i in range(len(self.highlights)) if i not in order)
        return order

    def _hl_hit(self, x, y):
        """(highlight index, corner|side|None) whose src_box is at image-local (x, y),
        or (None, None). Corners beat sides beat interiors; selected beats the rest."""
        if not self.hl_on.get() or not self.highlights:
            return None, None
        for i in self._hl_order():
            corner = self._corner_hit(self.highlights[i]["src_box"], x, y)
            if corner:
                return i, corner
        for i in self._hl_order():
            side = self._side_hit(self.highlights[i]["src_box"], x, y)
            if side:
                return i, side
        for i in self._hl_order():
            b = self.highlights[i]["src_box"]
            if b[0] <= x <= b[0] + b[2] and b[1] <= y <= b[1] + b[3]:
                return i, None
        return None, None

    def _dest_in_canvas(self, d):
        """A dest_box (OUTPUT space) as [x, y, w, h] in preview-canvas coords."""
        return [self._pv_off_x + d[0] * self._pv_w / OUT_W,
                self._pv_off_y + d[1] * self._pv_h / OUT_H,
                d[2] * self._pv_w / OUT_W, d[3] * self._pv_h / OUT_H]

    # ── Branding / watermark overlays (5.5.1g): model ops, geometry, hit-testing ──────
    def _brand_order(self):
        """Hit-test order: the selected overlay first (it wins overlaps)."""
        order = []
        if self.sel_brand is not None and self.sel_brand < len(self.branding_items):
            order.append(self.sel_brand)
        order.extend(i for i in range(len(self.branding_items)) if i not in order)
        return order

    def _brand_dest_hit(self, cx, cy):
        """(overlay index, corner|side|None) whose box is at preview-canvas (cx, cy).
        Corners beat sides beat interiors, mirroring the highlight dest hit-test."""
        if not self.brand_on.get() or not self.branding_items:
            return None, None
        order = self._brand_order()
        for i in order:
            corner = self._corner_hit(
                self._dest_in_canvas(self.branding_items[i]["box"]), cx, cy)
            if corner:
                return i, corner
        for i in order:
            side = self._side_hit(
                self._dest_in_canvas(self.branding_items[i]["box"]), cx, cy)
            if side:
                return i, side
        for i in order:
            b = self._dest_in_canvas(self.branding_items[i]["box"])
            if b[0] <= cx <= b[0] + b[2] and b[1] <= cy <= b[1] + b[3]:
                return i, None
        return None, None

    @staticmethod
    def _brand_aspect(item):
        """Height-per-width of the overlay's SOURCE image (undistorted lock-aspect
        target); 1.0 (square) when no image is loaded."""
        bgra = item.get("bgra")
        if bgra is None:
            return 1.0
        sh, sw = bgra.shape[:2]
        return sh / max(1, sw)

    @staticmethod
    def _clamp_brand(item):
        """Keep an overlay box inside the OUTPUT frame (position-only drags; a
        resize already clamps itself against the frame edges)."""
        d = item["box"]
        d[2] = min(d[2], OUT_W)
        d[3] = min(d[3], OUT_H)
        d[0] = max(0.0, min(d[0], OUT_W - d[2]))
        d[1] = max(0.0, min(d[1], OUT_H - d[3]))

    def _resize_dest_brand(self, item, corner, ox, oy):
        """Corner-resize, aspect LOCKED to the source image (width leads, height
        follows), clamped to the output frame. Mirrors _resize_dest_hl."""
        d = item["box"]
        ar = max(1e-6, self._brand_aspect(item))
        anchor_x = d[0] + d[2] if "w" in corner else d[0]
        anchor_y = d[1] + d[3] if "n" in corner else d[1]
        want_w = (anchor_x - ox) if "w" in corner else (ox - anchor_x)
        avail_w = anchor_x if "w" in corner else OUT_W - anchor_x
        avail_h = anchor_y if "n" in corner else OUT_H - anchor_y
        w = min(max(want_w, 20.0), avail_w, avail_h / ar)
        h = w * ar
        d[0] = anchor_x - w if "w" in corner else anchor_x
        d[1] = anchor_y - h if "n" in corner else anchor_y
        d[2], d[3] = w, h

    def _resize_dest_brand_side(self, item, side, ox, oy):
        """Side-resize, aspect LOCKED: opposite edge anchored, cross axis centered,
        clamped to the output frame. Mirrors _resize_dest_hl_side."""
        d = item["box"]
        ar = max(1e-6, self._brand_aspect(item))
        if side in ("e", "w"):
            anchor_x = d[0] + d[2] if side == "w" else d[0]
            cy = d[1] + d[3] / 2
            want_w = (anchor_x - ox) if side == "w" else (ox - anchor_x)
            avail_w = anchor_x if side == "w" else OUT_W - anchor_x
            max_h = 2 * min(cy, OUT_H - cy)
            w = min(max(want_w, 20.0), avail_w, max_h / ar)
            h = w * ar
            d[0] = anchor_x - w if side == "w" else anchor_x
            d[1] = cy - h / 2
        else:
            anchor_y = d[1] + d[3] if side == "n" else d[1]
            cx = d[0] + d[2] / 2
            want_h = (anchor_y - oy) if side == "n" else (oy - anchor_y)
            avail_h = anchor_y if side == "n" else OUT_H - anchor_y
            max_w = 2 * min(cx, OUT_W - cx)
            h = min(max(want_h, 20.0 * ar), avail_h, max_w * ar)
            w = h / ar
            d[0] = cx - w / 2
            d[1] = anchor_y - h if side == "n" else anchor_y
        d[2], d[3] = w, h

    @staticmethod
    def _resize_dest_brand_free(item, corner, ox, oy):
        """Corner-resize, FREE aspect (lock aspect ratio is off - the user's choice
        of shape), clamped to the output frame. Mirrors _resize_src_hl."""
        d = item["box"]
        anchor_x = d[0] + d[2] if "w" in corner else d[0]
        anchor_y = d[1] + d[3] if "n" in corner else d[1]
        w = max(20.0, (anchor_x - ox) if "w" in corner else (ox - anchor_x))
        h = max(20.0, (anchor_y - oy) if "n" in corner else (oy - anchor_y))
        w = min(w, anchor_x if "w" in corner else OUT_W - anchor_x)
        h = min(h, anchor_y if "n" in corner else OUT_H - anchor_y)
        d[0] = anchor_x - w if "w" in corner else anchor_x
        d[1] = anchor_y - h if "n" in corner else anchor_y
        d[2], d[3] = w, h

    @staticmethod
    def _resize_dest_brand_free_side(item, side, ox, oy):
        """Side-resize, FREE aspect: only the dragged dimension changes, opposite
        edge anchored, clamped to the output frame."""
        d = item["box"]
        if side in ("e", "w"):
            anchor_x = d[0] + d[2] if side == "w" else d[0]
            w = max(20.0, (anchor_x - ox) if side == "w" else (ox - anchor_x))
            w = min(w, anchor_x if side == "w" else OUT_W - anchor_x)
            d[0] = anchor_x - w if side == "w" else anchor_x
            d[2] = w
        else:
            anchor_y = d[1] + d[3] if side == "n" else d[1]
            h = max(20.0, (anchor_y - oy) if side == "n" else (oy - anchor_y))
            h = min(h, anchor_y if side == "n" else OUT_H - anchor_y)
            d[1] = anchor_y - h if side == "n" else anchor_y
            d[3] = h

    def _hl_dest_hit(self, cx, cy):
        """(highlight index, corner|side|None) whose dest_box is at preview-canvas
        (cx, cy). Tested in canvas space so the grab tolerance matches the other
        boxes; corners beat sides beat interiors."""
        if not self.hl_on.get() or not self.highlights:
            return None, None
        for i in self._hl_order():
            corner = self._corner_hit(
                self._dest_in_canvas(self.highlights[i]["dest_box"]), cx, cy)
            if corner:
                return i, corner
        for i in self._hl_order():
            side = self._side_hit(
                self._dest_in_canvas(self.highlights[i]["dest_box"]), cx, cy)
            if side:
                return i, side
        for i in self._hl_order():
            b = self._dest_in_canvas(self.highlights[i]["dest_box"])
            if b[0] <= cx <= b[0] + b[2] and b[1] <= cy <= b[1] + b[3]:
                return i, None
        return None, None

    def _resize_src_hl_side(self, box, side, x, y):
        """Side-drag a highlight src crop (5.5.1d): only the dragged dimension
        changes (the crop aspect is free, matching its corner resize); the opposite
        edge stays anchored, clamped to the image."""
        if side in ("e", "w"):
            anchor_x = box[0] + box[2] if side == "w" else box[0]
            w = max(MIN_BOX_W, (anchor_x - x) if side == "w" else (x - anchor_x))
            w = min(w, anchor_x if side == "w" else self._disp_w - anchor_x)
            box[0] = anchor_x - w if side == "w" else anchor_x
            box[2] = w
        else:
            anchor_y = box[1] + box[3] if side == "n" else box[1]
            h = max(MIN_BOX_W, (anchor_y - y) if side == "n" else (y - anchor_y))
            h = min(h, anchor_y if side == "n" else self._disp_h - anchor_y)
            box[1] = anchor_y - h if side == "n" else anchor_y
            box[3] = h

    def _resize_dest_hl_side(self, hl, side, ox, oy):
        """Side-drag a dest box (5.5.1d): aspect-locked to the src crop like the
        corner path, opposite edge anchored, cross axis centered, clamped to the
        frame (and the seam limit)."""
        d = hl["dest_box"]
        ar = max(1e-6, self._hl_aspect(hl["src_box"]))
        if side in ("e", "w"):
            anchor_x = d[0] + d[2] if side == "w" else d[0]
            cy = d[1] + d[3] / 2
            want_w = (anchor_x - ox) if side == "w" else (ox - anchor_x)
            avail_w = anchor_x if side == "w" else OUT_W - anchor_x
            max_h = 2 * min(cy, self._hl_max_y() - cy)
            w = min(max(want_w, 40.0), avail_w, max_h / ar)
            h = w * ar
            d[0] = anchor_x - w if side == "w" else anchor_x
            d[1] = cy - h / 2
        else:
            anchor_y = d[1] + d[3] if side == "n" else d[1]
            cx = d[0] + d[2] / 2
            want_h = (anchor_y - oy) if side == "n" else (oy - anchor_y)
            avail_h = anchor_y if side == "n" else self._hl_max_y() - anchor_y
            max_w = 2 * min(cx, OUT_W - cx)
            h = min(max(want_h, 40.0 * ar), avail_h, max_w * ar)
            w = h / ar
            d[0] = cx - w / 2
            d[1] = anchor_y - h if side == "n" else anchor_y
        d[2], d[3] = w, h

    def _resize_src_hl(self, box, corner, x, y):
        """Resize a highlight src crop from `corner` toward (x, y) - FREE aspect (the
        crop shape is the user's choice; the dest box re-locks to it), clamped."""
        anchor_x = box[0] + box[2] if "w" in corner else box[0]
        anchor_y = box[1] + box[3] if "n" in corner else box[1]
        w = max(MIN_BOX_W, (anchor_x - x) if "w" in corner else (x - anchor_x))
        h = max(MIN_BOX_W, (anchor_y - y) if "n" in corner else (y - anchor_y))
        w = min(w, anchor_x if "w" in corner else self._disp_w - anchor_x)
        h = min(h, anchor_y if "n" in corner else self._disp_h - anchor_y)
        box[0] = anchor_x - w if "w" in corner else anchor_x
        box[1] = anchor_y - h if "n" in corner else anchor_y
        box[2], box[3] = w, h

    def _resize_dest_hl(self, hl, corner, ox, oy):
        """Resize a dest box from `corner` toward OUTPUT-space (ox, oy): aspect-locked
        to the src crop (width leads, height follows), clamped to the frame."""
        d = hl["dest_box"]
        ar = max(1e-6, self._hl_aspect(hl["src_box"]))
        anchor_x = d[0] + d[2] if "w" in corner else d[0]
        anchor_y = d[1] + d[3] if "n" in corner else d[1]
        want_w = (anchor_x - ox) if "w" in corner else (ox - anchor_x)
        avail_w = anchor_x if "w" in corner else OUT_W - anchor_x
        avail_h = anchor_y if "n" in corner else self._hl_max_y() - anchor_y
        w = min(max(want_w, 40.0), avail_w, avail_h / ar)
        h = w * ar
        d[0] = anchor_x - w if "w" in corner else anchor_x
        d[1] = anchor_y - h if "n" in corner else anchor_y
        d[2], d[3] = w, h

    # ── Inspector callbacks ──────────────────────────────────────────────────────────
    def _sync_mode_ui(self):
        """Re-range/relabel the split slider and show/hide the seam-melt + cam-reset
        controls for the current layout mode (2.10.1). Pure UI sync - callable under
        `_applying` from both `_on_mode_change` (user click) and `_apply_layout`
        (restored layout). Does NOT recalc boxes or mark dirty."""
        single = self.single_panel.get()
        if single:
            self._split_label.configure(text="Line position %")
            self._split_scale.configure(from_=0, to=100)
            self._seam_melt_box.pack_forget()       # no seam to melt
            self._reset_cam_btn.pack_forget()        # cam box is hidden
            self._reset_game_btn.configure(text="Reset content box")
        else:
            self._split_label.configure(text="Gameplay height %")
            self._split_scale.configure(from_=30, to=80)
            # A 0..100 line position may sit outside the 30..80 split range - clamp it.
            v = int(self.split_var.get())
            if v < 30 or v > 80:
                self.split_var.set(max(30, min(80, v)))
            # Re-show the melt group ABOVE the border block; re-show the cam reset.
            self._seam_melt_box.pack(fill="x", before=self._border_block)
            self._reset_cam_btn.pack(side="left", padx=(8, 0))
            self._reset_game_btn.configure(text="Reset game box")

    def _on_mode_change(self):
        """Layout-mode toggle (single-panel ↔ split). Swap the split slider's role,
        hide/show the seam-melt + cam controls, then re-lock the content box to the new
        aspect and refresh. The split-slider reconfigure runs under `_applying` so it
        doesn't re-mark dirty or fight the apply (BUILDER gotcha: configuring a Scale can
        nudge its value)."""
        if self._applying:
            return
        self._applying = True
        try:
            self._sync_mode_ui()
        finally:
            self._applying = False
        # The content box aspect changed (full-frame vs split panel) - re-lock + redraw.
        self.recalculate_box_sizes()
        self._schedule_preview_update()
        self._mark_dirty()

    def _on_split_change(self, _val=None):
        if self._applying:
            return
        if self.single_panel.get():
            # Single-panel: the slider moves the decorative line/plate, NOT panel
            # heights - the content box is full-frame, so don't re-lock its size.
            if self.highlights:
                self._clamp_all_dests()
            self._schedule_preview_update()
            self._mark_dirty()
            return
        self.recalculate_box_sizes()   # box sizes derive from the split; schedules preview
        if self.highlights:
            self._clamp_all_dests()    # the seam moved - confined PiPs follow it
        self._mark_dirty()

    def _on_layout_change(self, _val=None):
        if self._applying:
            return
        self._schedule_preview_update()
        self._mark_dirty()

    def add_branding(self):
        """Append a branding/watermark overlay: pick its image immediately (an overlay
        with no image is invisible and would just confuse the list), size it from the
        image's own aspect, and select it. Enables the feature if it was off."""
        if self._path is None:
            self._say("Load a clip first - nothing to overlay.")
            return
        path = filedialog.askopenfilename(
            title="Select branding/watermark image",
            filetypes=[("Image Files", "*.png *.jpg *.jpeg")])
        if not path:
            return
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            self._say("Couldn't read that image.", "red_soft")
            return
        if not self.brand_on.get():
            self.brand_on.set(True)   # +Add implies enabling (var.set won't re-fire)
        sh, sw = img.shape[:2]
        w = OUT_W * 0.18
        h = w * sh / max(1, sw)
        item = {"path": path, "bgra": img,
                "box": [OUT_W * 0.06, OUT_H * 0.06, w, h],
                "lock_aspect": True, "opacity": 1.0, "_cache": {}}
        self._clamp_brand(item)
        self.branding_items.append(item)
        self.sel_brand = len(self.branding_items) - 1
        self._refresh_brand_rows()
        self._sync_brand_ctrls()
        self._schedule_preview_update()
        self._mark_dirty()

    def remove_branding(self, i):
        if not (0 <= i < len(self.branding_items)):
            return
        self.branding_items.pop(i)
        if not self.branding_items:
            self.sel_brand = None
        elif self.sel_brand is not None:
            if self.sel_brand > i:
                self.sel_brand -= 1
            if self.sel_brand >= len(self.branding_items):
                self.sel_brand = len(self.branding_items) - 1
        self._refresh_brand_rows()
        self._sync_brand_ctrls()
        self._schedule_preview_update()
        self._mark_dirty()

    def select_branding(self, i):
        if 0 <= i < len(self.branding_items) and i != self.sel_brand:
            self.sel_brand = i
            self._refresh_brand_rows()
            self._sync_brand_ctrls()
            self._schedule_preview_update()   # guide emphasis follows the selection

    def _refresh_brand_rows(self):
        """Rebuild the inspector's branding-image list (select + remove per row),
        mirroring _refresh_hl_rows."""
        for w in self._brand_rows.winfo_children():
            w.destroy()
        if not self.brand_on.get():
            return
        for i, item in enumerate(self.branding_items):
            row = tk.Frame(self._brand_rows, bg=PALETTE["bg"])
            row.pack(fill="x", pady=1)
            sel = i == self.sel_brand
            name = os.path.basename(item.get("path", "") or "") or f"Image {i + 1}"
            sel_btn = tk.Button(row, text=name,
                                command=lambda i=i: self.select_branding(i),
                                bg=PALETTE["card_hover"] if sel else PALETTE["card"],
                                fg=PALETTE["blue"] if sel else PALETTE["text_dim"],
                                activebackground=PALETTE["card_hover"],
                                activeforeground=PALETTE["blue"], relief="flat",
                                bd=0, anchor="w", padx=8, pady=2, cursor="hand2",
                                font=ui_font(9))
            sel_btn.pack(side="left", fill="x", expand=True)
            Tooltip(sel_btn, "Select this overlay for editing.")
            rm_btn = tk.Button(row, text="✕",
                               command=lambda i=i: self.remove_branding(i),
                               bg=PALETTE["panel"], fg=PALETTE["red_soft"],
                               activebackground=PALETTE["card_hover"],
                               activeforeground=PALETTE["red"], relief="flat", bd=0,
                               padx=6, pady=2, cursor="hand2", font=ui_font(9))
            rm_btn.pack(side="left", padx=(4, 0))
            Tooltip(rm_btn, "Remove this overlay.")

    def _sync_brand_ctrls(self):
        """Show the selected-overlay controls whenever one is selected, hide them
        otherwise, and load its values into the shared widgets. Called from the
        toggle, add/remove/select, a browse, and layout restore."""
        if not hasattr(self, "_brand_ctrls"):
            return
        if (self.brand_on.get() and self.sel_brand is not None
                and self.sel_brand < len(self.branding_items)):
            item = self.branding_items[self.sel_brand]
            has_img = item.get("bgra") is not None
            self._brand_label.configure(
                text=os.path.basename(item.get("path", "") or "") or "(none)",
                fg=PALETTE["text_dim"] if has_img else PALETTE["text_faint"])
            self.brand_lock_var.set(bool(item.get("lock_aspect", True)))
            self.brand_opacity_var.set(int(round(float(item.get("opacity", 1.0)) * 100)))
            if not self._brand_ctrls.winfo_manager():
                self._brand_ctrls.pack(fill="x", pady=(2, 0))
        else:
            self._brand_ctrls.pack_forget()

    def _on_brand_toggle(self):
        """Enable-branding checkbox: refresh the list/controls, then recomposite."""
        self._refresh_brand_rows()
        self._sync_brand_ctrls()
        self._on_layout_change()

    def _browse_branding(self):
        """Pick a new image FILE for the SELECTED overlay (adding a new overlay is
        add_branding; this replaces the current one's source)."""
        if self.sel_brand is None or self.sel_brand >= len(self.branding_items):
            self._say("Select (or add) an overlay first.")
            return
        path = filedialog.askopenfilename(
            title="Select branding/watermark image",
            filetypes=[("Image Files", "*.png *.jpg *.jpeg")])
        if not path:
            return
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            self._say("Couldn't read that image.", "red_soft")
            return
        item = self.branding_items[self.sel_brand]
        item["path"] = path
        item["bgra"] = img
        item["_cache"] = {}   # a new source image invalidates the memoized resize
        self._sync_brand_ctrls()
        self._refresh_brand_rows()
        self._on_layout_change()

    def _on_brand_lock_change(self):
        if self.sel_brand is not None and self.sel_brand < len(self.branding_items):
            self.branding_items[self.sel_brand]["lock_aspect"] = \
                bool(self.brand_lock_var.get())
        self._on_layout_change()

    def _on_brand_opacity_change(self, _val=None):
        if self.sel_brand is not None and self.sel_brand < len(self.branding_items):
            pct = max(0, min(100, int(self.brand_opacity_var.get())))
            self.branding_items[self.sel_brand]["opacity"] = pct / 100.0
        self._on_layout_change()

    # ── Layout persistence (last-used + 5 slots) ─────────────────────────────────────
    @staticmethod
    def _norm_box(box, w, h):
        """[x,y,w,h] → 0..1 fractions of (w, h). The ONE place this arithmetic lives."""
        return [box[0] / w, box[1] / h, box[2] / w, box[3] / h]

    @staticmethod
    def _denorm_box(box, w, h):
        """0..1 fractions → [x,y,w,h] in (w, h) space (inverse of _norm_box)."""
        return [box[0] * w, box[1] * h, box[2] * w, box[3] * h]

    def _snapshot_layout(self):
        """Current layout as a plain dict. Boxes are NORMALIZED (0..1 of the displayed
        image) so the layout survives any window size."""
        dw, dh = max(1, self._disp_w), max(1, self._disp_h)
        try:
            wm_scale = float(self.wm_scale_var.get())
        except (tk.TclError, ValueError):
            wm_scale = 1.0
        return {
            "game_box": self._norm_box(self.game_box, dw, dh),
            "cam_box": self._norm_box(self.cam_box, dw, dh),
            "split_ratio": int(self.split_var.get()),
            "single_panel": bool(self.single_panel.get()),
            "melt": bool(self.melt_on.get()),
            "melt_px": int(self.melt_px.get()),
            "solid_border": bool(self.border_on.get()),
            "border_color": self._border_hex,
            "border_px": int(self.border_px.get()),
            "watermark": {
                "on": bool(self.wm_on.get()),
                "text": self._wm_entry.get(),
                "scale": wm_scale,
                "platforms": {"twitch": bool(self.plat_twitch.get()),
                              "youtube": bool(self.plat_youtube.get())},
                "icon_mode": "stack" if self.icon_mode_var.get() == "stacked" else "row",
                "position": self.wm_pos_var.get(),
                "text_color": self._text_hex,
                "plate_color": self._plate_hex,
            },
            "highlights_on": bool(self.hl_on.get()),
            "highlights_below_seam": bool(self.hl_below.get()),
            # src normalized over the displayed image (like the boxes), dest over the
            # 1080×1920 output; locked_aspect stored per spec so the dict stands alone.
            # {**h, …}: extra model keys (visible range 2.8.7, future ones) ride
            # along verbatim; preset slots strip what mustn't travel (_save_slot).
            "highlights": [{
                **h,
                "src_box": self._norm_box(h["src_box"], dw, dh),
                "dest_box": self._norm_box(h["dest_box"], OUT_W, OUT_H),
                "locked_aspect": self._hl_aspect(h["src_box"]),
            } for h in self.highlights],
            # 5.5.1g: branding/watermark image overlays (generalized from the single
            # 5.5.1f avatar into a list, like highlights). Box normalized over the
            # 1080×1920 output, like a highlight's dest_box.
            "branding_on": bool(self.brand_on.get()),
            "branding_items": [{
                "path": item.get("path", "") or "",
                "box": self._norm_box(item["box"], OUT_W, OUT_H),
                "lock_aspect": bool(item.get("lock_aspect", True)),
                "opacity": float(item.get("opacity", 1.0)),
            } for item in self.branding_items],
        }

    def _apply_layout(self, layout):
        """Set every control/box from a layout dict (missing keys → defaults). Boxes are
        materialized immediately when the display size is known, else on first render."""
        layout = layout or {}
        wm = layout.get("watermark") or {}
        self._applying = True
        try:
            # 2.10.1: restore layout mode + rebuild the inspector's split-slider
            # range/labels + seam-melt/cam visibility BEFORE writing split_ratio, so a
            # single-panel 0..100 line position isn't clamped by the split range still on
            # the scale (default split ⇒ old layouts/presets/blocks restore unchanged).
            self.single_panel.set(bool(layout.get("single_panel", False)))
            self._sync_mode_ui()
            self.split_var.set(int(layout.get("split_ratio", 50)))
            self.hl_on.set(bool(layout.get("highlights_on", False)))
            self.hl_below.set(bool(layout.get("highlights_below_seam", False)))
            self.melt_on.set(bool(layout.get("melt", True)))
            self.melt_px.set(int(layout.get("melt_px", 50)))
            self.border_on.set(bool(layout.get("solid_border", False)))
            self.border_px.set(int(layout.get("border_px", 12)))
            self._border_hex = layout.get("border_color") or DEFAULT_BORDER_HEX
            self.wm_on.set(bool(wm.get("on", False)))
            self._wm_entry.delete(0, "end")
            self._wm_entry.insert(0, wm.get("text", ""))
            self.wm_scale_var.set(float(wm.get("scale", 1.0)))
            platforms = wm.get("platforms") or {}
            self.plat_twitch.set(bool(platforms.get("twitch", True)))
            self.plat_youtube.set(bool(platforms.get("youtube", False)))
            self.icon_mode_var.set(
                "stacked" if wm.get("icon_mode") == "stack" else "side by side")
            self.wm_pos_var.set(wm.get("position", "center"))
            self._text_hex = wm.get("text_color") or DEFAULT_TEXT_HEX
            self._plate_hex = wm.get("plate_color") or DEFAULT_PLATE_HEX
            self._refresh_swatches()
            self.brand_on.set(bool(layout.get("branding_on", False)))
            items_data = layout.get("branding_items")
            legacy_avatar = None
            if items_data is None and wm.get("avatar_on") and wm.get("avatar_path"):
                # Migration (5.5.1g): a pre-generalization single avatar overlay
                # becomes the first branding item; old blocks otherwise lack this key.
                legacy_avatar = {
                    "path": wm.get("avatar_path", "") or "",
                    "lock_aspect": bool(wm.get("avatar_keep_aspect", True)),
                    "cx": float(wm.get("avatar_cx", 0.15)),
                    "cy": float(wm.get("avatar_cy", 0.12)),
                    "w": max(0.04, min(0.40, int(wm.get("avatar_size", 12)) / 100.0)),
                }
                self.brand_on.set(True)
            self.branding_items = []
            for data in (items_data or ([legacy_avatar] if legacy_avatar else [])):
                path = data.get("path", "") or ""
                bgra = (cv2.imread(path, cv2.IMREAD_UNCHANGED)
                        if path and os.path.exists(path) else None)
                box = data.get("box")
                if box is not None:
                    box = self._denorm_box(box, OUT_W, OUT_H)
                elif "cx" in data:   # legacy avatar: box derived from center + width
                    w_px = data["w"] * OUT_W
                    sh, sw = bgra.shape[:2] if bgra is not None else (1, 1)
                    h_px = w_px * sh / sw if sw > 0 else w_px
                    box = [data["cx"] * OUT_W - w_px / 2,
                           data["cy"] * OUT_H - h_px / 2, w_px, h_px]
                else:
                    box = [OUT_W * 0.06, OUT_H * 0.06, OUT_W * 0.18, OUT_H * 0.18]
                item = {"path": path, "bgra": bgra, "box": box,
                        "lock_aspect": bool(data.get("lock_aspect", True)),
                        "opacity": float(data.get("opacity", 1.0)), "_cache": {}}
                self._clamp_brand(item)
                self.branding_items.append(item)
            self.sel_brand = 0 if self.branding_items else None
            self._refresh_brand_rows()
            self._sync_brand_ctrls()
        finally:
            self._applying = False

        gb, cb = layout.get("game_box"), layout.get("cam_box")
        self._pending_norm = (gb, cb) if (gb and cb) else None
        self._pending_hl_norm = layout.get("highlights") or []
        self._materialize_boxes()

    def _mark_dirty(self):
        """Debounced autosave of the last-used layout (survives a crash better than
        save-on-close only; config.json is tiny so the write is cheap)."""
        if self._applying:
            return
        if self._autosave_job is not None:
            self.after_cancel(self._autosave_job)
        self._autosave_job = self.after(500, self._autosave_last)
        self._mark_clip_dirty()   # the layout is part of the per-clip block (2.8.2)

    def _autosave_last(self):
        self._autosave_job = None
        if self.game_box is None or self._disp_w <= 0:
            return
        self.app.config.setdefault("editor", {})["last_layout"] = self._snapshot_layout()
        self.app.save_config()

    def _slot_count(self):
        """Current number of layout slots: the MIN_SLOTS floor or the saved list
        length, whichever is larger (5.5.2e)."""
        presets = (self.app.config.get("editor") or {}).get("presets") or []
        return max(MIN_SLOTS, len(presets))

    def _presets_list(self):
        """The presets config list, padded to the current slot count with None so
        every slot index is addressable (5.5.2e)."""
        presets = self.app.config.setdefault("editor", {}).setdefault(
            "presets", [None] * MIN_SLOTS)
        while len(presets) < self._slot_count():
            presets.append(None)
        return presets

    def _rebuild_slot_rows(self):
        """(Re)create the slot-row widgets from the current slot count (5.5.2e). Same
        destroy-and-rebuild pattern as the exporter's snippet rows. Rebuilds
        self._slot_btns; _refresh_slots then relabels them from config."""
        for w in self._slots_frame.winfo_children():
            w.destroy()
        self._slot_btns = []
        for i in range(self._slot_count()):
            srow = tk.Frame(self._slots_frame, bg=PALETTE["bg"])
            srow.pack(fill="x", pady=2)
            load_btn = tk.Button(
                srow, text=f"{i + 1} · (empty)", command=lambda i=i: self._load_slot(i),
                bg=PALETTE["card"], fg=PALETTE["text_faint"],
                activebackground=PALETTE["card_hover"], activeforeground=PALETTE["text"],
                relief="flat", bd=0, anchor="w", padx=8, pady=3, cursor="hand2",
                font=ui_font(9))
            load_btn.pack(side="left", fill="x", expand=True)
            Tooltip(load_btn, "Load this saved layout.")
            save_btn = tk.Button(srow, text="save", command=lambda i=i: self._save_slot(i),
                                 bg=PALETTE["panel"], fg=PALETTE["text_dim"],
                                 activebackground=PALETTE["card_hover"],
                                 activeforeground=PALETTE["text"], relief="flat", bd=0,
                                 padx=8, pady=3, cursor="hand2", font=ui_font(9))
            save_btn.pack(side="left", padx=(6, 0))
            Tooltip(save_btn, "Save the current layout into this slot.")
            self._slot_btns.append(load_btn)
        self._refresh_slots()

    def _add_slot(self):
        """Append one empty slot (5.5.2e). No maximum."""
        presets = self._presets_list()
        presets.append(None)
        self.app.save_config()
        self._rebuild_slot_rows()
        self._say(f"Added layout slot {len(presets)}.", "green")

    def _delete_last_slot(self):
        """Remove the last slot (5.5.2e), refusing below the floor and confirming when
        the slot holds a saved layout."""
        presets = self._presets_list()
        allowed, needs_confirm, name = slot_delete_check(presets)
        if not allowed:
            self._say(f"{MIN_SLOTS} slots minimum.")
            return
        n = len(presets)
        if needs_confirm and not messagebox.askyesno(
                f"Delete slot {n}?",
                f"Slot {n} holds '{name}'. Delete it?", parent=self):
            return
        presets.pop()
        self.app.save_config()
        self._rebuild_slot_rows()
        self._say(f"Deleted layout slot {n}.", "green")

    def _save_slot(self, i):
        if self.game_box is None:
            self._say("Load a clip first - nothing to save yet.")
            return
        snap = self._snapshot_layout()
        snap["name"] = self._slot_name.get().strip() or f"Slot {i + 1}"
        for h in snap.get("highlights") or []:
            h.pop("visible", None)   # presets are clip-agnostic - absolute frame
            # ranges are meaningless on another clip (the 2.8.2 block keeps them)
        presets = self._presets_list()   # padded to the current slot count
        presets[i] = snap
        self.app.save_config()
        self._refresh_slots()
        self._say(f"Saved layout '{snap['name']}' to slot {i + 1}.", "green")

    def _load_slot(self, i):
        presets = (self.app.config.get("editor") or {}).get("presets") or []
        preset = presets[i] if i < len(presets) else None
        if not preset:
            self._say(f"Slot {i + 1} is empty.")
            return
        self._apply_layout(preset)
        self._mark_dirty()
        self._say(f"Loaded layout '{preset.get('name', f'Slot {i + 1}')}'.", "green")

    def _refresh_slots(self):
        presets = (self.app.config.get("editor") or {}).get("presets") or []
        for i, btn in enumerate(self._slot_btns):
            preset = presets[i] if i < len(presets) else None
            if preset:
                btn.configure(text=f"{i + 1} · {preset.get('name', '?')}",
                              fg=PALETTE["text_dim"])
            else:
                btn.configure(text=f"{i + 1} · (empty)", fg=PALETTE["text_faint"])

    # ── Per-clip edit persistence (2.8.2: layout + keyframes + trim + gain) ──────────
    @staticmethod
    def _clip_key(path):
        """Stable per-file config key: hash of the case-normalized absolute path."""
        return hashlib.sha1(
            os.path.normcase(os.path.abspath(path)).encode()).hexdigest()[:16]

    def _snapshot_clip_edits(self):
        """The FULL edit model as one plain dict - the per-clip config block. Keyframe
        boxes normalize with the same math as layout boxes (display-size independent);
        keyframed highlight dicts pass unknown keys through (2.8.7 extends them)."""
        dw, dh = max(1, self._disp_w), max(1, self._disp_h)
        try:
            gain = float(self.gain_var.get())
        except (tk.TclError, ValueError):
            gain = 0.0
        try:
            sharp = float(self.sharpen_strength.get())
        except (tk.TclError, ValueError):
            sharp = 1.0
        sharp = max(0.0, min(3.0, sharp))
        return {
            "path": os.path.abspath(self._path),   # human-readable; the key wins on lookup
            "saved": int(time.time()),              # LRU eviction ordinal
            "layout": self._snapshot_layout(),
            "keyframes": [{
                "frame": int(kf["frame"]),
                "type": kf["type"],
                "game_box": self._norm_box(kf["game_box"], dw, dh),
                "cam_box": self._norm_box(kf["cam_box"], dw, dh),
                "split_ratio": int(kf["split_ratio"]),
                "highlights": [
                    {**h,
                     "src_box": self._norm_box(h["src_box"], dw, dh),
                     "dest_box": self._norm_box(h["dest_box"], OUT_W, OUT_H)}
                    for h in (kf.get("highlights") or [])],
            } for kf in self.keyframes],
            # 2.10.2a: segments is the source of truth; trim stays one release as a
            # compatibility breadcrumb (the outer bounds) so half-migrated state still reads.
            "segments": [list(seg) for seg in self.segments],
            "trim": [int(self.in_frame), int(self.out_frame)],
            "gain_db": gain,
            # 3.1: render-only sharpen, per-clip like gain (off / 1.0 on a fresh clip).
            "sharpen": bool(self.sharpen_on.get()),
            "sharpen_strength": sharp,
        }

    def _save_clip_edits(self):
        """Write the current clip's block (the debounce target; also called
        synchronously as the flush on close and on clip switch). Evicts the
        oldest-saved blocks past the cap."""
        if self._clipsave_job is not None:
            self.after_cancel(self._clipsave_job)
            self._clipsave_job = None
        if self._path is None or self.game_box is None or self._disp_w <= 0:
            return
        edits = self.app.config.setdefault("editor", {}).setdefault("clip_edits", {})
        edits[self._clip_key(self._path)] = self._snapshot_clip_edits()
        while len(edits) > CLIP_EDITS_CAP:
            del edits[min(edits, key=lambda k: edits[k].get("saved", 0))]
        self.app.save_config()

    def _apply_clip_edits(self, block, merge_segments=True):
        """Restore a persisted block: layout via _apply_layout, keyframes via the same
        pending-normalized mechanism as the boxes (display size may be unknown at load
        - they materialize in _materialize_boxes), trim clamped to the actual frame
        count (the file could differ from what was saved), gain clamped to the spinbox
        range. Runs under _applying where it writes controls, so nothing re-marks dirty.
        merge_segments=False (undo/redo only, via _apply_undo_snapshot) restores the
        segments list exactly as captured - a snapshot was already valid when it was
        taken, and re-running the equal-gain merge would silently erase a Mark Audio
        boundary (5.5.2f) left at 0 dB, making undo look like it removed every marker
        at once."""
        self._pending_kf_norm = block.get("keyframes") or []
        self._apply_layout(block.get("layout") or {})   # also calls _materialize_boxes
        # 2.10.2a: prefer segments; fall back to the legacy single trim; else whole clip.
        # Normalize against THIS file's length (it may differ from when the block was saved).
        raw_segs = block.get("segments")
        if not raw_segs:
            trim = block.get("trim") or []
            raw_segs = ([[int(trim[0]), int(trim[1])]] if len(trim) == 2
                        else [[0, self.total_frames - 1]])
        self.segments = self._normalize_segments(
            raw_segs, self.total_frames, merge=merge_segments)
        try:
            gain = float(block.get("gain_db", 0.0))
        except (TypeError, ValueError):
            gain = 0.0
        try:
            sharp = float(block.get("sharpen_strength", 1.0))
        except (TypeError, ValueError):
            sharp = 1.0
        self._applying = True
        try:
            self.gain_var.set(max(-30.0, min(30.0, gain)))
            # 3.1: a block missing the keys restores to off / 1.0 (defaults-merge safety).
            self.sharpen_on.set(bool(block.get("sharpen", False)))
            self.sharpen_strength.set(max(0.0, min(3.0, sharp)))
        finally:
            self._applying = False

    def _mark_clip_dirty(self):
        """Debounced per-clip block save - the autosave pattern. Every edit-model
        mutation (boxes, keyframes, trim, gain, highlights) funnels through here,
        which also makes it the one spot that catches edits AFTER a render (5.5.1c:
        rendered → stale) AND the undo record hook (5.5.2a). The _applying and
        _undo_restoring guards keep programmatic restores out of both."""
        if self._applying or self._path is None or self._undo_restoring:
            return
        if self._render_state == "rendered":
            self._set_render_state("stale")
        # Undo (5.5.2a): the FIRST fire of a gesture records the settled pre-mutation
        # snapshot; the coalesce timer folds every further fire (drag motion events,
        # rapid edits) into the same step and re-snapshots when the dust settles.
        if self.game_box is not None and self._disp_w > 0:
            if self._undo_coalesce_job is None and self._undo_current is not None:
                self._undo_stack.record(self._undo_current)
                self._refresh_undo_buttons()
            if self._undo_coalesce_job is not None:
                self.after_cancel(self._undo_coalesce_job)
            self._undo_coalesce_job = self.after(600, self._undo_settle)
        if self._clipsave_job is not None:
            self.after_cancel(self._clipsave_job)
        self._clipsave_job = self.after(500, self._save_clip_edits)

    # ── Undo/redo (5.5.2a) ───────────────────────────────────────────────────────────
    def _undo_settle(self):
        """Coalesce timer fired: the gesture is over, snapshot the settled state as
        the new current (the next gesture's pre-mutation record)."""
        self._undo_coalesce_job = None
        if (self._path is not None and self.game_box is not None
                and self._disp_w > 0):
            self._undo_current = self._snapshot_clip_edits()

    def _undo_commit(self):
        """End-of-gesture commit (5.5.2b correction round): settle a pending
        coalesce NOW so the next edit starts its own undo step. Called from the
        pointer-release handlers - two distinct drags in quick succession are two
        steps, never one. The 600 ms timer keeps coalescing only continuous input
        (motion events inside one drag, typed digits, held spinbox arrows)."""
        if self._undo_coalesce_job is not None:
            self.after_cancel(self._undo_coalesce_job)
            self._undo_settle()

    def _refresh_undo_buttons(self):
        self._undo_btn.configure(
            state="normal" if self._undo_stack.can_undo else "disabled")
        self._redo_btn.configure(
            state="normal" if self._undo_stack.can_redo else "disabled")

    def _do_undo(self):
        if self._path is None:
            return
        if self._undo_coalesce_job is not None:   # commit the in-flight edit first
            self.after_cancel(self._undo_coalesce_job)
            self._undo_settle()
        snap = (self._undo_stack.undo(self._undo_current)
                if self._undo_current is not None else None)
        if snap is None:
            self._say("Nothing to undo.")
            return
        self._undo_current = snap
        self._apply_undo_snapshot(snap)
        self._refresh_undo_buttons()
        self._say("Undo.", "green")

    def _do_redo(self):
        if self._path is None:
            return
        if self._undo_coalesce_job is not None:
            self.after_cancel(self._undo_coalesce_job)
            self._undo_settle()
        snap = (self._undo_stack.redo(self._undo_current)
                if self._undo_current is not None else None)
        if snap is None:
            self._say("Nothing to redo.")
            return
        self._undo_current = snap
        self._apply_undo_snapshot(snap)
        self._refresh_undo_buttons()
        self._say("Redo.", "green")

    def _apply_undo_snapshot(self, snap):
        """Restore a history snapshot: the 2.8.2 restorer plus the same refresh a
        clip-load restore performs. Runs under _undo_restoring so the funnel records
        nothing; afterwards the restore still counts as an edit downstream (render
        state, per-clip save)."""
        self._undo_restoring = True
        try:
            self.selected_kf = None       # may point into the replaced keyframe list
            self._tl_drag_kf = None
            self._cut_start = None        # a pending cut spans states - cancel it
            self._refresh_cut_btn()
            self._sel_seg = None          # segments may be reshaped (5.5.2b)
            self._refresh_seg_gain_row()
            self._play_pcm = None         # segments may differ (2.10.2c PCM cache)
            self._play_pcm_key = None
            # merge_segments=False: this snapshot is a trusted in-session state (not a
            # possibly-stale persisted block), so restore it verbatim - see
            # _apply_clip_edits.
            self._apply_clip_edits(snap, merge_segments=False)
            self._render_source_frame()
            self._update_counter()
            self._redraw_timeline()
            self._schedule_preview_update()
        finally:
            self._undo_restoring = False
        if self._render_state == "rendered":
            self._set_render_state("stale")
        # Persist the restored state via the save debounce directly (the funnel is
        # guard-blocked by design during restores).
        if self._clipsave_job is not None:
            self.after_cancel(self._clipsave_job)
        self._clipsave_job = self.after(500, self._save_clip_edits)

    # ── Resize (debounced like the old on_window_resize_trigger) ────────────────────
    def _on_resize(self, _e):
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(60, self._apply_resize)

    def _apply_resize(self):
        self._resize_job = None
        # Keep the preview viewer 9:16 - its width follows its height. Re-applying the
        # same width is a no-op, so this settles instead of looping.
        ph = self._preview_canvas.winfo_height()
        if ph > 20:
            want = max(90, int(ph * 9 / 16))
            if want != self._preview_canvas.winfo_width():
                self._preview_canvas.configure(width=want)
                self.update_idletasks()  # flush geometry so the render reads fresh sizes
        if self._path is not None:
            self._render_source_frame()
        else:
            self._show_empty()
