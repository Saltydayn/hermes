"""Subtitle Studio tab: transcribe a clip's speech into subtitles, then fix it up.

Phase 6.2a built the shell (3-column layout, current-clip resolution, read-only
transcript display, file-based persistence). Phase 6.2b built the words: the
Transcript sub-tab settings (model/language/quality/device, prompt seeds), a
threaded faster-whisper engine with clean GPU-to-CPU fallback, and stale-cut
detection with a re-transcribe path. Phase 6.3 makes the transcript editable and
synced to the clip (6.3a: click-to-seek, split/merge/delete/insert, numeric timing
nudge, confidence review, manual non-speech entries, undo/redo, self-contained
playback) and adds the Config sub-tab for deeper engine tuning (6.3b: VAD,
hallucination filter, confidence cutoff, karaoke timing offset, an Expert drawer,
gap-candidate detection).

Toggleable like Import/Editor/Export - not always-on (a whole feature module that
needs a downloaded Whisper model, not a utility tab). Pulls the current clip from
the bus (edited_clip preferred over raw_clip, by publish recency) and reads the
editor's cut model ONLY through config (read-only) - never imports another module.

Playback is self-contained (not the editor's player): a cached cut-audio WAV plays
through its own sounddevice stream, mirroring the editor's "one stream per play,
degrade to silent on any failure" pattern, without importing clip_editor. The 9:16
preview composites the real crop/seam/watermark via `shared.compositor` (promoted
out of the editor package in this same round) reading the editor's saved layout
block read-only through `shared.subtitles.layout_for` - never importing clip_editor.
"""

import os
import queue
import shutil
import tempfile
import threading
import time
import wave
import tkinter as tk
from time import perf_counter
from tkinter import colorchooser, filedialog, messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from shared import assets, compositor, paths, subtitles, video_utils, whisper_engine
from shared import config as config_mod
from shared.layout_interp import interpolate_layout
from shared.ui_helpers import (
    HelpDialog, PALETTE, ScrollableFrame, Tooltip, TutorialIntro, TutorialReplayButton,
    ask_choice,
    TutorialWalkthrough, bind_scale_jump, display_font, mono_font, show_toast,
    round_rect, themed_button, ui_font,
)

try:
    import sounddevice as sd
except Exception:  # noqa: BLE001 - sounddevice/PortAudio may be entirely unavailable
    sd = None

_DEVICE_LABELS = {"auto": "Auto (GPU if ready)", "cpu": "CPU", "gpu": "GPU"}
_DEVICE_CODES = {v: k for k, v in _DEVICE_LABELS.items()}
_QUALITY_LABELS = (("fast", "Fast"), ("balanced", "Balanced"), ("best", "Best"))

_UNDO_CAP = 50
_UNDO_SEQS = ("<Control-z>", "<Control-Z>")
_REDO_SEQS = ("<Control-y>", "<Control-Y>", "<Control-Shift-z>", "<Control-Shift-Z>")
_SAVE_DEBOUNCE_MS = 400
_TICK_MS = 66             # fallback tick rate before a clip's real fps is known
_SEQ_GRAB_MAX = 12        # forward gap (frames) bridged by grab() instead of a cap.set seek
                          # (mirrors the editor's SEQ_GRAB_MAX - a re-seek re-decodes a whole
                          # GOP; grabbing through a small gap is far cheaper)
# 6.5 live-test round: the zoom window used to be sized by a fixed WORD COUNT
# around the anchor, so the pan bar's drag-to-word mapping snapped between
# discrete words - jumpy, and worse on a long transcript (each pixel of drag
# covered several words). A fixed TIME duration instead, panned by continuous
# seconds, scrolls smoothly regardless of transcript length.
_TL_ZOOM_WINDOW_S = 4.0

# 6.5 C1: zoomed-timeline row geometry (y-bands within one canvas, top to bottom -
# pan bar, then the word/event body with its drag handles split above and below,
# then the waveform).
# 6.5c: the two handles used to share ONE band above the chip, so a word's END
# handle sat right next to the next word's START handle and grabbing the one you
# wanted was fiddly. Now a span's start handle is at its TOP-left and its end
# handle at its BOTTOM-right, in separate bands - two adjacent handles can never
# overlap, because they are never on the same row.
# v1.1: the scrub bar moved from the top to the BOTTOM row, under the waveform,
# so it no longer crowds the word chips and their handles.
_TL_START_HANDLE_Y0, _TL_START_HANDLE_Y1 = 2, 9
_TL_BODY_Y0, _TL_BODY_Y1 = 12, 44
_TL_END_HANDLE_Y0, _TL_END_HANDLE_Y1 = 47, 54
# 6.5 live-test round: the waveform row was too thin to actually read the
# spikes against - given real height and a brighter fill.
_TL_WAVE_Y0, _TL_WAVE_Y1 = 58, 120
_TL_SCRUB_Y0, _TL_SCRUB_Y1 = 125, 141
_TL_CANVAS_H = 144
_TL_SCRUB_THROTTLE_MS = 33   # scrub seeks at most ~30x/s; the latest position always wins
_TL_HANDLE_HIT_TOL = 7    # px tolerance for grabbing a start/end handle
_TL_DRAG_REDRAW_MS = 33   # 6.5c: throttle ceiling for the zoomed strip's drag redraw
                          # (each redraw renders a styled chip per visible word) -
                          # a THROTTLE, not a debounce, so a continuous drag still
                          # repaints instead of starving
_TL_DRAG_SETTLE_MS = 130  # separate timer slot: refresh the preview once motion stops
                          # (a preview frame costs a real decode; see _preview_frame_for)
_MOTION_LABELS = (("none", "None"), ("pop", "Pop"), ("bump", "Bump"))
_TRANSITION_LABELS = (("cut", "Cut"), ("fade", "Fade"))
_REVEAL_LABELS = (("full_line", "Full line"), ("rolling", "Rolling"), ("one_word", "One word"))
_HIGHLIGHT_MODE_LABELS = (("box", "Box"), ("outline", "Outline"), ("image", "Image"))
_WORD_EFFECT_LABELS = (("shake", "Shake"), ("size-pop", "Pop"))
_TIMING_CHIP_SIZE_CAP = 26   # reference-space font size cap for zoomed timeline chips -
                             # readability at chip scale, independent of the real style size



class SubtitleStudioModule(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.bus = app.bus
        self._closing = False
        self._left_subtab = "transcript"
        self._transcribing = False
        self._transcribing_key = None     # clip_key of the clip being transcribed
        self._cancel_event = None
        self._ui_queue = queue.Queue()
        self._ui_pump_job = None
        self._lang_picker = None
        self._lang2_picker = None

        # Cached faster-whisper model (6.5 A1): construction pays the full weight
        # load + CUDA/cuDNN init cost, so reuse it across transcribes/warm-ups
        # instead of rebuilding on every run.
        self._whisper_model = None
        self._whisper_model_key = None    # (model_id, device, compute) it matches
        self._whisper_model_lock = threading.Lock()
        self._whisper_build_lock = threading.Lock()
        self._style_question_key = None   # clip whose keep-or-switch style question is pending
        self._style_save_job = None       # debounced config save for style edits
        self._regroup_job = None          # debounced transcript regroup for style sliders
        self._sash_restore_job = None
        self._typing_burst = False        # an undo step is already open for the current typing
        self._tx = None                   # transcribe progress estimate state (_tx_tick)
        self._tx_job = None
        self._sections = {}               # collapsible key -> (body, show)
        self._warm_key_inflight = None

        # Clip/transcript state (6.3a)
        self._subject = None
        self._clip_key = None
        self._loaded_for_key = None
        self._segs_frames = None
        self._fps = 0.0
        self._total_frames = 0
        self._cut_len_s = 0.0
        self._transcript = None
        self._sel = None                # ("word", li, wi) | ("event", idx) | None
        self._sel_char_offset = 0
        self._sel_time_vars = None      # (start_var, end_var) while a word is selected
        self._caret_hint = None         # (li, wi, offset) to restore after a re-render
        self._word_ranges = {}
        self._event_ranges = {}
        self._undo_stack = []
        self._redo_stack = []
        self._flagged_queue = []
        self._review_idx = -1
        self._save_job = None
        self._expert_expanded = False

        # Playback state (self-contained, mirrors the editor's stream-per-play pattern)
        self._pcm = None
        self._pcm_rate = 0
        self._wav_for_key = None
        self._wav_for_fp = None
        self._stream = None
        self._audio_token = None
        self._audio_pos = 0
        self._audio_start_sample = 0
        self._audio_seek = None
        self._playing = False
        self._playhead_t = 0.0
        self._play_t0 = 0.0
        self._play_clock0 = 0.0
        self._play_job = None
        self._cur_word_key = None
        self._preview_photo = None
        self._preview_out_wh = (0, 0)

        # 6.5 C4: drag-to-reposition the subtitle block in the preview.
        self._sub_drag_anchor0 = None
        self._sub_drag_x0 = 0
        self._sub_drag_y0 = 0

        # Persistent preview capture (6.4b: replaces the old per-frame
        # threading.Thread(extract_frame) - a fresh cv2.VideoCapture open+seek per
        # frame was the actual cause of the choppy 6.3 preview, not Tk). Opened
        # once per loaded clip, read synchronously on the UI thread every tick -
        # mirrors the editor's _read_frame/_open_video pattern exactly, just
        # without its LRU cache (Subtitle Studio's access pattern is overwhelmingly
        # sequential playback, not back-and-forth scrubbing).
        self._prev_cap = None
        self._prev_cap_path = None
        self._prev_cap_pos = -1
        self._preview_resize_job = None   # debounces the pane's <Configure> re-render
        self._preview_frame_cache = None  # (frame_idx, BGR frame) - see _preview_frame_for

        # Timeline zoom + drag-to-adjust-timing (UX feedback round). Zoomed is the
        # DEFAULT (6.5c): nudging a word's timing is the common reason to look at
        # this strip at all, and the whole-clip tick view can't be dragged.
        self._tl_zoom = True
        self._tl_window = None      # (lo_t, hi_t) of the currently-drawn zoom window, or None
        self._tl_drag = None        # (target, "start"|"end"|"move") mid-drag, or None
        self._tl_drag_window = None  # zoom window frozen for the duration of a drag
        self._tl_drag_grab_dt = 0.0  # press time minus span start, so a move never jumps
        self._tl_hits = []          # [(target, x0, x1)] from the last redraw - hit geometry
        self._tl_cursor = ""
        self._tl_redraw_job = None  # throttle (never a debounce) for the drag redraw
        self._tl_preview_job = None  # settle pass: refresh the preview once motion stops
        self._tl_chip_photos = []   # 6.4c: PhotoImage refs for the zoomed styled word chips

        # Pan offset (seconds) is kept at 0 since v1.1 replaced the pan bar with a
        # scrub bar; the resets below still clear it. Plus a cached waveform.
        self._tl_pan_offset = 0.0
        self._tl_scrubbing = False      # v1.1: dragging the bottom scrub bar
        self._tl_scrub_hover = False
        self._tl_scrub_job = None
        self._tl_scrub_t = 0.0
        # After a scrub the window keeps centring on the playhead (like playback)
        # until a word is selected again.
        self._tl_follow_playhead = False
        self._tl_wave_peaks = None
        self._tl_wave_for_key = None
        self._tl_wave_for_fp = None
        self._tl_stay_at_word = False   # see _tl_zoom_center_t

        # 6.5 B1: ONE live edit mode - the transcript is always directly editable
        # (click places a real cursor; type to fix a word, space splits, backspace
        # over a space merges) AND always navigable (click also selects + seeks,
        # for the toolbar/effects/timing fields). Reconciled back into the model
        # on a short pause after typing (below), never on a "commit" step.
        self._type_reconcile_job = None

        self._build()

        # Undo/redo from anywhere in the tab (timeline, preview, buttons), not
        # only while the transcript has focus; _accepts_shortcut keeps them to
        # this tab while it is on screen.
        top = self.winfo_toplevel()
        for seq in _UNDO_SEQS:
            top.bind(seq, self._on_undo_key, add="+")
        for seq in _REDO_SEQS:
            top.bind(seq, self._on_redo_key, add="+")

        self.bind("<Visibility>", self._on_visible)
        self._refresh_state()
        # No model warm-up here: it holds 1-3.5 GB of RAM (plus VRAM on the GPU) and
        # used to run at every app start even when this tab was never opened.
        # _on_visible warms it the first time the tab is actually shown.

        self._ui_pump_job = self.after(50, self._ui_pump)

    # ── UI construction ──────────────────────────────────────────────────────────────
    def _build(self):
        header = tk.Frame(self, bg=PALETTE["bg"])
        header.pack(fill="x", pady=(22, 0), padx=26)
        tk.Label(header, text="Subtitle Studio", bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=display_font(20)).pack(anchor="w")
        bar = tk.Frame(header, bg=PALETTE["blue"], width=66, height=3)
        bar.pack(anchor="w", pady=(7, 0))
        bar.pack_propagate(False)
        tk.Label(header, text="Turn your clip's speech into burned-in subtitles.",
                 bg=PALETTE["bg"], fg=PALETTE["text_mute"], font=ui_font(10)).pack(
            anchor="w", pady=(8, 0))

        body = tk.Frame(self, bg=PALETTE["bg"])
        body.pack(fill="both", expand=True, padx=22, pady=(14, 16))

        left = tk.Frame(body, bg=PALETTE["bg"], width=256)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        # 6.4b: center (transcript) and right (preview) share a draggable sash so
        # the user can grow the 9:16 preview to fill their screen height with no
        # app-added gutter - the split is a ttk.PanedWindow, not a fixed-width pack,
        # and its position persists across restarts (config["subtitles"]["preview_split"]).
        self._center_right_pw = ttk.PanedWindow(body, orient="horizontal")
        self._center_right_pw.pack(side="left", fill="both", expand=True, padx=(12, 0))

        center = tk.Frame(self._center_right_pw, bg=PALETTE["bg"])
        right = tk.Frame(self._center_right_pw, bg=PALETTE["bg"])
        self._center_right_pw.add(center, weight=3)
        self._center_right_pw.add(right, weight=1)

        self._build_left(left)
        self._build_center(center)
        self._build_right(right)

        self._center_right_pw.bind("<ButtonRelease-1>", self._on_sash_release)
        self._sash_restore_job = self.after(0, self._restore_sash_position)

        TutorialReplayButton(self, self._start_walkthrough,
                             "Need a refresher? Replay the tour.")

    _MIN_PANE_W = 240   # px each side of the sash keeps, whatever was saved

    def _restore_sash_position(self):
        """Restore the saved divider, CLAMPED to what the window can actually
        give it (6.5c). A position saved on a wide window and reopened on a
        narrower one used to collapse the transcript pane to nothing - the
        widgets were all there, just squeezed to zero width."""
        pos = int(self.app.config.get("subtitles", {}).get("preview_split", 0) or 0)
        if pos <= 0:
            return
        try:
            total = self._center_right_pw.winfo_width()
        except tk.TclError:
            return
        if total <= 2 * self._MIN_PANE_W:
            # Not laid out yet. Applying a saved position against a 1px-wide
            # pane makes Tk clamp it to 0, which collapses the transcript
            # column for the whole session - so wait for a real width instead
            # of restoring into nothing.
            self._sash_restore_tries = getattr(self, "_sash_restore_tries", 0) + 1
            if self._sash_restore_tries < 40:
                self._sash_restore_job = self.after(50, self._restore_sash_position)
            return
        try:
            self._center_right_pw.sashpos(
                0, max(self._MIN_PANE_W, min(pos, total - self._MIN_PANE_W)))
        except tk.TclError:
            pass

    def _on_sash_release(self, _event=None):
        try:
            pos = self._center_right_pw.sashpos(0)
        except tk.TclError:
            return
        self.app.config.setdefault("subtitles", {})["preview_split"] = pos
        self.app.save_config()
        self._render_preview_frame(self._playhead_t)

    def _build_left(self, left):
        # The sub-tab buttons are pinned at the top of the column, OUTSIDE the
        # scrolling area, so they never scroll out of reach; only the selected
        # tab's own content scrolls. The settings form is taller than a small
        # window's left column, so that content scrolls (mirrors the exporter's
        # right column - ScrollableFrame, house pattern) rather than clipping
        # Quality/Runs on/Transcribe off-screen.
        subtab_row = tk.Frame(left, bg=PALETTE["bg"])
        subtab_row.pack(fill="x", pady=(0, 10))
        self._transcript_tab_btn = self._make_subtab_btn(subtab_row, "Transcript", "transcript")
        self._transcript_tab_btn.pack(side="left", fill="x", expand=True)
        self._style_tab_btn = self._make_subtab_btn(subtab_row, "Style", "style")
        self._style_tab_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        # Label reads "Advanced"; the internal key stays "config" (config schema,
        # widget names, and the tour target all key off it).
        self._config_tab_btn = self._make_subtab_btn(subtab_row, "Advanced", "config")
        self._config_tab_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._subtab_btns = {"transcript": self._transcript_tab_btn,
                             "style": self._style_tab_btn,
                             "config": self._config_tab_btn}

        # Pinned under the scrolling tabs, so the one button that matters at
        # the end is always in reach (it used to sit at the very bottom of the
        # Style tab).
        self._build_render_footer(left)

        self._left_scroll = ScrollableFrame(left)
        self._left_scroll.pack(fill="both", expand=True)
        host = self._left_scroll.body

        self._transcript_panel = tk.Frame(host, bg=PALETTE["bg"])
        self._transcript_panel.pack(fill="both", expand=True)
        self._style_panel = tk.Frame(host, bg=PALETTE["bg"])
        self._build_style_panel(self._style_panel)
        self._config_panel = tk.Frame(host, bg=PALETTE["bg"])
        self._build_config_panel(self._config_panel)

        self._no_model_lbl = tk.Label(
            self._transcript_panel,
            text="Get a speech model in the Downloads tab first.",
            bg=PALETTE["bg"], fg=PALETTE["orange"], font=ui_font(10),
            wraplength=220, justify="left", anchor="w")

        self._settings_frame = tk.Frame(self._transcript_panel, bg=PALETTE["bg"])
        self._build_settings_form(self._settings_frame)
        self._style_subtab_btns()

    def _build_render_footer(self, left):
        foot = tk.Frame(left, bg=PALETTE["bg"])
        foot.pack(side="bottom", fill="x", pady=(8, 0))
        tk.Frame(foot, bg=PALETTE["border"], height=1).pack(fill="x", pady=(0, 8))
        self._sub_enable_lbl = tk.Label(foot, text="", bg=PALETTE["bg"],
                                        font=ui_font(9, "bold"), anchor="w")
        self._sub_enable_lbl.pack(fill="x")
        self._srt_var = tk.BooleanVar(
            value=bool(self.app.config.get("subtitles", {}).get("srt_export", False)))
        srt_chk = ttk.Checkbutton(foot, text="Also save a .srt file",
                                  variable=self._srt_var, command=self._on_srt_toggle)
        srt_chk.pack(anchor="w", pady=(4, 4))
        Tooltip(srt_chk, "A plain subtitle file next to the video, for platforms "
                         "with their own captions. Text and timing only - no "
                         "colors, effects, or animation.")
        apply_btn = themed_button(foot, "Apply & Render", self._on_apply_and_render,
                                  kind="primary")
        apply_btn.pack(fill="x")
        self._apply_btn = apply_btn
        Tooltip(apply_btn, "Turns subtitles on for this clip and renders it in the "
                           "Clip Editor. Style changes made afterwards apply to "
                           "this clip automatically.")
        self._disable_sub_btn = themed_button(
            foot, "Turn off for this clip", self._on_disable_subtitles, kind="neutral")
        self._refresh_subtitle_enable_label()

    def _make_subtab_btn(self, parent, text, name):
        """A tab-toggle button that actually restyles on switch. Not `themed_button`
        - that factory's hover effect closes over a fixed `kind` at creation time,
        so reconfiguring its colors later leaves a stale hover-leave color (the
        real cause of "Transcript" always looking selected).

        takefocus=0 matters: selecting this module's notebook tab makes ttk move
        focus to the pane's first focusable widget, which was this row's first
        button - and a focused Tk button on Windows paints a dotted focus rect
        that highlightthickness=0 does not suppress, so "Transcript" always wore
        a dotted outline. These are mouse-driven toggles; they never need
        keyboard traversal."""
        btn = tk.Button(parent, text=text, relief="flat", bd=0, padx=8, pady=7,
                        cursor="hand2", highlightthickness=0, font=ui_font(10),
                        takefocus=0, command=lambda: self._show_subtab(name))
        return btn

    def _style_subtab_btns(self):
        for name, btn in self._subtab_btns.items():
            active = name == self._left_subtab
            bg = PALETTE["green"] if active else PALETTE["panel"]
            fg = PALETTE["deep"] if active else PALETTE["text"]
            btn.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)

    def _show_subtab(self, name):
        self._left_subtab = name
        self._transcript_panel.pack_forget()
        self._style_panel.pack_forget()
        self._config_panel.pack_forget()
        if name == "transcript":
            self._transcript_panel.pack(fill="both", expand=True)
        elif name == "style":
            self._style_panel.pack(fill="both", expand=True)
        else:
            self._config_panel.pack(fill="both", expand=True)
        self._style_subtab_btns()

    # ── Settings form (6.2b) ──────────────────────────────────────────────────────────
    def _build_settings_form(self, parent):
        scfg = self.app.config.setdefault("subtitles", {})

        tk.Label(parent, text="MODEL", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(anchor="w")
        self._model_var = tk.StringVar()
        self._model_combo = ttk.Combobox(parent, textvariable=self._model_var,
                                         state="readonly")
        self._model_combo.pack(fill="x", pady=(2, 10))
        self._model_combo.bind("<<ComboboxSelected>>", self._on_model_change)
        self._model_ids_by_name = {}

        tk.Label(parent, text="LANGUAGE", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(anchor="w")
        self._lang_picker = self._make_language_picker(
            parent, scfg.get("language", "auto"), self._on_language_change)

        # v1.1: Model + Language + Transcribe is all most clips need; the
        # spelling hints, quality, and device fold away under "More options".
        opts = self._collapsible(parent, "More options", "transcript_more")
        self._game_terms_var = tk.BooleanVar(value=bool(scfg.get("game_terms", False)))
        self._game_terms_chk = ttk.Checkbutton(
            opts, text="Contains game terms", variable=self._game_terms_var,
            command=self._on_game_terms_toggle)
        self._game_terms_chk.pack(anchor="w")

        self._game_terms_frame = tk.Frame(opts, bg=PALETTE["bg"])
        self._game_terms_text_var = tk.StringVar(value=scfg.get("game_terms_text", ""))
        self._game_terms_entry = ttk.Entry(self._game_terms_frame,
                                          textvariable=self._game_terms_text_var)
        self._game_terms_entry.pack(fill="x", pady=(4, 0))
        self._game_terms_entry.bind("<FocusOut>", self._on_game_terms_text_change)
        self._game_terms_entry.bind("<Return>", self._on_game_terms_text_change)
        tk.Label(self._game_terms_frame,
                 text="Specific words or names (optional) - character names, item "
                     "names, callouts. Example: Tempest, Noita, gg ez",
                 bg=PALETTE["bg"], fg=PALETTE["text_faint"], font=ui_font(8),
                 wraplength=220, justify="left", anchor="w").pack(fill="x", pady=(2, 0))
        if scfg.get("game_terms"):
            self._game_terms_frame.pack(fill="x", pady=(4, 0))

        self._second_lang_var = tk.BooleanVar(value=bool(scfg.get("second_language")))
        self._second_lang_chk = ttk.Checkbutton(
            opts, text="Has a 2nd language", variable=self._second_lang_var,
            command=self._on_second_lang_toggle)
        self._second_lang_chk.pack(anchor="w", pady=(8, 0))

        self._second_lang_frame = tk.Frame(opts, bg=PALETTE["bg"])
        self._lang2_picker = self._make_language_picker(
            self._second_lang_frame, scfg.get("second_language") or "en",
            self._on_second_language_change)

        tk.Label(self._second_lang_frame, text="How much of it?", bg=PALETTE["bg"],
                 fg=PALETTE["text_faint"], font=ui_font(8)).pack(anchor="w")
        weight_row = tk.Frame(self._second_lang_frame, bg=PALETTE["bg"])
        weight_row.pack(fill="x")
        tk.Label(weight_row, text="A few terms", bg=PALETTE["bg"],
                 fg=PALETTE["text_faint"], font=ui_font(7)).pack(side="left")
        self._second_weight_var = tk.IntVar(
            value=int(scfg.get("second_language_weight", 50)))
        self._second_weight_scale = tk.Scale(
            weight_row, from_=0, to=100, orient="horizontal", resolution=1,
            showvalue=0, variable=self._second_weight_var,
            command=self._on_second_weight_change, bg=PALETTE["panel"],
            troughcolor=PALETTE["deep"], activebackground=PALETTE["green"],
            fg=PALETTE["text_dim"], highlightthickness=0, bd=0, sliderrelief="flat",
            sliderlength=14, width=10)
        self._second_weight_scale.pack(side="left", fill="x", expand=True, padx=(6, 6))
        bind_scale_jump(self._second_weight_scale)
        tk.Label(weight_row, text="Almost 50/50", bg=PALETTE["bg"],
                 fg=PALETTE["text_faint"], font=ui_font(7)).pack(side="left")
        Tooltip(weight_row, "How heavily the 2nd language mixes in - e.g. Noita's "
                            "in-game terms are almost all Finnish, so that clip "
                            "would sit near 'Almost 50/50'.")

        if scfg.get("second_language"):
            self._second_lang_frame.pack(fill="x", pady=(4, 0))

        tk.Label(opts, text="These quietly tell the model to expect your slang "
                              "and mixed speech, for cleaner spelling.",
                 bg=PALETTE["bg"], fg=PALETTE["text_faint"], font=ui_font(8),
                 wraplength=220, justify="left", anchor="w").pack(fill="x", pady=(8, 10))

        tk.Label(opts, text="QUALITY (precision)", bg=PALETTE["bg"],
                 fg=PALETTE["text_mute"], font=ui_font(9)).pack(anchor="w")
        quality_row = tk.Frame(opts, bg=PALETTE["bg"])
        quality_row.pack(fill="x", pady=(2, 2))
        self._quality_btns = {}
        for key, label in _QUALITY_LABELS:
            btn = self._make_quality_button(quality_row, key, label)
            btn.pack(side="left", fill="x", expand=True)
            self._quality_btns[key] = btn
        Tooltip(quality_row, "Fast = int8, Balanced = float16, Best = float32. "
                            "Higher is more accurate; near-free on GPU, slower on CPU.")
        tk.Label(opts, text="Higher is more accurate; near-free on GPU, slower "
                              "on CPU.", bg=PALETTE["bg"], fg=PALETTE["text_faint"],
                 font=ui_font(8), wraplength=220, justify="left",
                 anchor="w").pack(fill="x", pady=(2, 10))
        self._highlight_quality()

        tk.Label(opts, text="RUNS ON", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(anchor="w")
        self._device_var = tk.StringVar(
            value=_DEVICE_LABELS.get(scfg.get("device", "auto"),
                                     _DEVICE_LABELS["auto"]))
        self._device_combo = ttk.Combobox(
            opts, textvariable=self._device_var, state="readonly",
            values=list(_DEVICE_LABELS.values()))
        self._device_combo.pack(fill="x", pady=(2, 2))
        self._device_combo.bind("<<ComboboxSelected>>", self._on_device_change)
        self._device_status_lbl = tk.Label(
            opts, text="", bg=PALETTE["bg"], fg=PALETTE["text_faint"],
            font=ui_font(8), wraplength=220, justify="left", anchor="w")
        self._device_status_lbl.pack(fill="x", pady=(0, 10))
        self._refresh_device_status()

        # The button and the progress block swap inside one slot, so the button
        # comes back where it was after a run (it used to re-pack at the bottom
        # of the panel, below the auto-sync option).
        slot = tk.Frame(parent, bg=PALETTE["bg"])
        slot.pack(fill="x", pady=(12, 0))
        self._transcribe_btn = themed_button(
            slot, "Transcribe clip", self._on_transcribe, kind="primary")
        self._transcribe_btn.pack(fill="x", pady=(4, 0))

        self._progress_frame = tk.Frame(slot, bg=PALETTE["bg"])
        self._progress_lbl = tk.Label(
            self._progress_frame, text="", bg=PALETTE["bg"], fg=PALETTE["text_dim"],
            font=ui_font(9), anchor="w")
        self._progress_lbl.pack(fill="x")
        self._progress_bar = ttk.Progressbar(self._progress_frame, mode="determinate",
                                             maximum=100)
        self._progress_bar.pack(fill="x", pady=(4, 4))
        self._cancel_btn = themed_button(self._progress_frame, "Cancel",
                                         self._on_cancel_transcribe, kind="danger")
        self._cancel_btn.pack(fill="x")

        self._auto_var = tk.BooleanVar(value=bool(scfg.get("auto_transcribe", False)))
        auto_chk = ttk.Checkbutton(
            parent, text="Auto-transcribe & keep in sync", variable=self._auto_var,
            command=self._on_auto_toggle)
        auto_chk.pack(anchor="w", pady=(14, 0))
        self._auto_chk = auto_chk
        tk.Label(parent, text="Locks these settings and transcribes every new clip "
                              "on its own, and again when you change the cuts. If "
                              "you fixed words by hand, it asks first.",
                 bg=PALETTE["bg"], fg=PALETTE["text_faint"], font=ui_font(8),
                 wraplength=220, justify="left", anchor="w").pack(fill="x", pady=(2, 0))

        self._status_lbl = tk.Label(
            parent, text="", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
            font=ui_font(8), wraplength=220, justify="left", anchor="w")
        self._status_lbl.pack(fill="x", pady=(8, 0))

    def _make_language_picker(self, parent, initial_code, on_change):
        """A collapsible type-to-filter language picker. Collapsed (the default and
        the state it returns to after a pick) shows the current selection as a
        single clickable field; clicking it expands a search Entry + Listbox
        (subtitles.filter_languages) in its place, and picking an entry applies it
        and collapses back down. Returns a dict {"code", "collapsed", "entry",
        "listbox", "set_locked", ...} - `code` is always the live selection."""
        state = {"code": initial_code, "filtered": list(subtitles.LANGUAGES),
                 "expanded": False, "locked": False}

        container = tk.Frame(parent, bg=PALETTE["bg"])
        container.pack(fill="x", pady=(2, 6))

        collapsed = tk.Frame(container, bg=PALETTE["card"],
                             highlightbackground=PALETTE["border"],
                             highlightthickness=1, cursor="hand2")
        collapsed_lbl = tk.Label(collapsed, text="", bg=PALETTE["card"],
                                 fg=PALETTE["text"], font=ui_font(10), anchor="w",
                                 cursor="hand2")
        collapsed_lbl.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=6)
        chev = tk.Label(collapsed, text="▾", bg=PALETTE["card"],
                        fg=PALETTE["text_mute"], font=ui_font(9), cursor="hand2")
        chev.pack(side="right", padx=(4, 10))

        expanded = tk.Frame(container, bg=PALETTE["bg"])
        entry = ttk.Entry(expanded)
        entry.pack(fill="x")
        listbox = tk.Listbox(
            expanded, height=6, bg=PALETTE["card"], fg=PALETTE["text_dim"],
            selectbackground=PALETTE["green"], selectforeground=PALETTE["deep"],
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=PALETTE["border"], font=ui_font(9),
            exportselection=False)
        listbox.pack(fill="x", pady=(2, 0))

        def refresh_list(query=""):
            state["filtered"] = subtitles.filter_languages(query)
            listbox.delete(0, "end")
            for _code, name in state["filtered"]:
                listbox.insert("end", name)
            idx = 0
            for i, (code, _name) in enumerate(state["filtered"]):
                if code == state["code"]:
                    idx = i
                    break
            if state["filtered"]:
                listbox.selection_clear(0, "end")
                listbox.selection_set(idx)
                listbox.activate(idx)
                listbox.see(idx)

        def update_collapsed_label():
            code = state["code"]
            # language_name("auto") is worded for sentences ("...in the detected
            # language"); the picker itself reads Auto-detect.
            collapsed_lbl.configure(text="Auto-detect" if code in (None, "", "auto")
                                    else subtitles.language_name(code))

        def set_expanded(val):
            state["expanded"] = val
            if val:
                collapsed.pack_forget()
                entry.delete(0, "end")
                refresh_list()
                expanded.pack(fill="x")
                entry.focus_set()
            else:
                expanded.pack_forget()
                collapsed.pack(fill="x")

        def on_collapsed_click(_e=None):
            if state["locked"]:
                return
            set_expanded(not state["expanded"])

        def on_search(event=None):
            # KeyRelease also fires for Up/Down/Return (used for navigation/confirm
            # below) - re-filtering on those would reset the just-moved highlight.
            if event is not None and event.keysym in ("Up", "Down", "Return"):
                return
            refresh_list(entry.get())

        def move_active(delta):
            size = listbox.size()
            if size == 0:
                return
            cur = listbox.index("active")
            new = max(0, min(size - 1, cur + delta))
            listbox.selection_clear(0, "end")
            listbox.selection_set(new)
            listbox.activate(new)
            listbox.see(new)

        def on_arrow_down(_e=None):
            move_active(1)
            return "break"

        def on_arrow_up(_e=None):
            move_active(-1)
            return "break"

        def on_hover_motion(event):
            # Hover previews (highlights) a row without confirming it - only a
            # click or Enter actually applies the selection.
            idx = listbox.nearest(event.y)
            if idx < 0 or idx >= listbox.size() or idx in listbox.curselection():
                return
            listbox.selection_clear(0, "end")
            listbox.selection_set(idx)
            listbox.activate(idx)

        def confirm_index(idx):
            if idx is None or idx < 0 or idx >= len(state["filtered"]):
                return
            code, _name = state["filtered"][idx]
            state["code"] = code
            update_collapsed_label()
            set_expanded(False)
            on_change(code)

        def on_enter(_e=None):
            sel = listbox.curselection()
            if sel:
                confirm_index(sel[0])
            return "break"

        def on_click(event):
            confirm_index(listbox.nearest(event.y))
            return "break"

        collapsed_lbl.bind("<Button-1>", on_collapsed_click)
        chev.bind("<Button-1>", on_collapsed_click)
        collapsed.bind("<Button-1>", on_collapsed_click)
        entry.bind("<KeyRelease>", on_search)
        entry.bind("<Down>", on_arrow_down)
        entry.bind("<Up>", on_arrow_up)
        entry.bind("<Return>", on_enter)
        listbox.bind("<Motion>", on_hover_motion)
        listbox.bind("<Button-1>", on_click)
        listbox.bind("<Return>", on_enter)
        listbox.bind("<Up>", on_arrow_up)
        listbox.bind("<Down>", on_arrow_down)

        update_collapsed_label()
        collapsed.pack(fill="x")

        def set_locked(val):
            state["locked"] = val
            if val and state["expanded"]:
                set_expanded(False)

        state["container"] = container
        state["collapsed"] = collapsed
        state["entry"] = entry
        state["listbox"] = listbox
        state["set_locked"] = set_locked
        return state

    def _make_quality_button(self, parent, key, label):
        return tk.Button(
            parent, text=label, relief="flat", bd=0, padx=8, pady=6,
            cursor="hand2", highlightthickness=0, font=ui_font(10),
            takefocus=0, command=lambda: self._on_quality_change(key))

    def _highlight_quality(self):
        q = self.app.config.get("subtitles", {}).get("quality", "balanced")
        for key, btn in self._quality_btns.items():
            if key == q:
                btn.configure(bg=PALETTE["green"], fg=PALETTE["deep"],
                             activebackground=PALETTE["green"],
                             activeforeground=PALETTE["deep"])
            else:
                btn.configure(bg=PALETTE["panel"], fg=PALETTE["text"],
                             activebackground=PALETTE["panel"],
                             activeforeground=PALETTE["text"])

    def _refresh_device_status(self):
        if subtitles.gpu_available():
            self._device_status_lbl.configure(text="Your NVIDIA card is set up.")
        else:
            self._device_status_lbl.configure(text="Runs on the processor.")

    # ── Settings change handlers ──────────────────────────────────────────────────────
    def _on_model_change(self, _e=None):
        model_id = self._model_ids_by_name.get(self._model_var.get())
        if not model_id:
            return
        self.app.config.setdefault("subtitles", {})["model"] = model_id
        self.app.save_config()
        self._dispose_whisper_model()
        self._maybe_warm_model()

    def _on_language_change(self, code):
        self.app.config.setdefault("subtitles", {})["language"] = code
        self.app.save_config()

    def _on_second_language_change(self, code):
        if self._second_lang_var.get():
            self.app.config.setdefault("subtitles", {})["second_language"] = code
            self.app.save_config()

    def _on_game_terms_toggle(self):
        scfg = self.app.config.setdefault("subtitles", {})
        scfg["game_terms"] = bool(self._game_terms_var.get())
        if scfg["game_terms"]:
            self._game_terms_frame.pack(fill="x", pady=(4, 0), after=self._game_terms_chk)
        else:
            self._game_terms_frame.pack_forget()
        self.app.save_config()

    def _on_game_terms_text_change(self, _e=None):
        self.app.config.setdefault("subtitles", {})["game_terms_text"] = (
            self._game_terms_text_var.get().strip())
        self.app.save_config()

    def _on_second_lang_toggle(self):
        scfg = self.app.config.setdefault("subtitles", {})
        if self._second_lang_var.get():
            self._second_lang_frame.pack(fill="x", pady=(4, 0), after=self._second_lang_chk)
            scfg["second_language"] = self._lang2_picker["code"]
        else:
            self._second_lang_frame.pack_forget()
            scfg["second_language"] = ""
        self.app.save_config()

    def _on_second_weight_change(self, val):
        self.app.config.setdefault("subtitles", {})["second_language_weight"] = int(
            float(val))
        self.app.save_config()

    def _on_quality_change(self, key):
        self.app.config.setdefault("subtitles", {})["quality"] = key
        self.app.save_config()
        self._highlight_quality()

    def _on_device_change(self, _e=None):
        code = _DEVICE_CODES.get(self._device_var.get(), "auto")
        self.app.config.setdefault("subtitles", {})["device"] = code
        self.app.save_config()
        self._dispose_whisper_model()
        self._maybe_warm_model()

    def _on_auto_toggle(self):
        self.app.config.setdefault("subtitles", {})["auto_transcribe"] = bool(
            self._auto_var.get())
        self.app.save_config()
        self._refresh_state()

    # ── Style sub-tab (6.4b): the karaoke burn-in look + the render-pickup trigger ────
    def _build_style_panel(self, parent):
        scfg = self.app.config.setdefault("subtitles", {})
        style_cfg = scfg.setdefault("style", {})
        defaults = subtitles.SubtitleStyle()

        tk.Label(parent, text="PRESETS", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(anchor="w")
        preset_grid = tk.Frame(parent, bg=PALETTE["bg"])
        preset_grid.pack(fill="x", pady=(2, 8))
        preset_grid.grid_columnconfigure(0, weight=1)
        preset_grid.grid_columnconfigure(1, weight=1)
        self._preset_btns = {}
        for i, (name, _preset_style) in enumerate(subtitles.SUBTITLE_PRESETS):
            b = tk.Button(preset_grid, text=name, relief="flat", bd=0, padx=4, pady=6,
                         cursor="hand2", highlightthickness=0, font=ui_font(8),
                         takefocus=0, bg=PALETTE["card"], fg=PALETTE["text_dim"],
                         activebackground=PALETTE["card_hover"], activeforeground=PALETTE["text"],
                         command=lambda n=name: self._apply_named_style(n))
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=2)
            self._preset_btns[name] = b
        Tooltip(preset_grid, "Pick a ready-made look - you can still tweak it "
                            "afterward, and tweaking never changes the preset itself.")
        # Highlighted once both _preset_btns AND _slot_load_btns exist - see
        # _rebuild_slot_rows below, which is built later in this same pass and
        # calls _highlight_named_style_btns() itself once both are fresh.

        # 6.5 C4: position - drag directly in the preview to reposition (no
        # preset buttons; a live-test round dropped them as redundant with the
        # drag itself - TODO: the future onboarding/tutorial pass should call
        # out that the subtitle text is draggable in the preview, since there's
        # no button here hinting at it).

        tk.Label(parent, text="FONT", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(anchor="w")
        self._font_var = tk.StringVar(value=style_cfg.get("font", defaults.font))
        self._font_combo = ttk.Combobox(parent, textvariable=self._font_var, state="readonly",
                                        values=subtitles.available_fonts()["bundled"])
        self._font_combo.pack(fill="x", pady=(2, 8))
        self._font_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._on_style_change("font", self._font_var.get()))
        # The system-font scan is slow - never on the UI thread.
        threading.Thread(target=self._scan_fonts_worker, daemon=True).start()

        self._style_size_scale = self._build_config_slider(
            parent, "Text size", style_cfg.get("size", defaults.size), 24, 140, "px",
            lambda v: self._on_style_change("size", int(v)),
            "Subtitle text size, measured at the 1080x1920 reference resolution.")

        self._style_fill_swatch = self._style_color_row(
            parent, "Text color", "fill",
            "The subtitle text's own fill color, used for words that are not "
            "currently being spoken. With one word at a time you will not see "
            "it, because the only word on screen is always the current one.")

        self._word_gap_scale = self._build_config_slider(
            parent, "Space between words",
            style_cfg.get("word_gap", defaults.word_gap) * 100,
            80, 250, "%", lambda v: self._on_style_change("word_gap", v / 100.0),
            "How much air sits between words on screen. A plain typographic "
            "space is tight at subtitle sizes with a heavy font and a thick "
            "outline, so two words can read as one - the default is a little "
            "wider than that. Affects the burn-in and the preview, never the "
            "transcript.")

        tk.Label(parent, text="Drag the subtitles in the preview to move them.",
                 bg=PALETTE["bg"], fg=PALETTE["text_faint"], font=ui_font(8),
                 wraplength=220, justify="left", anchor="w").pack(fill="x", pady=(2, 0))

        # v1.1: the basics above stay visible; the finer controls fold away.
        sec = self._collapsible(parent, "Outline", "style_outline")
        self._outline_on_var = tk.BooleanVar(
            value=bool(style_cfg.get("outline_on", defaults.outline_on)))
        self._build_config_toggle(
            sec, "Outline on", self._outline_on_var,
            lambda: self._on_style_change("outline_on", bool(self._outline_on_var.get())),
            "A dark outline drawn behind the text for readability over busy "
            "footage. Drawn in one pass behind ALL letters, then the fill on top, "
            "so a thick outline never smears into a neighbouring word.")
        self._style_outline_swatch = self._style_color_row(
            sec, "Outline color", "outline_color", "The outline's color.")
        self._outline_width_scale = self._build_config_slider(
            sec, "Outline width", style_cfg.get("outline_width", defaults.outline_width),
            0, 30, "px", lambda v: self._on_style_change("outline_width", int(v)),
            "How thick the outline is, at the reference resolution.")

        sec = self._collapsible(parent, "Active-word highlight", "style_highlight")
        self._highlight_on_var = tk.BooleanVar(
            value=bool(style_cfg.get("highlight_on", defaults.highlight_on)))
        self._build_config_toggle(
            sec, "Highlight on", self._highlight_on_var,
            lambda: self._on_style_change("highlight_on", bool(self._highlight_on_var.get())),
            "A colored backdrop behind whichever word is CURRENTLY being spoken - "
            "never the whole line, just that one word. With one word at a time "
            "there is only ever the current word on screen, so everything is "
            "highlighted and nothing uses the plain text color.")
        self._style_highlight_swatch = self._style_color_row(
            sec, "Highlight color", "highlight_color",
            "The active-word backdrop's color. With one word at a time this is "
            "the color you see the whole time, since the single word on screen "
            "is always the current one - including while it fades in or out.")
        self._highlight_mode_btns = self._make_segmented(
            sec, _HIGHLIGHT_MODE_LABELS, self._on_highlight_mode_change)
        Tooltip(next(iter(self._highlight_mode_btns.values())),
               "Box: a rounded backdrop (default). Outline: no box - the active "
               "word's own outline recolors instead, a glowing-edge look. Image: "
               "your own picture sits behind the active word.")
        self._highlight_segmented(self._highlight_mode_btns,
                                  style_cfg.get("highlight_mode", defaults.highlight_mode))
        self._highlight_image_row = tk.Frame(sec, bg=PALETTE["bg"])
        self._highlight_image_lbl = tk.Label(
            self._highlight_image_row, text="", bg=PALETTE["bg"],
            fg=PALETTE["text_faint"], font=ui_font(8), anchor="w")
        self._highlight_image_lbl.pack(side="left", fill="x", expand=True)
        themed_button(self._highlight_image_row, "Choose image...",
                     self._on_pick_highlight_image, kind="neutral").pack(side="right")
        self._refresh_highlight_image_label()
        if style_cfg.get("highlight_mode", defaults.highlight_mode) == "image":
            self._highlight_image_row.pack(fill="x", pady=(4, 8))

        sec = self._collapsible(parent, "Reveal & animation", "style_reveal")
        self._section_label(
            sec, "REVEAL STYLE", "Reveal styles",
            "How words appear on screen. One word at a time shows just the "
            "current word. Build-up adds each word to a line as it is spoken, "
            "then clears when the line is full. Full line shows the whole line "
            "at once and highlights each word as it is said.")
        self._reveal_btns = self._make_segmented(
            sec, _REVEAL_LABELS, lambda k: self._on_style_change("reveal", k))
        Tooltip(next(iter(self._reveal_btns.values())),
               "Full line: the whole caption appears at once, current word "
               "highlighted. Rolling: words build up until the line is full, "
               "then resets for the next segment. One word: a single word at a "
               "time, replaced as the speaker goes.")
        self._highlight_segmented(self._reveal_btns, style_cfg.get("reveal", defaults.reveal))

        self._max_lines_scale = self._build_config_slider(
            sec, "Max lines (Rolling)", style_cfg.get("max_lines", defaults.max_lines),
            1, 3, "", lambda v: self._on_style_change("max_lines", int(v)),
            "How many lines Rolling fills with words before it resets and starts "
            "the next segment fresh (no carry-over).")

        self._section_label(
            sec, "ACTIVE-WORD MOTION", "Active-word motion",
            "An optional animation on the word currently being spoken, such as "
            "a small pop or a brief size bump, to draw the eye. Set it to none "
            "to keep words still.")
        self._motion_btns = self._make_segmented(
            sec, _MOTION_LABELS, lambda k: self._on_style_change("motion", k))
        self._highlight_segmented(self._motion_btns, style_cfg.get("motion", defaults.motion))

        self._section_label(
            sec, "TRANSITION", "Transition",
            "How one piece of subtitle hands over to the next. A hard cut swaps "
            "instantly; a fade dissolves between them. What counts as a piece "
            "depends on the reveal style: with one word at a time it is each "
            "word, so every word cuts or fades into the next. With build-up or "
            "full line it is the whole sentence, so the cut or fade happens "
            "between sentences while the words inside just highlight or build "
            "up on their own timings.")
        self._transition_btns = self._make_segmented(
            sec, _TRANSITION_LABELS, lambda k: self._on_style_change("transition", k))
        self._highlight_segmented(self._transition_btns,
                                  style_cfg.get("transition", defaults.transition))

        self._effect_intensity_scale = self._build_config_slider(
            sec, "Per-word effect intensity",
            style_cfg.get("effect_intensity", defaults.effect_intensity) * 100,
            50, 250, "%", lambda v: self._on_style_change("effect_intensity", v / 100.0),
            "How strong a tagged word's Shake or Pop reads (word selection bar, "
            "Timing view). 100% is the default strength; higher makes a tagged "
            "word's emphasis clearly stronger.")

        themed_button(parent, "Reset style to defaults", self._on_reset_style,
                     kind="neutral").pack(fill="x", pady=(10, 4))

        tk.Label(parent, text="MY SLOTS", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(anchor="w", pady=(8, 0))
        slot_name_row = tk.Frame(parent, bg=PALETTE["bg"])
        slot_name_row.pack(fill="x", pady=(2, 4))
        self._slot_name_var = tk.StringVar(value="")
        ttk.Entry(slot_name_row, textvariable=self._slot_name_var).pack(
            side="left", fill="x", expand=True)
        themed_button(slot_name_row, "+ Save", self._on_add_slot, kind="neutral").pack(
            side="left", padx=(6, 0))
        Tooltip(slot_name_row, "Name your current tweaked style and save it as a "
                              "reusable slot, like the editor's layout slots.")
        self._slots_frame = tk.Frame(parent, bg=PALETTE["bg"])
        self._slots_frame.pack(fill="x", pady=(0, 8))
        self._rebuild_slot_rows()


    def _collapsible(self, parent, title, key, default_open=False):
        """A click-to-open section for settings most people never touch (v1.1:
        the side panel shows the essentials, the rest folds away). Returns the
        body frame to build into. Open/closed is remembered per section."""
        opened = self.app.config.setdefault("subtitles", {}).setdefault("open_sections", {})
        wrap = tk.Frame(parent, bg=PALETTE["bg"])
        wrap.pack(fill="x", pady=(8, 0))
        head = tk.Frame(wrap, bg=PALETTE["card"], cursor="hand2")
        head.pack(fill="x")
        chev = tk.Label(head, bg=PALETTE["card"], fg=PALETTE["text_dim"],
                        font=ui_font(9), cursor="hand2")
        chev.pack(side="left", padx=(8, 4), pady=5)
        lbl = tk.Label(head, text=title, bg=PALETTE["card"], fg=PALETTE["text"],
                       font=ui_font(9, "bold"), cursor="hand2")
        lbl.pack(side="left", pady=5)
        body = tk.Frame(wrap, bg=PALETTE["bg"])

        def show(is_open):
            chev.configure(text="▾" if is_open else "▸")
            if is_open:
                body.pack(fill="x", pady=(6, 4))
            else:
                body.pack_forget()

        def toggle(_e=None):
            is_open = not opened.get(key, default_open)
            opened[key] = is_open
            self.app.save_config()
            show(is_open)

        for w in (head, chev, lbl):
            w.bind("<Button-1>", toggle)
        show(opened.get(key, default_open))
        self._sections[key] = (body, show)
        return body

    def _section_label(self, parent, text, help_title=None, help_text=None,
                       pady=(8, 0)):
        """A side-panel section heading, optionally with a `?` HelpDialog box
        (house convention #8). Returns the row frame, so callers can pack other
        widgets relative to it with `before=`."""
        row = tk.Frame(parent, bg=PALETTE["bg"])
        row.pack(fill="x", pady=pady)
        tk.Label(row, text=text, bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(side="left")
        if help_text:
            qbtn = tk.Label(row, text="?", bg=PALETTE["card"], fg=PALETTE["text_dim"],
                            font=ui_font(9, "bold"), cursor="hand2", padx=5)
            qbtn.pack(side="left", padx=(6, 0))
            title = help_title or text
            qbtn.bind("<Button-1>",
                     lambda _e: HelpDialog(self, title, body=help_text))
        return row

    def _make_segmented(self, parent, labels, on_pick):
        row = tk.Frame(parent, bg=PALETTE["bg"])
        row.pack(fill="x", pady=(2, 8))
        btns = {}
        for key, text in labels:
            b = tk.Button(row, text=text, relief="flat", bd=0, padx=6, pady=6,
                         cursor="hand2", highlightthickness=0, font=ui_font(9),
                         takefocus=0, command=lambda k=key: on_pick(k))
            b.pack(side="left", fill="x", expand=True, padx=(0, 2))
            btns[key] = b
        return btns

    @staticmethod
    def _highlight_segmented(btns, current):
        for key, btn in btns.items():
            active = key == current
            bg = PALETTE["green"] if active else PALETTE["panel"]
            fg = PALETTE["deep"] if active else PALETTE["text"]
            btn.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)

    def _style_color_row(self, parent, label, key, help_text):
        row = tk.Frame(parent, bg=PALETTE["bg"])
        row.pack(fill="x", pady=(4, 0))
        top = tk.Frame(row, bg=PALETTE["bg"])
        top.pack(fill="x")
        tk.Label(top, text=label, bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=ui_font(10)).pack(side="left")
        qbtn = tk.Label(top, text="?", bg=PALETTE["card"], fg=PALETTE["text_dim"],
                        font=ui_font(9, "bold"), cursor="hand2", padx=5)
        qbtn.pack(side="left", padx=(6, 0))
        qbtn.bind("<Button-1>", lambda _e: HelpDialog(self, label, body=help_text))
        style_cfg = self.app.config.setdefault("subtitles", {}).setdefault("style", {})
        current = style_cfg.get(key) or getattr(subtitles.SubtitleStyle(), key)
        swatch = tk.Button(top, width=3, bg=current, relief="flat", bd=0, cursor="hand2",
                           activebackground=current, highlightthickness=1,
                           highlightbackground=PALETTE["border"],
                           command=lambda: self._pick_style_color(key))
        swatch.pack(side="right")
        Tooltip(swatch, f"Pick the {label.lower()}.")
        return swatch

    def _pick_style_color(self, key):
        style_cfg = self.app.config.setdefault("subtitles", {}).setdefault("style", {})
        current = style_cfg.get(key) or getattr(subtitles.SubtitleStyle(), key)
        _rgb, hex_str = colorchooser.askcolor(initialcolor=current, parent=self)
        if not hex_str:
            return
        self._on_style_change(key, hex_str)
        swatch = {"fill": self._style_fill_swatch, "outline_color": self._style_outline_swatch,
                 "highlight_color": self._style_highlight_swatch}.get(key)
        if swatch is not None:
            swatch.configure(bg=hex_str, activebackground=hex_str)

    def _scan_fonts_worker(self):
        """The system-font scan (slow - reads every installed font's name table)
        runs here, off the UI thread; only the result touches Tk, via _marshal."""
        fonts = subtitles.available_fonts()
        self._marshal(self._fonts_scanned, fonts)

    def _fonts_scanned(self, fonts):
        self._font_combo.configure(
            values=fonts["bundled"] + fonts["system"] + fonts["imported"])

    _STYLE_SAVE_MS = 400
    _REGROUP_MS = 150

    def _schedule_style_save(self):
        """Debounced config save for style edits: a slider or a drag in the preview
        fires per mouse event, and rewriting config.json on every one is wasted
        work. The config dict itself is updated immediately (the hub saves it on
        close regardless)."""
        if getattr(self, "_style_save_job", None) is not None:
            self.after_cancel(self._style_save_job)
        self._style_save_job = self.after(self._STYLE_SAVE_MS, self._flush_style_save)

    def _flush_style_save(self):
        self._style_save_job = None
        self.app.save_config()

    def _regroup_pane(self):
        self._regroup_job = None
        self._render_timing_view()
        self._apply_cur_tag()
        self._apply_sel_tag()

    def _on_style_change(self, key, value):
        style_cfg = self.app.config.setdefault("subtitles", {}).setdefault("style", {})
        style_cfg[key] = value
        # A manual tweak means the style no longer exactly matches whatever named
        # preset/slot was last applied - clear the highlighted "active" one so
        # the picker honestly reads "custom" rather than a stale match.
        self.app.config["subtitles"]["active_slot"] = ""
        self._sync_clip_style()
        self._schedule_style_save()
        if key == "reveal":
            self._highlight_segmented(self._reveal_btns, value)
        elif key == "motion":
            self._highlight_segmented(self._motion_btns, value)
        elif key == "transition":
            self._highlight_segmented(self._transition_btns, value)
        elif key == "highlight_mode":
            self._highlight_segmented(self._highlight_mode_btns, value)
        self._highlight_named_style_btns()
        # 6.5c: these three decide how words are grouped into on-screen units,
        # and the transcript pane shows those same units - so it has to regroup
        # with them, not just the preview.
        if key in ("reveal", "max_lines", "max_width", "word_gap", "size", "font"):
            # Rebuilding the pane costs ~35 ms on a long transcript, so slider
            # steps share one regroup once the slider pauses.
            if getattr(self, "_regroup_job", None) is not None:
                self.after_cancel(self._regroup_job)
            self._regroup_job = self.after(self._REGROUP_MS, self._regroup_pane)
        self._render_preview_frame(self._playhead_t)

    # ── Drag-to-reposition the subtitle block (6.5 C4) ────────────────────────────────
    def _on_sub_drag_press(self, event):
        style_cfg = self.app.config.get("subtitles", {}).get("style", {})
        defaults = subtitles.SubtitleStyle()
        self._sub_drag_anchor0 = (style_cfg.get("anchor_x", defaults.anchor_x),
                                  style_cfg.get("anchor_y", defaults.anchor_y))
        self._sub_drag_x0 = event.x
        self._sub_drag_y0 = event.y

    def _on_sub_drag_motion(self, event):
        if self._sub_drag_anchor0 is None:
            return
        out_w, out_h = self._preview_out_wh
        if out_w <= 0 or out_h <= 0:
            return
        ax0, ay0 = self._sub_drag_anchor0
        new_ax = max(0.0, min(1.0, ax0 + (event.x - self._sub_drag_x0) / out_w))
        new_ay = max(0.0, min(1.0, ay0 + (event.y - self._sub_drag_y0) / out_h))
        style_cfg = self.app.config.setdefault("subtitles", {}).setdefault("style", {})
        style_cfg["anchor_x"] = new_ax
        style_cfg["anchor_y"] = new_ay
        self.app.config["subtitles"]["active_slot"] = ""
        self._sync_clip_style()
        self._schedule_style_save()
        self._render_preview_frame(self._playhead_t)

    def _on_sub_drag_release(self, _event):
        self._sub_drag_anchor0 = None

    def _on_reset_style(self):
        if not messagebox.askyesno(
                "Reset style to defaults?",
                "This resets every subtitle style value back to its default. "
                "Continue?", parent=self):
            return
        self.app.config.setdefault("subtitles", {})["style"] = subtitles.SubtitleStyle().to_dict()
        self.app.config["subtitles"]["active_slot"] = ""
        self._sync_clip_style()
        self.app.save_config()
        self._rebuild_style_panel()
        self._render_preview_frame(self._playhead_t)

    # ── Per-clip styles (v1.1) ────────────────────────────────────────────────────────
    # The Style tab always edits the open clip's style: once a clip has been set
    # up for subtitles, every style change is written to its per-clip block as
    # well as to the global "last used" style, so a render (here or straight from
    # the editor) never burns in an outdated look. Clips never set up just follow
    # the last used style. Opening a clip that remembers a different look asks
    # which one to keep.
    def _sync_clip_style(self):
        """Copy the current style onto the open clip's per-clip block, if it has
        one. Caller saves the config."""
        if not self._clip_key:
            return
        per_clip = self.app.config.setdefault("subtitles", {}).setdefault("per_clip", {})
        block = per_clip.get(self._clip_key)
        if block is not None:
            block["style"] = dict(self.app.config["subtitles"].get("style") or {})

    def _check_clip_style(self):
        """On opening a clip: if it remembers a style that differs from the
        current one, queue the keep-or-switch question (asked once the tab is on
        screen, never while the user is busy in another tab)."""
        self._style_question_key = None
        if not self._clip_key:
            return
        saved = subtitles.clip_style(self.app.config, self._clip_key)
        current = self.app.config.get("subtitles", {}).get("style") or {}
        if saved is not None and not subtitles.same_style(saved, current):
            self._style_question_key = self._clip_key
            self.after_idle(self._ask_clip_style)

    def _ask_clip_style(self):
        key = self._style_question_key
        if key is None or key != self._clip_key or self._closing:
            return
        try:
            if not self.winfo_viewable():
                return   # asked again from _on_visible when the tab is shown
        except tk.TclError:
            return
        self._style_question_key = None
        choice = ask_choice(
            self, "Use this clip's style?",
            "This clip was made with a different subtitle style than the one "
            "you are using now.\n\nKeep current: this clip switches to your "
            "current style.\nUse clip's style: your style switches back to the "
            "one this clip was made with.",
            [("clip", "Use clip's style"), ("keep", "Keep current")], "keep")
        scfg = self.app.config.setdefault("subtitles", {})
        if choice == "clip":
            saved = subtitles.clip_style(self.app.config, key)
            if saved is not None:
                scfg["style"] = subtitles.SubtitleStyle.from_dict(saved).to_dict()
                scfg["active_slot"] = ""
            self._rebuild_style_panel()
            self._render_timing_view()
        else:
            self._sync_clip_style()
        self.app.save_config()
        self._render_preview_frame(self._playhead_t)

    def _rebuild_style_panel(self):
        for w in self._style_panel.winfo_children():
            w.destroy()
        self._build_style_panel(self._style_panel)

    # ── Presets + user save-slots (6.4c) ──────────────────────────────────────────────
    def _apply_named_style(self, name):
        style = subtitles.find_named_style(self.app.config, name)
        if style is None:
            return
        self.app.config.setdefault("subtitles", {})["style"] = style.to_dict()
        self.app.config["subtitles"]["active_slot"] = name
        self._sync_clip_style()
        self.app.save_config()
        self._rebuild_style_panel()
        self._render_preview_frame(self._playhead_t)
        self._say(f"Applied '{name}'.", "green")

    def _highlight_named_style_btns(self):
        active = self.app.config.get("subtitles", {}).get("active_slot", "")
        all_btns = {}
        all_btns.update(getattr(self, "_preset_btns", {}))
        all_btns.update(getattr(self, "_slot_load_btns", {}))
        for name, btn in all_btns.items():
            is_active = bool(active) and name == active
            bg = PALETTE["green"] if is_active else PALETTE["card"]
            fg = PALETTE["deep"] if is_active else PALETTE["text_dim"]
            btn.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)

    def _rebuild_slot_rows(self):
        for w in self._slots_frame.winfo_children():
            w.destroy()
        self._slot_load_btns = {}
        slots = subtitles.list_slots(self.app.config)
        if not slots:
            tk.Label(self._slots_frame, text="No saved slots yet - tweak a style "
                                             "and save it above.",
                     bg=PALETTE["bg"], fg=PALETTE["text_faint"], font=ui_font(8),
                     wraplength=220, justify="left", anchor="w").pack(fill="x")
            return
        for i, entry in enumerate(slots):
            name = entry.get("name", f"Slot {i + 1}")
            row = tk.Frame(self._slots_frame, bg=PALETTE["bg"])
            row.pack(fill="x", pady=2)
            load_btn = tk.Button(
                row, text=name, command=lambda i=i: self._on_load_slot(i),
                bg=PALETTE["card"], fg=PALETTE["text_dim"],
                activebackground=PALETTE["card_hover"], activeforeground=PALETTE["text"],
                relief="flat", bd=0, anchor="w", padx=8, pady=4, cursor="hand2",
                font=ui_font(9))
            load_btn.pack(side="left", fill="x", expand=True)
            Tooltip(load_btn, "Load this saved style.")
            del_btn = tk.Button(
                row, text="Delete", command=lambda i=i: self._on_delete_slot(i),
                bg=PALETTE["panel"], fg=PALETTE["text_dim"],
                activebackground=PALETTE["card_hover"], activeforeground=PALETTE["red_soft"],
                relief="flat", bd=0, padx=8, pady=4, cursor="hand2", font=ui_font(8))
            del_btn.pack(side="left", padx=(4, 0))
            self._slot_load_btns[name] = load_btn
        self._highlight_named_style_btns()

    def _on_add_slot(self):
        name = self._slot_name_var.get().strip()
        style_cfg = self.app.config.setdefault("subtitles", {}).setdefault("style", {})
        subtitles.save_slot(self.app.config, name, style_cfg)
        saved_name = subtitles.list_slots(self.app.config)[-1]["name"]
        self._slot_name_var.set("")
        self.app.config["subtitles"]["active_slot"] = saved_name
        self.app.save_config()
        self._rebuild_slot_rows()
        self._highlight_named_style_btns()
        self._say(f"Saved style slot '{saved_name}'.", "green")

    def _on_load_slot(self, i):
        slots = subtitles.list_slots(self.app.config)
        if i >= len(slots):
            return
        self._apply_named_style(slots[i].get("name", f"Slot {i + 1}"))

    def _on_delete_slot(self, i):
        slots = subtitles.list_slots(self.app.config)
        if i >= len(slots):
            return
        name = slots[i].get("name", "?")
        if not messagebox.askyesno("Delete slot?",
                                   f"Delete the saved style '{name}'?", parent=self):
            return
        subtitles.delete_slot(self.app.config, i)
        if self.app.config.get("subtitles", {}).get("active_slot") == name:
            self.app.config["subtitles"]["active_slot"] = ""
        self.app.save_config()
        self._rebuild_slot_rows()
        self._say(f"Deleted slot '{name}'.", "green")

    # ── Highlight modes (6.4c: box | outline | image) ─────────────────────────────────
    def _on_highlight_mode_change(self, mode):
        self._on_style_change("highlight_mode", mode)
        if mode == "image":
            self._highlight_image_row.pack(fill="x", pady=(4, 8))
        else:
            self._highlight_image_row.pack_forget()

    def _refresh_highlight_image_label(self):
        style_cfg = self.app.config.get("subtitles", {}).get("style", {})
        path = style_cfg.get("highlight_image", "")
        self._highlight_image_lbl.configure(
            text=os.path.basename(path) if path else "No image chosen")

    def _on_pick_highlight_image(self):
        path = filedialog.askopenfilename(
            parent=self, title="Choose a highlight image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp"), ("All files", "*.*")])
        if not path:
            return
        self._on_style_change("highlight_image", path)
        self._refresh_highlight_image_label()

    # ── SRT sidecar (6.4c) ─────────────────────────────────────────────────────────────
    def _on_srt_toggle(self):
        self.app.config.setdefault("subtitles", {})["srt_export"] = bool(self._srt_var.get())
        self.app.save_config()

    # ── Render pickup (6.4b): enable/apply for this clip + the editor bus jump ───────
    def _refresh_subtitle_enable_label(self):
        if not hasattr(self, "_sub_enable_lbl"):
            return
        per_clip = self.app.config.get("subtitles", {}).get("per_clip", {})
        block = per_clip.get(self._clip_key) if self._clip_key else None
        enabled = bool(block and block.get("enabled"))
        if enabled:
            self._sub_enable_lbl.configure(text="Subtitles: ON for this clip",
                                           fg=PALETTE["green"])
            self._disable_sub_btn.pack(fill="x", pady=(6, 0))
        else:
            self._sub_enable_lbl.configure(text="Subtitles: OFF for this clip",
                                           fg=PALETTE["text_mute"])
            self._disable_sub_btn.pack_forget()

    def _on_apply_and_render(self):
        """Enable subtitles for this clip with the current style, then ask the
        editor to run its existing Render. Later style edits keep reaching this
        clip on their own (_sync_clip_style), so a plain Render from the editor
        also uses the current look. The passive pickup in the editor's render
        loop is the guaranteed floor - this is purely the convenience of not
        having to switch tabs and press Render manually."""
        if self._subject is None:
            self._say("Load a clip first.")
            return
        self._commit_now()
        if self._transcript is None or not any(ln.words for ln in self._transcript.lines):
            messagebox.showinfo(
                "No transcript yet",
                "Transcribe this clip first - there's nothing to burn in yet.",
                parent=self)
            return
        style_cfg = self.app.config.setdefault("subtitles", {}).setdefault("style", {})
        per_clip = self.app.config.setdefault("subtitles", {}).setdefault("per_clip", {})
        per_clip[self._clip_key] = {"enabled": True, "style": dict(style_cfg)}
        self.app.save_config()
        self._refresh_subtitle_enable_label()
        self.bus.publish("render_request", self._subject)
        self.bus.request_handoff("render_request", "editor")
        self._say("Subtitles enabled - rendering in the Clip Editor.", "green")

    def _on_disable_subtitles(self):
        per_clip = self.app.config.setdefault("subtitles", {}).setdefault("per_clip", {})
        block = per_clip.get(self._clip_key)
        if block is not None:
            block["enabled"] = False
            self.app.save_config()
        self._refresh_subtitle_enable_label()

    # ── Config sub-tab (6.3b): engine tuning in plain language + an Expert drawer ────
    def _build_config_panel(self, parent):
        scfg = self.app.config.setdefault("subtitles", {})

        self._vad_var = tk.BooleanVar(value=bool(scfg.get("vad", False)))
        self._vad_chk = self._build_config_toggle(
            parent, "Filter noise & silence", self._vad_var, self._on_vad_toggle,
            "Skips music and quiet stretches so the model invents fewer words. "
            "Good for noisy gameplay clips. Rare downside: can clip very quiet "
            "speech. Takes effect on the NEXT transcription.")

        self._halluc_var = tk.BooleanVar(value=bool(scfg.get("reduce_hallucinations", False)))
        self._halluc_chk = self._build_config_toggle(
            parent, "Reduce made-up words in quiet parts", self._halluc_var,
            self._on_halluc_toggle,
            "Drops phantom phrases the model sometimes invents over near-silence "
            "(e.g. a repeated word during a quiet stretch). Takes effect on the "
            "NEXT transcription.")

        self._snap_var = tk.BooleanVar(value=bool(scfg.get("snap_word_starts", False)))
        self._snap_chk = self._build_config_toggle(
            parent, "Snap word starts to audio", self._snap_var,
            self._on_snap_toggle,
            "Nudges each word's start a touch earlier or later to match where "
            "the audio actually gets loud nearby - fixes a word that starts a "
            "beat early (on a breath) or late (after a quiet gap). Small, "
            "bounded correction; never moves past the previous or next word. "
            "Off by default - try it and compare, it isn't guaranteed to help "
            "every clip. Takes effect on the NEXT transcription.")

        self._confidence_scale = self._build_config_slider(
            parent, "Flag shaky words below", scfg.get("flag_confidence", 0.6) * 100,
            40, 95, "%", self._on_confidence_change,
            "Words the model is unsure about get a dotted underline in the "
            "transcript so you can check them - nothing is ever deleted. "
            "Example: a word at 55% confidence flags at a 60% cutoff. "
            "Re-highlights the current transcript right away, no re-transcribe "
            "needed.")

        self._offset_scale = self._build_config_slider(
            parent, "Karaoke timing offset", scfg.get("timing_offset_ms", 0),
            -500, 500, " ms", self._on_offset_change,
            "Nudges every word a touch earlier (negative) or later (positive) if "
            "subtitles feel slightly ahead of or behind the audio. Applies to the "
            "preview right away and to the final render later - fully "
            "reversible, never re-transcribes.")

        self._expert_frame = tk.Frame(parent, bg=PALETTE["card"],
                                      highlightbackground=PALETTE["border"],
                                      highlightthickness=1)
        self._expert_frame.pack(fill="x", pady=(10, 6))
        head = tk.Frame(self._expert_frame, bg=PALETTE["card"], cursor="hand2")
        head.pack(fill="x", padx=10, pady=8)
        tk.Label(head, text="Expert settings", bg=PALETTE["card"],
                 fg=PALETTE["text_dim"], font=ui_font(10)).pack(side="left")
        self._expert_chev = tk.Label(head, text="▸", bg=PALETTE["card"],
                                     fg=PALETTE["text_mute"], font=ui_font(10),
                                     cursor="hand2")
        self._expert_chev.pack(side="right")
        head.bind("<Button-1>", lambda _e: self._toggle_expert_drawer())
        self._expert_chev.bind("<Button-1>", lambda _e: self._toggle_expert_drawer())
        tk.Label(self._expert_frame, text="Search effort (beam), no-speech / "
                 "log-prob / compression thresholds, repeat-loop guard, "
                 "temperature. Safe defaults - only touch these if you know "
                 "Whisper.", bg=PALETTE["card"], fg=PALETTE["text_faint"],
                 font=ui_font(8), wraplength=210, justify="left",
                 anchor="w").pack(fill="x", padx=10, pady=(0, 8))
        self._expert_body = tk.Frame(self._expert_frame, bg=PALETTE["card"])
        self._build_expert_body(self._expert_body, scfg)
        if self._expert_expanded:
            self._expert_body.pack(fill="x", padx=10, pady=(0, 10))

        themed_button(parent, "Reset all to defaults", self._on_reset_config,
                     kind="neutral").pack(fill="x", pady=(4, 0))

    def _build_config_toggle(self, parent, label, var, command, help_text):
        row = tk.Frame(parent, bg=PALETTE["bg"])
        row.pack(fill="x", pady=(0, 8))
        top = tk.Frame(row, bg=PALETTE["bg"])
        top.pack(fill="x")
        tk.Label(top, text=label, bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=ui_font(10), wraplength=170, justify="left",
                 anchor="w").pack(side="left")
        qbtn = tk.Label(top, text="?", bg=PALETTE["card"], fg=PALETTE["text_dim"],
                        font=ui_font(9, "bold"), cursor="hand2", padx=5)
        qbtn.pack(side="left", padx=(6, 0))
        qbtn.bind("<Button-1>", lambda _e: HelpDialog(self, label, body=help_text))
        chk = ttk.Checkbutton(top, variable=var, command=command)
        chk.pack(side="right")
        return chk

    def _build_config_slider(self, parent, label, initial_value, lo, hi, unit, command,
                             help_text):
        row = tk.Frame(parent, bg=PALETTE["bg"])
        row.pack(fill="x", pady=(4, 8))
        top = tk.Frame(row, bg=PALETTE["bg"])
        top.pack(fill="x")
        tk.Label(top, text=label, bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=ui_font(10)).pack(side="left")
        qbtn = tk.Label(top, text="?", bg=PALETTE["card"], fg=PALETTE["text_dim"],
                        font=ui_font(9, "bold"), cursor="hand2", padx=5)
        qbtn.pack(side="left", padx=(6, 0))
        qbtn.bind("<Button-1>", lambda _e: HelpDialog(self, label, body=help_text))
        val_lbl = tk.Label(top, text=f"{int(round(initial_value))}{unit}",
                           bg=PALETTE["bg"], fg=PALETTE["text_dim"], font=mono_font(9))
        val_lbl.pack(side="right")
        scale = tk.Scale(
            row, from_=lo, to=hi, orient="horizontal", resolution=1, showvalue=0,
            bg=PALETTE["panel"], troughcolor=PALETTE["deep"],
            activebackground=PALETTE["blue"], fg=PALETTE["text_dim"],
            highlightthickness=0, bd=0, sliderrelief="flat")
        scale.set(initial_value)
        scale.pack(fill="x", pady=(4, 0))
        bind_scale_jump(scale)

        def on_change(val):
            v = float(val)
            val_lbl.configure(text=f"{int(round(v))}{unit}")
            command(v)

        scale.configure(command=on_change)
        return scale

    def _build_expert_body(self, parent, scfg):
        expert = scfg.setdefault("expert", {})
        self._expert_vars = {}

        def add_spin(label, key, lo, hi, step, help_text, is_float=True):
            row = tk.Frame(parent, bg=PALETTE["card"])
            row.pack(fill="x", pady=(6, 0))
            top = tk.Frame(row, bg=PALETTE["card"])
            top.pack(fill="x")
            tk.Label(top, text=label, bg=PALETTE["card"], fg=PALETTE["text_dim"],
                     font=ui_font(9), wraplength=140, justify="left",
                     anchor="w").pack(side="left")
            qbtn = tk.Label(top, text="?", bg=PALETTE["panel"], fg=PALETTE["text_dim"],
                            font=ui_font(8, "bold"), cursor="hand2", padx=4)
            qbtn.pack(side="left", padx=(4, 0))
            qbtn.bind("<Button-1>", lambda _e: HelpDialog(self, label, body=help_text))
            init = expert.get(key, 0)
            var = (tk.DoubleVar(value=float(init)) if is_float
                   else tk.IntVar(value=int(init)))

            def commit(_e=None, k=key, v=var):
                self._on_expert_change(k, v.get())

            spin = tk.Spinbox(
                top, from_=lo, to=hi, increment=step, textvariable=var, width=7,
                bg=PALETTE["deep"], fg=PALETTE["text"], buttonbackground=PALETTE["panel"],
                relief="flat", font=mono_font(9), command=commit)
            spin.pack(side="right")
            spin.bind("<Return>", commit)
            spin.bind("<FocusOut>", commit)
            self._expert_vars[key] = var

        add_spin("Beam override (0 = use Quality)", "beam_override", 0, 10, 1,
                 "Force a specific search width instead of following the Quality "
                 "control. 0 uses Quality's own mapping (Fast=1, Balanced=5, "
                 "Best=8). Higher is slower and slightly more accurate.",
                 is_float=False)
        add_spin("No-speech threshold", "no_speech_threshold", 0.0, 1.0, 0.05,
                 "How confident the model must be that a stretch has NO speech "
                 "before it skips it. Safe default 0.6 - lower it if speech is "
                 "being skipped, raise it if silence is being transcribed.")
        add_spin("Log-prob threshold", "log_prob_threshold", -5.0, 0.0, 0.1,
                 "Rejects a segment if its average word confidence falls below "
                 "this. Safe default -1.0 - more negative accepts shakier "
                 "segments.")
        add_spin("Compression ratio threshold", "compression_ratio_threshold",
                 1.0, 5.0, 0.1,
                 "Flags a segment as likely gibberish/repetition if its text "
                 "compresses too easily. Safe default 2.4.")

        cop_row = tk.Frame(parent, bg=PALETTE["card"])
        cop_row.pack(fill="x", pady=(6, 0))
        tk.Label(cop_row, text="Repeat-loop guard", bg=PALETTE["card"],
                 fg=PALETTE["text_dim"], font=ui_font(9)).pack(side="left")
        qbtn = tk.Label(cop_row, text="?", bg=PALETTE["panel"], fg=PALETTE["text_dim"],
                        font=ui_font(8, "bold"), cursor="hand2", padx=4)
        qbtn.pack(side="left", padx=(4, 0))
        qbtn.bind("<Button-1>", lambda _e: HelpDialog(
            self, "Repeat-loop guard",
            body="Lets the model use its own previous line as context for the "
                 "next one. Safe default OFF - turning it on can read more "
                 "naturally across sentences, but if one segment ever comes out "
                 "wrong the next one tends to repeat or compound that mistake "
                 "instead of recovering on its own."))
        cop_var = tk.BooleanVar(value=bool(expert.get("condition_on_previous_text", False)))
        ttk.Checkbutton(
            cop_row, variable=cop_var,
            command=lambda: self._on_expert_change(
                "condition_on_previous_text", bool(cop_var.get()))).pack(side="right")
        self._expert_vars["condition_on_previous_text"] = cop_var

        add_spin("Temperature (0 = default ladder)", "temperature", 0.0, 1.0, 0.1,
                 "Pins the model's sampling temperature to one fixed value "
                 "instead of its own fallback ladder. Safe default 0, which "
                 "keeps the ladder - it starts at greedy (fully repeatable) for "
                 "every segment and only escalates on a segment that already "
                 "failed a quality check, so it can retry its way out of a "
                 "stuck repeat instead of looping forever. Only pin this if you "
                 "know what it does.")

    def _toggle_expert_drawer(self):
        self._expert_expanded = not self._expert_expanded
        if self._expert_expanded:
            self._expert_body.pack(fill="x", padx=10, pady=(0, 10))
            self._expert_chev.configure(text="▾")
        else:
            self._expert_body.pack_forget()
            self._expert_chev.configure(text="▸")

    def _on_vad_toggle(self):
        self.app.config.setdefault("subtitles", {})["vad"] = bool(self._vad_var.get())
        self.app.save_config()

    def _on_halluc_toggle(self):
        self.app.config.setdefault("subtitles", {})["reduce_hallucinations"] = bool(
            self._halluc_var.get())
        self.app.save_config()

    def _on_snap_toggle(self):
        self.app.config.setdefault("subtitles", {})["snap_word_starts"] = bool(
            self._snap_var.get())
        self.app.save_config()

    def _on_confidence_change(self, v):
        self.app.config.setdefault("subtitles", {})["flag_confidence"] = round(v / 100.0, 3)
        self.app.save_config()
        self._rebuild_flagged_queue()
        self._render_editor()

    def _on_offset_change(self, v):
        self.app.config.setdefault("subtitles", {})["timing_offset_ms"] = int(v)
        self.app.save_config()
        self._highlight_current_word(self._playhead_t)
        self._render_preview_frame(self._playhead_t)

    def _on_expert_change(self, key, value):
        expert = self.app.config.setdefault("subtitles", {}).setdefault("expert", {})
        expert[key] = value
        self.app.save_config()

    def _on_reset_config(self):
        if not messagebox.askyesno(
                "Reset all to defaults?",
                "This resets every Config value (curated and Expert) back to its "
                "default. Continue?", parent=self):
            return
        defaults = config_mod.DEFAULTS["subtitles"]
        scfg = self.app.config.setdefault("subtitles", {})
        scfg["vad"] = defaults["vad"]
        scfg["reduce_hallucinations"] = defaults["reduce_hallucinations"]
        scfg["snap_word_starts"] = defaults["snap_word_starts"]
        scfg["flag_confidence"] = defaults["flag_confidence"]
        scfg["timing_offset_ms"] = defaults["timing_offset_ms"]
        scfg["expert"] = dict(defaults["expert"])
        self.app.save_config()
        self._rebuild_config_panel()
        self._rebuild_flagged_queue()
        self._render_editor()

    def _rebuild_config_panel(self):
        for w in self._config_panel.winfo_children():
            w.destroy()
        self._expert_expanded = False
        self._build_config_panel(self._config_panel)

    # ── Centre column: toolbar + selection bar + interactive transcript + timeline ──
    def _build_center(self, center):
        # 6.5 live-test round: dropped the filename label - no need to see it.
        self._clip_row = row = tk.Frame(
            center, bg=PALETTE["card"], highlightbackground=PALETTE["border"],
            highlightthickness=1, bd=0)
        row.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(row, bg=PALETTE["card"])
        inner.pack(fill="x", padx=12, pady=8)
        self._clip_len_lbl = tk.Label(inner, text="", bg=PALETTE["card"],
                                      fg=PALETTE["text_mute"], font=ui_font(9))
        self._clip_len_lbl.pack(side="right")

        self._stale_banner = tk.Frame(
            center, bg="#3a2f14", highlightbackground=PALETTE["yellow"],
            highlightthickness=1, bd=0)
        stale_inner = tk.Frame(self._stale_banner, bg="#3a2f14")
        stale_inner.pack(fill="x", padx=10, pady=8)
        tk.Label(stale_inner, text="Cuts changed. This transcript no longer lines up.",
                 bg="#3a2f14", fg="#ffe6a8", font=ui_font(9), anchor="w",
                 wraplength=420, justify="left").pack(side="left", fill="x", expand=True)
        themed_button(stale_inner, "Re-transcribe", self._on_transcribe,
                     kind="neutral").pack(side="right")

        self._timing_panel = tk.Frame(center, bg=PALETTE["bg"])
        self._build_timing_panel(self._timing_panel)
        self._timing_panel.pack(fill="both", expand=True)

    def _build_timing_panel(self, center):
        """One toolbar row (6.5c). Insert word and Delete are gone: typing in a
        gap creates a word and carves it a timeslot, and deleting a word's
        letters deletes the word, so the buttons only duplicated what the
        transcript already does. The Word/Start/End fields went with them -
        timings are dragged on the timeline against the waveform. What is left
        is what has no other home: undo/redo, the per-word effects, and adding a
        non-speech entry."""
        toolbar = tk.Frame(center, bg=PALETTE["deep"],
                           highlightbackground=PALETTE["border"], highlightthickness=1)
        toolbar.pack(fill="x", pady=(0, 8))
        row = tk.Frame(toolbar, bg=PALETTE["deep"])
        row.pack(fill="x", padx=8, pady=6)

        def tb(parent, text, cmd):
            b = tk.Button(parent, text=text, command=cmd, relief="flat", bd=0,
                         padx=8, pady=5, cursor="hand2", bg=PALETTE["card"],
                         fg=PALETTE["text_dim"], activebackground=PALETTE["card_hover"],
                         activeforeground=PALETTE["text"], highlightthickness=0,
                         takefocus=0, font=ui_font(9))
            b.pack(side="left", padx=(0, 4))
            return b

        # Ctrl+Z / Ctrl+Y (and Ctrl+Shift+Z), like any text editor. A bare-letter
        # hotkey was tried and scrapped in 6.5 - it fought normal typing.
        Tooltip(tb(row, "Undo", self._do_undo), "Undo (Ctrl+Z)")
        Tooltip(tb(row, "Redo", self._do_redo), "Redo (Ctrl+Y or Ctrl+Shift+Z)")
        tk.Frame(row, bg=PALETTE["border"], width=1).pack(side="left", fill="y",
                                                          padx=6)

        tk.Label(row, text="Effects", bg=PALETTE["deep"], fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(side="left", padx=(0, 6))
        self._effect_btns = {}
        for key, label in _WORD_EFFECT_LABELS:
            self._effect_btns[key] = tb(row, label,
                                       lambda k=key: self._toggle_selected_effect(k))
        Tooltip(self._effect_btns["shake"],
               "Shake: a small jitter the whole time it is shown. Pop: a brief "
               "size bump the moment it appears. Applies to the selected word or "
               "non-speech entry; strength is the Style tab's intensity slider.")
        tk.Frame(row, bg=PALETTE["border"], width=1).pack(side="left", fill="y",
                                                          padx=6)

        add_btn = tb(row, "+ Non-speech", self._do_add_nonspeech)
        Tooltip(add_btn, "Adds an entry for something that is not speech - "
                        "[*laughter*], [*explosion*] - with its own place on the "
                        "timeline. It shakes a little by default so it reads as "
                        "something happening.")

        self._review_btn = tk.Button(
            row, text="Review flagged (0)", command=self._on_review_clicked,
            relief="flat", bd=0, padx=10, pady=5, cursor="hand2",
            bg=PALETTE["yellow"], fg=PALETTE["deep"], activebackground=PALETTE["yellow"],
            activeforeground=PALETTE["deep"], highlightthickness=0, takefocus=0,
            font=ui_font(9, "bold"), state="disabled")
        self._review_btn.pack(side="right")

        # Context strip: only what the CURRENT selection actually needs - a
        # shaky word's keep/delete, a suggested gap's confirm/dismiss, a
        # non-speech entry's label. Empty the rest of the time.
        self._sel_bar = tk.Frame(row, bg=PALETTE["deep"])
        self._sel_bar.pack(side="right", padx=(8, 8))
        self._render_selection_bar()

        text_wrap = tk.Frame(center, bg=PALETTE["deep"],
                             highlightbackground=PALETTE["border"], highlightthickness=1,
                             bd=0)
        # NOT packed yet: the timeline below reserves its space FIRST (see the
        # side="bottom" packs at the end of this method). Packing an
        # expand=True widget before them lets it claim every spare pixel, and
        # Tk then simply DROPS the slaves that no longer fit - at 1920x1080 the
        # whole timeline strip silently vanished (6.5c).
        self._transcript_text = tk.Text(
            text_wrap, bg=PALETTE["deep"], fg=PALETTE["text_dim"], font=mono_font(11),
            wrap="word", relief="flat", bd=0, padx=14, pady=12,
            # Tk's default caret is black, which on this pane's near-black
            # background is invisible - clicking anywhere but a word looked like
            # the cursor had not moved at all (6.5c). It had; it just could not
            # be seen.
            insertbackground=PALETTE["text"], insertwidth=2,
            # 6.5c: the gap between rows used to be a literal blank line, which
            # was editable, clickable, and belonged to nothing. It is spacing
            # now - the widget draws the air, so there is no empty row to land
            # a cursor in.
            spacing1=7, spacing3=7)
        self._transcript_text.pack(fill="both", expand=True)
        self._transcript_text.tag_configure("tc", foreground=PALETTE["blue"],
                                            font=mono_font(9))
        self._transcript_text.tag_configure("empty", foreground=PALETTE["text_mute"],
                                            font=ui_font(11))
        self._transcript_text.tag_configure("shaky", foreground=PALETTE["yellow"],
                                            underline=1)
        # 6.5c: a word the user added by hand, not one the model heard.
        self._transcript_text.tag_configure("added", foreground=PALETTE["green"])
        self._transcript_text.tag_configure("sel_word", background="#2b4a63",
                                            foreground=PALETTE["text"])
        self._transcript_text.tag_configure("cur", background=PALETTE["green"],
                                            foreground=PALETTE["deep"])
        self._transcript_text.tag_configure("event_text", foreground=PALETTE["purple"],
                                            font=ui_font(11, "italic"))
        self._transcript_text.tag_configure("gap_text", foreground=PALETTE["text_mute"],
                                            font=ui_font(11, "italic"))
        self._transcript_text.tag_configure("event_kind", foreground=PALETTE["purple"],
                                            font=ui_font(8))
        # "sel_word" must win over "shaky"/"cur" so a selected flagged word still
        # reads as selected. Tk resolves overlapping tags by RAISE order - later
        # tag_raise wins, so raise selection state last whenever both may apply.
        self._transcript_text.tag_raise("sel_word")
        self._transcript_text.tag_raise("cur")
        self._bind_editable_text(self._transcript_text)

        tl_row = tk.Frame(center, bg=PALETTE["bg"])
        tl_row.pack(side="bottom", fill="x", pady=(8, 2))
        self._zoom_btn = tk.Button(
            tl_row, text="Zoom: on", command=self._toggle_zoom, relief="flat", bd=0,
            padx=8, pady=3, cursor="hand2", bg=PALETTE["card"], fg=PALETTE["text_dim"],
            activebackground=PALETTE["card_hover"], activeforeground=PALETTE["text"],
            highlightthickness=0, takefocus=0, font=ui_font(8))
        self._zoom_btn.pack(side="left")
        self._tl_note_lbl = tk.Label(
            tl_row, text="", bg=PALETTE["bg"], fg=PALETTE["text_faint"],
            font=ui_font(8))
        self._tl_note_lbl.pack(side="left", padx=(8, 0))
        self._sync_zoom_ui()

        self._timeline_canvas = tk.Canvas(
            center, height=_TL_CANVAS_H, bg=PALETTE["deep"], highlightthickness=1,
            highlightbackground=PALETTE["border"], bd=0)
        self._timeline_canvas.pack(side="bottom", fill="x")
        self._timeline_canvas.bind("<ButtonPress-1>", self._tl_on_press)
        self._timeline_canvas.bind("<B1-Motion>", self._tl_on_drag)
        self._timeline_canvas.bind("<ButtonRelease-1>", self._tl_on_release)
        self._timeline_canvas.bind("<Motion>", self._tl_on_hover)
        self._timeline_canvas.bind("<Button-3>", self._tl_on_right_click)

        # Last, so it fills whatever the toolbar and the timeline leave behind.
        text_wrap.pack(fill="both", expand=True)

    def _build_right(self, right):
        tk.Label(right, text="PREVIEW (9:16)", bg=PALETTE["bg"],
                 fg=PALETTE["text_mute"], font=ui_font(9)).pack(anchor="w")
        # 6.4b: fills whatever the draggable sash gives it (no fixed box) - drag
        # the divider to grow the preview toward the pane's full height, borderless
        # aside from the unavoidable 9:16 letterbox against a non-9:16 pane shape.
        self._preview_frame = tk.Frame(
            right, bg="#000000",
            highlightbackground=PALETTE["border"], highlightthickness=1, bd=0)
        self._preview_frame.pack(fill="both", expand=True, pady=(8, 0))
        self._preview_img_lbl = tk.Label(self._preview_frame, bg="#000000", bd=0,
                                         highlightthickness=0, cursor="fleur")
        self._preview_img_lbl.place(relx=0.5, rely=0.5, anchor="center")
        # 6.5 C4: drag anywhere in the preview to reposition the subtitle block -
        # it's the only draggable thing shown here (no crop boxes/highlights like
        # the editor's preview has), so no hit-testing needed to disambiguate.
        self._preview_img_lbl.bind("<ButtonPress-1>", self._on_sub_drag_press)
        self._preview_img_lbl.bind("<B1-Motion>", self._on_sub_drag_motion)
        self._preview_img_lbl.bind("<ButtonRelease-1>", self._on_sub_drag_release)
        Tooltip(self._preview_img_lbl, "Drag to reposition the subtitles.")
        self._preview_placeholder_lbl = tk.Label(
            self._preview_frame, text="Load a clip to preview", bg="#000000",
            fg=PALETTE["text_faint"], font=ui_font(9), justify="center")
        self._preview_placeholder_lbl.place(relx=0.5, rely=0.5, anchor="center")
        self._preview_frame.bind("<Configure>", self._on_preview_frame_configure)

        pvbar = tk.Frame(right, bg=PALETTE["bg"])
        pvbar.pack(fill="x", pady=(8, 0))
        self._play_btn = tk.Label(
            pvbar, text="▶", bg=PALETTE["card"], fg=PALETTE["text"], font=ui_font(11),
            width=3, cursor="hand2", highlightbackground=PALETTE["border"],
            highlightthickness=1)
        self._play_btn.pack(side="left")
        self._play_btn.bind("<Button-1>", lambda _e: self._toggle_play())
        self._pos_lbl = tk.Label(pvbar, text="0:00 / 0:00", bg=PALETTE["bg"],
                                 fg=PALETTE["text_mute"], font=ui_font(9))
        self._pos_lbl.pack(side="left", padx=(8, 0))

        stay_row = tk.Frame(right, bg=PALETTE["bg"])
        stay_row.pack(fill="x", pady=(4, 0))
        self._stay_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(stay_row, text="Stay at word", variable=self._stay_var,
                        command=self._on_stay_toggle).pack(side="left")
        Tooltip(stay_row, "The timeline always scrolls along with playback. Turn "
                          "this on and stopping returns the playhead to the word "
                          "you selected, so you can replay the same spot over and "
                          "over without losing your place.")

        # (The old "click any word to jump" hint lived here; the floating
        # replay-tour button sat on top of it. The tour now explains it.)

    # ── Current-clip resolution + state (7.3/7.4) ─────────────────────────────────────
    def _pick_source(self):
        """Prefer raw_clip over edited_clip, unlike exporter._pick_source (which
        wants whichever is most recently published). The editor's own subtitles
        render-pickup (_snapshot_subtitles_for_render) always keys off its loaded
        SOURCE clip - stable across repeat renders of the same clip - never off
        the render OUTPUT, which gets a brand-new timestamped path every render.
        Bus-recency picking would make Subtitle Studio switch its clip_key to that
        new output path after every render, orphaning the just-saved transcript
        (6.5 P0 fix - confirmed root cause of "transcript is empty after Apply &
        Render -> export -> return"). raw_clip is published once per import/load
        and stays put across any number of renders, so anchoring on it matches the
        editor's identity exactly. Fall back to edited_clip only when no raw_clip
        has ever been published (e.g. a file loaded directly as an edited clip).
        (None, None) when the bus has nothing."""
        path, _seq = self.bus.latest_info("raw_clip")
        if path:
            return path, "raw_clip"
        path, _seq = self.bus.latest_info("edited_clip")
        if path:
            return path, "edited_clip"
        return None, None

    def _on_visible(self, _e=None):
        self._refresh_state()
        self._maybe_warm_model()
        if self._style_question_key is not None:
            self.after_idle(self._ask_clip_style)

    def _sync_left_panel(self):
        installed = assets.installed_model_ids()
        if not installed:
            self._settings_frame.pack_forget()
            self._no_model_lbl.pack(fill="x", pady=(4, 0))
            return
        self._no_model_lbl.pack_forget()
        self._settings_frame.pack(fill="both", expand=True)

        scfg = self.app.config.setdefault("subtitles", {})
        names = [assets.get_asset(mid)["name"] for mid in installed]
        self._model_combo.configure(values=names)
        self._model_ids_by_name = {assets.get_asset(mid)["name"]: mid for mid in installed}
        current_id = scfg.get("model") or ""
        if current_id not in installed:
            current_id = installed[0]
            scfg["model"] = current_id
            self.app.save_config()
        self._model_var.set(assets.get_asset(current_id)["name"])
        self._refresh_device_status()

    def _segments_for(self, subject, dtype, total_frames):
        if dtype == "raw_clip":
            return subtitles.kept_segments_for(self.app.config, subject, total_frames)
        return [[0, max(0, total_frames - 1)]]

    def _set_clip_context(self, subject, segs_frames, cut_s, fps, n):
        changed = subject != self._subject
        if changed and self._clip_key is not None and self._transcript is not None:
            # Write pending typing and saves to the clip they belong to BEFORE the
            # key moves on (they used to land on the next clip).
            self._commit_now()
        self._subject = subject
        self._segs_frames = segs_frames
        self._cut_len_s = cut_s or 0.0
        self._fps = fps
        self._total_frames = n
        self._clip_key = paths.clip_key(subject) if subject else None
        if changed:
            self._stop_play()
            self._pcm = None
            self._pcm_rate = 0
            self._wav_for_key = None
            self._wav_for_fp = None
            self._playhead_t = 0.0
            if subject:
                self._open_preview_capture(subject)
            else:
                self._release_preview_capture()
            self._render_preview_frame(0.0)
            self._refresh_subtitle_enable_label()
            self._check_clip_style()

    # ── Persistent preview capture (6.4b) ──────────────────────────────────────────────
    def _open_preview_capture(self, path):
        self._release_preview_capture()
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            cap.release()
            return
        self._prev_cap = cap
        self._prev_cap_path = path
        self._prev_cap_pos = 0

    def _release_preview_capture(self):
        if self._prev_cap is not None:
            try:
                self._prev_cap.release()
            except Exception:  # noqa: BLE001 - releasing must never crash shutdown
                pass
            self._prev_cap = None
        self._prev_cap_path = None
        self._prev_cap_pos = -1
        self._preview_frame_cache = None

    def _read_source_frame(self, idx):
        """BGR frame at source index `idx`, decoded synchronously on the UI thread
        via the persistent capture. Sequential-grab optimized (mirrors the editor's
        _read_frame): a small forward gap is bridged with cheap grab()-and-discard
        calls instead of cap.set, which re-seeks to the previous keyframe and
        re-decodes everything since (the real cost on this kind of
        footage is GOP-bound seek time, not decode itself)."""
        if self._prev_cap is None:
            return None
        idx = max(0, int(idx))
        if idx != self._prev_cap_pos:
            gap = idx - self._prev_cap_pos
            if self._prev_cap_pos >= 0 and 0 < gap <= _SEQ_GRAB_MAX:
                for _ in range(gap):
                    if not self._prev_cap.grab():
                        self._prev_cap_pos = -1
                        return None
            else:
                self._prev_cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = self._prev_cap.read()
        if not ok or frame is None:
            self._prev_cap_pos = -1
            return None
        self._prev_cap_pos = idx + 1
        return frame

    def _refresh_state(self):
        self._sync_left_panel()
        subject, dtype = self._pick_source()
        if not subject:
            self._clip_len_lbl.configure(text="")
            self._hide_stale_banner()
            self._set_clip_context(None, None, None, 0.0, 0)
            self._loaded_for_key = None
            self._load_transcript_editor(None)
            self._show_message("Load a clip in Import or the Editor, then come here "
                               "to add subtitles.")
            return

        meta = video_utils.probe_video(subject)
        segs_frames, cut_s, live_fp, fps, n = None, None, None, 0.0, 0
        if meta is not None:
            _w, _h, fps, n = meta
            if fps > 0 and n > 0:
                segs_frames = self._segments_for(subject, dtype, n)
                kept = sum(max(0, s[1] - s[0] + 1) for s in segs_frames)
                cut_s = kept / fps
                live_fp = subtitles.cut_fingerprint(segs_frames, fps)
        self._clip_len_lbl.configure(
            text=f"{self._fmt_tc(cut_s)} - cut length" if cut_s is not None else "")
        self._set_clip_context(subject, segs_frames, cut_s, fps, n)

        if (self._transcribing and self._clip_key == self._transcribing_key
                and self._loaded_for_key == self._clip_key):
            return   # don't disturb the display of the clip being transcribed

        clip_key = self._clip_key
        if self._loaded_for_key != clip_key:
            transcript = subtitles.load_transcript(clip_key)
            self._loaded_for_key = clip_key
            if transcript is None:
                self._hide_stale_banner()
                self._load_transcript_editor(None)
                self._show_message("No transcript yet.")
                if (assets.installed_model_ids()
                        and self.app.config.get("subtitles", {}).get("auto_transcribe")):
                    self._on_transcribe(confirm=False)
                return
            self._load_transcript_editor(transcript)
        else:
            transcript = self._transcript
            if transcript is None:
                self._hide_stale_banner()
                self._show_message("No transcript yet.")
                return

        if live_fp is not None and live_fp != transcript.cut_fingerprint:
            # Keep-in-sync redoes a transcript on its own only when nobody fixed
            # anything in it; otherwise the banner asks first (Re-transcribe
            # confirms before replacing).
            if (self.app.config.get("subtitles", {}).get("auto_transcribe")
                    and not subtitles.is_hand_edited(transcript)):
                self._hide_stale_banner()
                self._on_transcribe(confirm=False)
                return
            self._show_stale_banner()
        else:
            self._hide_stale_banner()

    def _show_stale_banner(self):
        if not self._stale_banner.winfo_ismapped():
            self._stale_banner.pack(fill="x", pady=(0, 10), after=self._clip_row)

    def _hide_stale_banner(self):
        if self._stale_banner.winfo_ismapped():
            self._stale_banner.pack_forget()

    # ── Transcript display (message state) ────────────────────────────────────────────
    def _show_message(self, msg):
        self._transcript_text.delete("1.0", "end")
        self._transcript_text.insert("end", msg, ("empty",))

    @staticmethod
    def _fmt_tc(seconds):
        if seconds is None:
            return "0:00"
        seconds = max(0, seconds)
        m, s = divmod(int(seconds), 60)
        return f"{m}:{s:02d}"

    def _fmt_mmssff(self, seconds):
        fps = self._fps if self._fps > 0 else 30.0
        seconds = max(0.0, seconds)
        total_frames = int(round(seconds * fps))
        fps_i = max(1, int(round(fps)))
        ff = total_frames % fps_i
        total_seconds = total_frames // fps_i
        m, s = divmod(total_seconds, 60)
        return f"{m}:{s:02d}:{ff:02d}"

    def _parse_mmssff(self, text):
        fps = self._fps if self._fps > 0 else 30.0
        try:
            parts = text.strip().split(":")
            if len(parts) == 3:
                m, s, f = int(parts[0]), int(parts[1]), int(parts[2])
            elif len(parts) == 2:
                m, s, f = int(parts[0]), int(parts[1]), 0
            else:
                return None
            return m * 60 + s + f / fps
        except (ValueError, ZeroDivisionError):
            return None

    def _say(self, msg, palette_key=None):
        self._status_lbl.configure(text=msg, fg=PALETTE.get(palette_key, PALETTE["text_mute"]))

    # ── The interactive transcript editor (6.3a) ──────────────────────────────────────
    def _load_transcript_editor(self, transcript):
        self._transcript = transcript
        self._sel = None
        self._sel_char_offset = 0
        self._undo_stack = []
        self._redo_stack = []
        self._review_idx = -1
        self._cur_word_key = None
        self._stop_play()
        self._pcm = None
        self._pcm_rate = 0
        self._wav_for_key = None
        self._wav_for_fp = None
        self._tl_pan_offset = 0
        self._tl_wave_peaks = None
        self._tl_wave_for_key = None
        self._tl_wave_for_fp = None
        self._rebuild_flagged_queue()
        self._render_editor()
        if self._tl_zoom:
            self._ensure_tl_wave_ready()

    def _rebuild_flagged_queue(self):
        cutoff = self.app.config.get("subtitles", {}).get("flag_confidence", 0.6)
        queue_ = []
        if self._transcript is not None:
            for li, line in enumerate(self._transcript.lines):
                for wi, w in enumerate(line.words):
                    if subtitles.is_flagged(w, cutoff):
                        queue_.append((li, wi))
        self._flagged_queue = queue_
        if hasattr(self, "_review_btn"):
            self._review_btn.configure(
                text=f"Review flagged ({len(queue_)})",
                state=("normal" if queue_ else "disabled"))

    def _render_editor(self):
        self._render_timing_view()
        self._apply_cur_tag()
        self._render_selection_bar()
        self._redraw_timeline_strip(self._playhead_t)

    def _render_timing_view(self):
        self._typing_burst = False
        # Where is the caret RIGHT NOW, before anything is rebuilt? Captured
        # before the flush, because the flush can create or remove words and
        # the widget is the only place that still knows.
        caret = self._caret_word_offset()
        # NOT cleared here: the debounced reconcile absorbs typed text and sets
        # the hint BEFORE it asks for this re-render, so clearing it up front
        # threw away the only record of where the caret belonged.
        # Rebuilding this pane reads the MODEL, so any typing still sitting in
        # the widget has to be written in first or it is discarded (6.5c bug 1).
        self._flush_pending_typing()
        # A word created out of typed text supersedes it - the caret belongs in
        # the new word, not wherever it used to be.
        caret = self._caret_hint or caret
        self._caret_hint = None
        text, word_ranges, event_ranges = (
            self._transcript_text, self._word_ranges, self._event_ranges)
        yview = text.yview()
        text.delete("1.0", "end")
        # Marks are NOT removed by deleting the text they sit in - they collapse
        # to the deletion point - so last render's word marks would all survive
        # pointing at 1.0 and mis-anchor the next resync.
        for name in text.mark_names():
            if name.startswith("wm_"):
                text.mark_unset(name)
        word_ranges.clear()
        event_ranges.clear()
        if self._transcript is None:
            text.insert("end", "No transcript yet.", ("empty",))
            return
        cutoff = self.app.config.get("subtitles", {}).get("flag_confidence", 0.6)
        # 6.5c: rows are grouped the way the CURRENT reveal style will actually
        # show them - one row per word for one word at a time, one row per
        # segment otherwise - so the thing being edited is the thing that will
        # appear on screen. Switching style regroups this pane immediately.
        style = subtitles.SubtitleStyle.from_dict(
            self.app.config.get("subtitles", {}).get("style"))
        measure, max_width_px = subtitles.reference_packer(style)
        rows = subtitles.display_rows(self._transcript, style, measure, max_width_px)
        if not rows:
            text.insert("end", "Transcript is empty.", ("empty",))
        for row in rows:
            if row.kind == "words":
                self._render_words_row(text, row, cutoff, word_ranges)
            else:
                idx = row.items[0]
                self._render_event_row(text, idx, self._transcript.events[idx],
                                      event_ranges, show_tc=True)
            text.insert("end", "\n")
        text.yview_moveto(yview[0])
        self._restore_caret(caret)

    def _caret_word_offset(self):
        """Where the caret is, as (li, wi, characters into that word), or None.

        Read from the LIVE widget rather than from the selection, because while
        typing the two are routinely different - the selection is whatever was
        last clicked, the caret is where the letters are going."""
        widget = self._transcript_text
        try:
            idx = widget.index("insert")
        except tk.TclError:
            return None
        hit = (self._word_tag_containing(widget, idx)
               or self._word_at_boundary(widget, idx, "end")
               or self._word_at_boundary(widget, idx, "start"))
        if hit is None:
            return None
        rng = self._live_tag_range(widget, *hit)
        if not rng:
            return None
        count = widget.count(rng[0], idx, "chars")
        return (hit[0], hit[1], max(0, count[0] if count else 0))

    def _restore_caret(self, hint=None):
        """Put the caret back where it was before a re-render.

        Rebuilding the pane empties the widget and refills it, which leaves the
        insert mark at the very end of the document - the "cursor jumped to the
        bottom" after adding or deleting a word (6.5c). `hint` is the
        (li, wi, offset) captured beforehand; without one, fall back to the
        selection. Getting this right is what keeps typing from being
        interrupted: creating a word re-renders, and the caret has to come back
        INSIDE the new word, at the same letter, or the next keystroke lands
        somewhere else entirely."""
        widget = self._transcript_text
        if hint is None and self._sel is not None and self._sel[0] == "word":
            hint = (self._sel[1], self._sel[2], max(0, self._sel_char_offset))
        if hint is None:
            return
        li, wi, offset = hint
        rng = self._live_tag_range(widget, li, wi)
        if not rng:
            return
        try:
            pos = widget.index(f"{rng[0]}+{max(0, offset)}c")
            if widget.compare(pos, ">", rng[1]):
                pos = rng[1]
            widget.mark_set("insert", pos)
            widget.see(pos)
        except tk.TclError:
            pass

    def _render_words_row(self, text, row, cutoff, ranges):
        """One display row: its time range, then each of its words. The words
        keep their real (li, wi) model indices as tag names, so every edit path
        - resync, marks, clicks, arrow navigation - is unaffected by how the
        rows happen to be grouped."""
        tc = f"{self._fmt_mmssff(row.start)}-{self._fmt_mmssff(row.end)}  "
        start = text.index("end-1c")
        text.insert("end", tc)
        end = text.index("end-1c")
        text.tag_add("tc", start, end)
        for li, wi in row.items:
            if li < len(self._transcript.lines):
                words = self._transcript.lines[li].words
                if wi < len(words):
                    self._render_word(text, li, wi, words[wi], cutoff, ranges)

    def _render_word(self, text, li, wi, w, cutoff, ranges):
        """One word's tagged span + click binding."""
        wstart = text.index("end-1c")
        text.insert("end", w.text)
        wend = text.index("end-1c")
        tagname = f"w_{li}_{wi}"
        text.tag_add(tagname, wstart, wend)
        # A tag disappears the moment ALL of its text is deleted, and a word with
        # no tag can never be edited again (6.5c bug 2). A mark survives that -
        # it just collapses to the deletion point - so _resync_word_tag can
        # re-derive the span from here and the word stays editable. Left gravity
        # keeps text typed at the word's start inside the word.
        text.mark_set(f"wm_{li}_{wi}", wstart)
        text.mark_gravity(f"wm_{li}_{wi}", "left")
        if getattr(w, "added", False):
            text.tag_add("added", wstart, wend)
        elif subtitles.is_flagged(w, cutoff):
            text.tag_add("shaky", wstart, wend)
        if self._sel == ("word", li, wi):
            text.tag_add("sel_word", wstart, wend)
        ranges[(li, wi)] = (wstart, wend)
        text.insert("end", " ")


    def _render_event_row(self, text, idx, ev, ranges, show_tc=True):
        if show_tc:
            tc = f"{self._fmt_mmssff(ev.start)}-{self._fmt_mmssff(ev.end)}  "
            start = text.index("end-1c")
            text.insert("end", tc, ("tc",))
        else:
            start = text.index("end-1c")
        is_candidate = ev.kind == "gap_candidate"
        body_start = text.index("end-1c")
        label = ev.text if ev.text else "(suggested gap - unlabelled)"
        text.insert("end", label, ("gap_text" if is_candidate else "event_text",))
        body_end = text.index("end-1c")
        tagname = f"ev_{idx}"
        text.tag_add(tagname, body_start, body_end)
        if self._sel == ("event", idx):
            text.tag_add("sel_word", body_start, body_end)
        if show_tc:
            kindtxt = "  suggested" if is_candidate else "  manual"
            text.insert("end", kindtxt, ("event_kind",))
        end = text.index("end-1c")
        ranges[idx] = (start, end)

    def _char_offset_at(self, event, li, wi):
        widget = event.widget
        rng = self._word_ranges.get((li, wi))
        if not rng:
            return 0
        try:
            idx = widget.index(f"@{event.x},{event.y}")
            count = widget.count(rng[0], idx, "chars")
            return max(0, (count[0] if count else 0))
        except tk.TclError:
            return 0

    def _on_pane_press(self, event):
        """Mouse-down in the transcript: dispatch to the word or event under the
        pointer. One binding for the whole pane - per-word tag bindings were
        re-registered on every re-render and each left a Tcl command behind for
        the rest of the session. Always lets Tk's own click handling run so the
        caret lands natively on a word."""
        widget = event.widget
        self._press_on_event = False
        try:
            names = widget.tag_names(widget.index(f"@{event.x},{event.y}"))
        except tk.TclError:
            return None
        for name in names:
            if name.startswith("w_"):
                try:
                    li, wi = (int(p) for p in name[2:].split("_"))
                except ValueError:
                    continue
                if (self._transcript is not None and li < len(self._transcript.lines)
                        and wi < len(self._transcript.lines[li].words)):
                    self._on_word_click(li, wi)(event)
                return None
            if name.startswith("ev_"):
                try:
                    idx = int(name[3:])
                except ValueError:
                    continue
                # An event row is edited through its label field, not by typing
                # into the pane, so no caret is placed in it - which also means
                # the release handler must not act on the caret (still on the
                # previously clicked word) and re-select that word.
                self._press_on_event = True
                return self._on_event_click(idx)(event)
        return None

    def _on_word_click(self, li, wi):
        def handler(event):
            # 6.5 B1: click both selects (toolbar/timing fields + seeks the
            # preview/timeline) AND drops a real cursor - return None so Tk's own
            # click handling still places it natively.
            offset = self._char_offset_at(event, li, wi)
            self._select_word_in_place(li, wi, offset)
            offset_ms = self.app.config.get("subtitles", {}).get("timing_offset_ms", 0)
            word = self._transcript.lines[li].words[wi]
            self._seek_to(subtitles.apply_offset(word.start, offset_ms))
            return None
        return handler

    def _on_event_click(self, idx):
        def handler(_event):
            self._select_event(idx)
            return "break"
        return handler

    # ── Direct in-place editing (type to fix a word; space splits; backspace/
    # delete over a space merges) - always active, no mode toggle (6.5 B1). ───────
    _EDIT_KEEP_KEYS = frozenset((
        "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
        "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
        "Caps_Lock", "Escape", "Tab", "F1", "F2", "F3", "F4", "F5", "F6",
        "F7", "F8", "F9", "F10", "F11", "F12"))

    def _bind_editable_text(self, widget):
        # Guard first: the timecodes are a read-only visual aid, not content.
        widget.bind("<KeyPress>", self._guard_readonly, add=False)
        widget.bind("<KeyPress-space>", self._on_type_space_press)
        widget.bind("<KeyPress-Return>", self._on_type_return_press)
        widget.bind("<KeyPress-BackSpace>", self._on_type_backspace_press)
        widget.bind("<KeyPress-Delete>", self._on_type_delete_press)
        widget.bind("<KeyRelease>", self._on_type_key_release)
        widget.bind("<FocusOut>", lambda _e: self._reconcile_typed_text())
        widget.bind("<ButtonRelease-1>", self._on_pane_click, add="+")
        widget.bind("<Button-1>", self._on_pane_press, add="+")
        # v1.1: the arrow keys move like in any text editor (Left/Right by
        # character, Ctrl+Left/Right by word, Shift to select, Up/Down by row);
        # the word selection and playhead follow the caret on key release.
        # 6.5 B3's word-by-word arrows made fixing a single letter clunky.
        # Bound on the widget too so Tk's own Text undo/paste chords never run.
        for seq in _UNDO_SEQS:
            widget.bind(seq, self._on_undo_key)
        for seq in _REDO_SEQS:
            widget.bind(seq, self._on_redo_key)
        for seq in ("<Control-c>", "<Control-C>", "<<Copy>>"):
            widget.bind(seq, self._on_copy)

    _NOT_WORDS_TAGS = ("tc", "event_kind", "empty")

    def _words_only(self, widget, lo, hi):
        """The text in [lo, hi) as plain words: timecodes, row labels, and row
        breaks left out, so copied text pasted back never turns "0:02:06-0:03:21"
        into a word that gets burned into the video."""
        skip = []
        for tag in self._NOT_WORDS_TAGS:
            ranges = widget.tag_ranges(tag)
            skip += [(str(ranges[i]), str(ranges[i + 1])) for i in range(0, len(ranges), 2)]
        skip.sort(key=lambda r: widget.index(r[0]))
        parts, pos = [], lo
        for t0, t1 in skip:
            if widget.compare(t1, "<=", pos) or widget.compare(t0, ">=", hi):
                continue
            if widget.compare(pos, "<", t0):
                parts.append(widget.get(pos, t0))
            if widget.compare(t1, ">", pos):
                pos = t1
        if widget.compare(pos, "<", hi):
            parts.append(widget.get(pos, hi))
        return " ".join(" ".join(parts).split())

    def _on_copy(self, event):
        widget = event.widget
        sel = widget.tag_ranges("sel")
        if sel:
            widget.clipboard_clear()
            widget.clipboard_append(self._words_only(widget, str(sel[0]), str(sel[1])))
        return "break"

    @staticmethod
    def _flatten_paste(raw):
        """Clipboard text as one line of words, keeping a single leading/trailing
        space when the copied text had one - "Hey Leute " pasted before "kann"
        must stay two words apart, not become "Leutekann"."""
        core = " ".join(raw.split())
        if not core:
            return ""
        if raw[:1].isspace():
            core = " " + core
        if raw[-1:].isspace():
            core += " "
        return core

    _TEXT_EDIT_KEYS = frozenset(("BackSpace", "Delete", "Return", "space"))

    def _is_text_edit(self, event):
        """Would this keypress change the text? Printable characters and the
        editing keys do; navigation and shortcut chords do not, except paste and
        cut."""
        if event.state & 0x0004:
            return event.keysym.lower() in ("v", "x")
        return event.keysym in self._TEXT_EDIT_KEYS or (
            bool(event.char) and event.char.isprintable())

    def _begin_typing_step(self):
        """First text-changing key of a burst: snapshot the model so Ctrl+Z can
        take the whole burst back in one step. Pending typing is flushed first so
        the snapshot matches what is on screen. The burst ends when typing pauses
        (the debounced reconcile), the pane is rebuilt, or the user clicks."""
        if self._typing_burst or self._transcript is None:
            return
        self._flush_pending_typing()
        self._push_undo()
        self._typing_burst = True

    def _on_undo_key(self, _event=None):
        if not self._accepts_shortcut():
            return None
        self._do_undo()
        return "break"

    def _on_redo_key(self, _event=None):
        if not self._accepts_shortcut():
            return None
        self._do_redo()
        return "break"

    def _accepts_shortcut(self):
        """Ctrl+Z/Y are bound on the toplevel, so they reach this tab only while
        it is the one on screen, and never steal undo from a text field."""
        try:
            if not self.winfo_viewable():
                return False
            focus = self.focus_get()
        except (tk.TclError, KeyError):
            return False
        if isinstance(focus, (tk.Entry, tk.Spinbox)):
            return False
        return True

    def _guard_readonly(self, event):
        """Refuse any keystroke that would edit a row's timecode (6.5c).

        The timecodes share the transcript widget with the words - they are
        drawn as text - but they are only a visual aid, derived from the word
        times. Typing over one edits nothing real and just corrupts the display
        until the next re-render, so edits landing in (or spanning) a `tc` range
        are dropped. Navigation, modifiers, and the app's own shortcuts pass
        through untouched."""
        if event.keysym in self._EDIT_KEEP_KEYS:
            return None
        if event.state & 0x0004 and event.keysym not in ("BackSpace", "Delete") \
                and event.keysym.lower() not in ("x", "v"):
            return None   # Ctrl chords are shortcuts, not text entry (cut/paste are)
        widget = event.widget
        if self._is_text_edit(event) and widget.tag_ranges("empty"):
            # The pane shows a message, not words. With no transcript
            # there is nothing to type into; with an emptied one, the message
            # makes way and the typing becomes the first word.
            if self._transcript is None:
                return "break"
            empty = widget.tag_ranges("empty")
            widget.delete(empty[0], empty[1])
            widget.mark_set("insert", "1.0")
        try:
            sel = widget.tag_ranges("sel")
            if sel:
                lo, hi = str(sel[0]), str(sel[1])
            else:
                lo = hi = widget.index("insert")
                if event.keysym == "BackSpace":
                    lo = widget.index(f"{lo}-1c")
                elif event.keysym == "Delete":
                    hi = widget.index(f"{hi}+1c")
        except tk.TclError:
            return None
        if self._touches_timecode(widget, lo, hi):
            if widget.tag_ranges("sel") and self._is_text_edit(event):
                return self._edit_across_rows(widget, lo, hi, event)
            return "break"
        if self._is_text_edit(event):
            self._begin_typing_step()
            if event.state & 0x0004 and event.keysym.lower() == "v":
                return self._paste_plain(widget)
        return None

    def _paste_plain(self, widget):
        """Paste as one line of words: line breaks and tabs in copied text would
        otherwise land inside a row and split it in ways the model cannot follow."""
        try:
            pasted = self._flatten_paste(widget.clipboard_get())
        except tk.TclError:
            return "break"
        if widget.tag_ranges("sel"):
            widget.delete("sel.first", "sel.last")
        widget.insert("insert", pasted)
        self._resync_all_word_tags(widget)
        if self._type_reconcile_job is not None:
            self.after_cancel(self._type_reconcile_job)
        self._type_reconcile_job = self.after(500, self._reconcile_typed_text)
        return "break"

    def _edit_across_rows(self, widget, lo, hi, event):
        """A selection spanning several rows was typed over, cut, or deleted.

        Tk would delete the timecodes and row breaks along with the words, so
        this deletes only the word text inside the selection and leaves each
        row's timecode and break in place; the normal reconcile then trims the
        partly selected words and drops the emptied ones. A typed character or a
        paste lands where the selection started (or at the first word after it,
        when the selection started on a timecode)."""
        self._begin_typing_step()
        ctrl = bool(event.state & 0x0004)
        if ctrl and event.keysym.lower() == "x":
            widget.clipboard_clear()
            widget.clipboard_append(self._words_only(widget, lo, hi))
        insert_text = ""
        if ctrl and event.keysym.lower() == "v":
            try:
                insert_text = widget.clipboard_get()
            except tk.TclError:
                insert_text = ""
        elif not ctrl and event.keysym not in ("BackSpace", "Delete") and event.char:
            insert_text = " " if event.keysym == "space" else event.char
        # Protected spans inside the selection: timecodes and row breaks.
        keep = []
        ranges = widget.tag_ranges("tc")
        for i in range(0, len(ranges), 2):
            keep.append((str(ranges[i]), str(ranges[i + 1])))
        pos = lo
        while True:
            nl = widget.search("\n", pos, stopindex=hi)
            if not nl:
                break
            keep.append((nl, widget.index(f"{nl}+1c")))
            pos = widget.index(f"{nl}+1c")
        # Delete the gaps between protected spans, last first so indices hold.
        cuts, cur = [], lo
        for k0, k1 in sorted(keep, key=lambda r: widget.index(r[0])):
            if widget.compare(k1, "<=", cur) or widget.compare(k0, ">=", hi):
                continue
            if widget.compare(cur, "<", k0):
                cuts.append((cur, k0))
            cur = k1 if widget.compare(k1, ">", cur) else cur
        if widget.compare(cur, "<", hi):
            cuts.append((cur, hi))
        widget.tag_remove("sel", "1.0", "end")
        for c0, c1 in reversed(cuts):
            widget.delete(c0, c1)
        if insert_text:
            at = lo
            if self._touches_timecode(widget, at, at):
                rng = widget.tag_nextrange("tc", f"{at} linestart")
                at = str(rng[1]) if rng else at
            widget.mark_set("insert", at)
            widget.insert(at, self._flatten_paste(insert_text) if len(insert_text) > 1
                          else insert_text)
        else:
            widget.mark_set("insert", cuts[0][0] if cuts else lo)
        # The re-render that drops the emptied words would otherwise leave the
        # caret at the end of the document; bring it back to the edit point.
        caret = widget.index("insert")
        for li, line in enumerate(self._transcript.lines):
            for wi in range(len(line.words)):
                brng = self._live_tag_range(widget, li, wi)
                if (brng and widget.compare(brng[0], "<", brng[1])
                        and widget.compare(brng[1], "<=", caret)):
                    n = widget.count(brng[0], brng[1], "chars")
                    self._caret_hint = (li, wi, n[0] if n else 0)
        self._resync_all_word_tags(widget)
        if self._type_reconcile_job is not None:
            self.after_cancel(self._type_reconcile_job)
        self._type_reconcile_job = self.after(500, self._reconcile_typed_text)
        return "break"

    def _touches_timecode(self, widget, lo, hi):
        """Does [lo, hi) overlap any timecode? A zero-width position counts as
        inside only when it is strictly within one - sitting at a timecode's
        far edge is the first word's start, where typing is fine."""
        ranges = widget.tag_ranges("tc")
        for i in range(0, len(ranges), 2):
            t0, t1 = str(ranges[i]), str(ranges[i + 1])
            if widget.compare(lo, "==", hi):
                if widget.compare(t0, "<=", lo) and widget.compare(lo, "<", t1):
                    return True
            elif widget.compare(lo, "<", t1) and widget.compare(t0, "<", hi):
                return True
        return False

    def _on_pane_click(self, event):
        """Land the caret on a word, wherever in the row it was clicked.

        Clicking the empty stretch past the end of a row, or the space between
        two words, otherwise parked the caret on nothing - it looked like the
        click had missed, and the effect buttons had no word to act on. The
        nearest word takes it instead, and gets selected (6.5c)."""
        self._typing_burst = False   # a click starts a new undo step
        if getattr(self, "_press_on_event", False) and event.type == tk.EventType.ButtonRelease:
            self._press_on_event = False
            return   # the press selected an event row; the caret never moved
        widget = event.widget
        if widget.tag_ranges("sel"):
            return   # a drag-selection: leave both the selection and the caret alone
        try:
            idx = widget.index("insert")
        except tk.TclError:
            return
        for rng in self._event_ranges.values():
            if rng and widget.compare(idx, ">=", rng[0]) and \
                    widget.compare(idx, "<=", rng[1]):
                return   # an event row owns this click - _on_event_click has it
        hit = (self._word_tag_containing(widget, idx)
               or self._word_at_boundary(widget, idx, "start")
               or self._word_at_boundary(widget, idx, "end"))
        if hit is None:
            before = self._word_at_or_before(widget, idx)
            after = self._word_at_or_after(widget, idx)
            # On a row's timecode (a click there, or Up/Down landing in one)
            # the row's own first word is meant, not the previous row's last.
            hit = ((after or before) if self._touches_timecode(widget, idx, idx)
                   else (before or after))
            if hit is None:
                return
            rng = self._live_tag_range(widget, *hit)
            if rng:
                widget.mark_set("insert", rng[1] if hit is before else rng[0])
        if self._sel == ("word",) + tuple(hit):
            return
        self._select_word_in_place(*hit)
        # Clicking a word jumps the playhead there; clicking beside one has to do
        # the same, or half the row behaves differently from the other half.
        li, wi = hit
        if li < len(self._transcript.lines):
            words = self._transcript.lines[li].words
            if wi < len(words):
                offset_ms = self.app.config.get("subtitles", {}).get("timing_offset_ms", 0)
                self._seek_to(subtitles.apply_offset(words[wi].start, offset_ms))

    def _live_tag_range(self, widget, li, wi):
        """The word tag's CURRENT extent, queried fresh from the widget - NOT the
        cached ranges dict, which goes stale the instant typing changes that
        word's length (a plain index string like "1.24" doesn't track content
        edits; the tag itself does, so re-querying tag_ranges() is the only way
        to get the right span mid-edit)."""
        rng = widget.tag_ranges(f"w_{li}_{wi}")
        return (str(rng[0]), str(rng[1])) if rng else None

    def _word_tag_containing(self, widget, idx):
        """(li, wi) whose tagged range STRICTLY contains `idx` (not at an edge), or
        None. Used to find which word a typed space landed inside."""
        for li, wi in self._word_ranges:
            rng = self._live_tag_range(widget, li, wi)
            if rng and widget.compare(rng[0], "<", idx) and widget.compare(idx, "<", rng[1]):
                return (li, wi)
        return None

    def _word_at_boundary(self, widget, idx, which):
        """(li, wi) of the word tag whose start (which="start") or end
        (which="end") is EXACTLY `idx`, or None."""
        for li, wi in self._word_ranges:
            rng = self._live_tag_range(widget, li, wi)
            if rng is None:
                continue
            target = rng[1] if which == "end" else rng[0]
            if widget.compare(target, "==", idx):
                return (li, wi)
        return None

    def _resync_word_tag(self, widget, li, wi):
        """Re-derive the word's tag range by scanning outward from wherever its
        tag CURRENTLY sits to the nearest space/newline on each side, then
        re-applies the tag to that full span (6.5 bugfix: Tk does NOT extend a
        tag to cover a character typed exactly at its edge, so typing at the
        very start/end of a word - a common case right after a click - silently
        fell outside the tag and never reconciled back into the model). Safe
        because words are always space-delimited in these panes; returns the
        resynced (start, end), or None if the tag is gone."""
        tagname = f"w_{li}_{wi}"
        old = self._live_tag_range(widget, li, wi)
        from_mark = False
        if old is None:
            # The tag is gone - every character it covered was deleted (select a
            # word and retype it, or backspace it away). Fall back to the word's
            # mark, which is still sitting exactly where that text was, and let
            # the scan below rebuild the span from whatever is there now.
            anchor = self._word_mark_index(widget, li, wi)
            if anchor is None:
                return None
            old = (anchor, anchor)
            from_mark = True
        start = old[0]
        while widget.compare(start, ">", "1.0"):
            prev = widget.get(f"{start}-1c", start)
            if not prev or prev in (" ", "\n"):
                break
            start = widget.index(f"{start}-1c")
        end = old[1]
        while True:
            nxt = widget.get(end, f"{end}+1c")
            if not nxt or nxt in (" ", "\n"):
                break
            end = widget.index(f"{end}+1c")
        if (start, end) != old or from_mark:
            widget.tag_remove(tagname, "1.0", "end")
            if widget.compare(start, "<", end):
                widget.tag_add(tagname, start, end)
                if from_mark:
                    # The tag was rebuilt from scratch, so the mark has to be
                    # re-established too. Clicks need nothing: one widget-level
                    # handler resolves the word from its tag (_on_pane_press).
                    widget.mark_set(f"wm_{li}_{wi}", start)
                    widget.mark_gravity(f"wm_{li}_{wi}", "left")
        return (start, end)

    def _word_mark_index(self, widget, li, wi):
        """The word's position mark, or None if it is gone too."""
        try:
            return str(widget.index(f"wm_{li}_{wi}"))
        except tk.TclError:
            return None

    def _resync_all_word_tags(self, widget):
        for li, wi in list(self._word_ranges.keys()):
            self._resync_word_tag(widget, li, wi)

    def _resync_caret_row_tags(self, widget):
        """The per-keystroke resync: only the words on the caret's row can have
        moved from plain typing, so scanning every word on every key (linear in
        transcript length) is wasted work on long clips. Rows are one text line
        each, and a row break is always a structural edit with its own
        re-render."""
        try:
            line = widget.index("insert").split(".")[0]
        except tk.TclError:
            return
        for (li, wi), (start, _end) in list(self._word_ranges.items()):
            if str(start).split(".")[0] == line:
                self._resync_word_tag(widget, li, wi)

    def _on_type_space_press(self, event):
        """A space typed strictly inside a word IS the split action - swallow the
        keystroke and split there instead of inserting a literal space."""
        if self._transcript is None:
            return None
        guarded = self._guard_readonly(event)
        if guarded is not None:
            return guarded
        widget = event.widget
        if widget.tag_ranges("sel"):
            return None   # typing over a selection: Tk replaces it, reconcile follows
        self._resync_all_word_tags(widget)
        # This decides where to split from word.text's LENGTH, so the model has
        # to be current first - a stale word.text splits at the wrong offset.
        if self._flush_pending_typing()[1]:
            # The flush removed words, so the pane is rebuilt first (the caret
            # is restored by the re-render) - then this key is handled on the
            # fresh pane. Returning here used to swallow the keystroke: type
            # "darf es" over a selection and the space vanished.
            self._after_edit()
        idx = widget.index("insert")
        hit = self._word_tag_containing(widget, idx)
        if hit is None:
            return None
        li, wi = hit
        start = self._live_tag_range(widget, li, wi)[0]
        count = widget.count(start, idx, "chars")
        offset = count[0] if count else 0
        word = self._transcript.lines[li].words[wi]
        if offset <= 0 or offset >= len(word.text):
            return None   # at the very edge - a normal space here is harmless
        self._push_undo()
        subtitles.split_word(self._transcript, li, wi, offset)
        self._select_word(li, wi)
        return "break"

    def _on_type_backspace_press(self, event):
        """Backspacing over the single space BETWEEN two words merges them."""
        return self._on_type_merge_press(event, forward=False)

    def _on_type_delete_press(self, event):
        """Forward-deleting the single space BETWEEN two words merges them."""
        return self._on_type_merge_press(event, forward=True)

    def _on_type_merge_press(self, event, forward):
        """Backspace and delete behave like any text editor (6.5c).

        In the middle of a word they delete a character - the keystroke just
        passes through. On a boundary they JOIN what is on either side, and what
        that means depends on where the two words live:

        - both in the same line  ->  the two words become one (this is how a
          model that split "Tem pest" gets put back together).
        - in different lines     ->  the two lines join instead, and both words
          keep their own text and timing. That is the row-merge a text editor
          does when you backspace at the start of a line.
        """
        if self._transcript is None:
            return None
        widget = event.widget
        if widget.tag_ranges("sel"):
            # A selection is deleted, never treated as a join - even when the
            # caret happens to sit on a word boundary at one end of it.
            return self._guard_readonly(event)
        self._begin_typing_step()
        self._resync_all_word_tags(widget)
        if self._flush_pending_typing()[1]:
            # The flush removed words, so the pane is rebuilt first (the caret
            # is restored by the re-render) - then this key is handled on the
            # fresh pane. Returning here used to swallow the keystroke: type
            # "darf es" over a selection and the space vanished.
            self._after_edit()
        idx = widget.index("insert")
        hit = self._word_at_boundary(widget, idx, "end" if forward else "start")
        if hit is None:
            return None   # inside a word: an ordinary character delete
        if forward:
            first, second = hit, self._next_word(*hit)
        else:
            first, second = self._prev_word(*hit), hit
        if first is None or second is None:
            # The very start or end of the transcript - nothing to join. Swallow
            # it so a stray keystroke cannot eat into the row's timecode.
            return "break"
        self._push_undo()
        if first[0] == second[0]:
            subtitles.merge_words(self._transcript, first[0], first[1])
        else:
            subtitles.merge_lines(self._transcript, first[0])
        self._select_word(*first)
        return "break"

    def _on_type_return_press(self, event):
        """Return starts a new segment at the cursor (6.5c) - the counterpart to
        the row-join above. Splits the line so the word at the cursor begins the
        next row; a no-op where the split would produce nothing."""
        if self._transcript is None:
            return "break"
        widget = event.widget
        if widget.tag_ranges("sel"):
            return "break"   # a line break never replaces a selection
        guarded = self._guard_readonly(event)
        if guarded is not None:
            return guarded
        self._resync_all_word_tags(widget)
        if self._flush_pending_typing()[1]:
            self._after_edit()   # rebuilt first; this key is still handled below
        idx = widget.index("insert")
        hit = (self._word_at_boundary(widget, idx, "start")
               or self._word_at_or_after(widget, idx))
        if hit is None:
            return "break"
        li, wi = hit
        if wi <= 0:
            return "break"   # already the start of a line
        self._push_undo()
        subtitles.split_line(self._transcript, li, wi)
        self._select_word(li + 1, 0)
        return "break"

    _CARET_MOVE_KEYS = frozenset(("Left", "Right", "Up", "Down", "Home", "End",
                                  "Prior", "Next"))

    def _on_type_key_release(self, event):
        if event.keysym in self._CARET_MOVE_KEYS:
            self._on_pane_click(event)   # same as a click at the new caret spot
            return
        # Resync immediately (cheap - just tag boundary scans on this row) so a
        # word's tag never falls behind what's on screen, even mid fast typing;
        # the actual model write below stays debounced.
        self._resync_caret_row_tags(event.widget)
        if self._type_reconcile_job is not None:
            self.after_cancel(self._type_reconcile_job)
        self._type_reconcile_job = self.after(500, self._reconcile_typed_text)

    def _flush_typed_text(self):
        """Reconcile what is ON SCREEN back into the transcript, and report
        (text_changed, structure_changed).

        The pane is a text editor first: the user selects a run of words and
        retypes or deletes it, and the transcript has to follow. Three outcomes,
        all driven by what the widget actually holds now:

        - a word's span holds different text  ->  that word's text is updated.
        - several words resolve to ONE span   ->  they were replaced by a single
          word. The first keeps the typed text and stretches to cover the whole
          range that was overwritten; the rest are deleted.
        - a word has no text left at all      ->  it was deleted, so it goes.

        Without the collapse rule, N words replaced by one all resolved to the
        same span and each took that span's text - retyping one "oh," in a run
        of them rewrote every one (6.5c)."""
        if self._transcript is None:
            return False, False
        widget = self._transcript_text
        changed = False
        spans = {}
        gone = []
        for li, wi in sorted(self._word_ranges.keys()):
            if li >= len(self._transcript.lines):
                continue
            if wi >= len(self._transcript.lines[li].words):
                continue
            rng = self._resync_word_tag(widget, li, wi)
            if rng is None:
                gone.append((li, wi))
                continue
            try:
                current = widget.get(rng[0], rng[1])
            except tk.TclError:
                continue
            if not current:
                gone.append((li, wi))
                continue
            spans.setdefault(rng, []).append((li, wi))

        doomed = list(gone)
        for rng, owners in spans.items():
            current = widget.get(rng[0], rng[1])
            li, wi = owners[0]
            word = self._transcript.lines[li].words[wi]
            if len(owners) > 1:
                # One span, several words: a multi-word selection was replaced.
                last_li, last_wi = owners[-1]
                word.end = max(word.end,
                              self._transcript.lines[last_li].words[last_wi].end)
                doomed.extend(owners[1:])
            if " " in current:
                continue
            if current != word.text:
                word.text = current
                changed = True

        structure = self._absorb_new_tokens(widget)
        for li, wi in sorted(set(doomed), reverse=True):
            if li < len(self._transcript.lines) and \
                    wi < len(self._transcript.lines[li].words):
                subtitles.delete_word(self._transcript, li, wi)
                structure = True
        return changed, structure

    def _absorb_new_tokens(self, widget):
        """Turn text the user typed where no word exists into real words (6.5c).

        Type at the end of a row, in the space between two words, or on a fresh
        row after Return, and that text belongs to nothing - it used to be
        ignored and wiped out by the next re-render. Here it becomes a word,
        placed straight after whichever word it follows on screen, given a span
        suited to its length, and flagged `added` so it stands out until it has
        been dragged where it belongs.

        Returns True if any word was created."""
        made = False
        last_seen = None      # the tagged word most recently passed, in reading order
        pending = []          # (after_key, text) in document order
        total_rows = int(widget.index("end-1c").split(".")[0])
        for row in range(1, total_rows + 1):
            line_text = widget.get(f"{row}.0", f"{row}.end")
            col = 0
            while col < len(line_text):
                if line_text[col].isspace():
                    col += 1
                    continue
                run_end = col
                while run_end < len(line_text) and not line_text[run_end].isspace():
                    run_end += 1
                token = line_text[col:run_end]
                idx = f"{row}.{col}"
                names = widget.tag_names(idx)
                owner = next((n for n in names if n.startswith("w_")), None)
                if owner is not None:
                    parts = owner.split("_")
                    last_seen = (int(parts[1]), int(parts[2]))
                elif not any(n in ("tc", "event_text", "event_kind", "gap_text",
                                   "empty") for n in names):
                    # Is the caret inside this token? If so the word about to be
                    # created from it must end up holding the caret, at the same
                    # letter, or the re-render interrupts typing mid-word.
                    caret_off = None
                    try:
                        ins = widget.index("insert")
                        if widget.compare(ins, ">=", idx) and \
                                widget.compare(ins, "<=", f"{row}.{run_end}"):
                            cnt = widget.count(idx, ins, "chars")
                            caret_off = max(0, cnt[0] if cnt else 0)
                    except tk.TclError:
                        caret_off = None
                    pending.append((last_seen, token, caret_off))
                col = run_end

        # Several tokens can follow the SAME existing word ("und dann" typed in
        # one go). Each has to land after the previous one, or they insert at the
        # same index and come out reversed.
        nudge = {}
        for after, token, caret_off in pending:
            step = nudge.get(after, 0)
            nudge[after] = step + 1
            if after is None:
                li, wi = 0, step
                if not self._transcript.lines:
                    self._transcript.lines.append(subtitles.Line(words=[]))
            else:
                li, wi = after[0], after[1] + 1 + step
            if li >= len(self._transcript.lines):
                continue
            wi = min(wi, len(self._transcript.lines[li].words))
            subtitles.insert_word_squeeze(self._transcript, li, wi, text=token,
                                         span=subtitles.natural_span(token))
            self._transcript.lines[li].words[wi].added = True
            if caret_off is not None:
                self._caret_hint = (li, wi, caret_off)
            made = True
            last_seen = (li, wi)
        return made

    def _flush_pending_typing(self):
        """Cancel the pending debounce and apply the typed text immediately.

        Called before anything that rebuilds the pane from the model, or that
        reads word.text to decide a structural edit. Typing lands on screen at
        once but only reaches the model 500ms later, so without this ANY action
        in that window (a toolbar button, a split, an undo) rebuilt the pane
        from a transcript that did not have the last keystrokes yet and threw
        them away (6.5c bug 1)."""
        if self._type_reconcile_job is None:
            return False, False
        self.after_cancel(self._type_reconcile_job)
        self._type_reconcile_job = None
        return self._flush_typed_text()

    def _reconcile_typed_text(self):
        """The debounced path: apply the typed text, then refresh everything that
        depends on it. Never re-renders the text widgets (that would reset the
        cursor mid-typing) - but the preview, timeline, and current-word
        highlight do refresh, so an edit propagates with no commit step."""
        self._type_reconcile_job = None
        self._typing_burst = False
        changed, structure = self._flush_typed_text()
        if structure:
            # Words were removed, so every tag keyed on (li, wi) below them is
            # stale - the pane has to be rebuilt, not just refreshed.
            self._rebuild_flagged_queue()
            self._render_editor()
        if changed or structure:
            self._debounced_save()
            self._update_playhead_ui(self._playhead_t)

    def _select_word(self, li, wi, char_offset=0):
        """Select a word AND fully re-render both text panes - needed whenever the
        model's word indices may have shifted (insert/delete/split/merge/undo/
        redo/review-jump), since the widgets' tags are keyed by (li, wi) and go
        stale the instant the transcript's structure changes."""
        self._sel = ("word", li, wi)
        self._sel_char_offset = char_offset
        self._tl_pan_offset = 0   # 6.5 C1: a fresh selection re-follows, never fights a pan
        self._tl_follow_playhead = False
        self._render_editor()

    def _select_word_in_place(self, li, wi, char_offset=0):
        """Lightweight selection for a plain click on an ALREADY-rendered word -
        no full re-render, so the real text cursor Tk's own click handling just
        placed there survives (6.5 B1: click both selects for the toolbar/timing
        fields AND drops a cursor for direct typing, in one motion)."""
        self._sel = ("word", li, wi)
        self._sel_char_offset = char_offset
        self._tl_pan_offset = 0
        self._tl_follow_playhead = False
        self._render_selection_bar()
        self._apply_sel_tag()
        self._redraw_timeline_strip(self._playhead_t)

    def _apply_sel_tag(self):
        self._highlight_effect_btns()
        """Move the "sel_word" highlight tag to the current selection without
        touching the text content - the light-select counterpart to
        _apply_cur_tag."""
        widget = self._transcript_text
        widget.tag_remove("sel_word", "1.0", "end")
        if self._sel is None:
            return
        if self._sel[0] == "word":
            _, li, wi = self._sel
            rng = self._live_tag_range(widget, li, wi)
        else:
            _, idx = self._sel
            rng = self._event_ranges.get(idx)
        if rng:
            widget.tag_add("sel_word", rng[0], rng[1])

    def _select_event(self, idx):
        """Select an event (a suggested gap or a confirmed non-speech entry) AND
        seek + scroll it into view - 6.5 B5 fix: this used to only select, so
        clicking a suggested gap never actually moved the preview/timeline to
        it, and it could be scrolled off-screen in the text pane."""
        self._sel = ("event", idx)
        self._tl_pan_offset = 0
        self._tl_follow_playhead = False
        self._render_editor()
        if self._transcript is None or not (0 <= idx < len(self._transcript.events)):
            return
        ev = self._transcript.events[idx]
        offset_ms = self.app.config.get("subtitles", {}).get("timing_offset_ms", 0)
        self._seek_to(subtitles.apply_offset(ev.start, offset_ms))
        rng = self._event_ranges.get(idx)
        if rng:
            self._transcript_text.see(rng[0])

    # ── Arrow-key navigation (6.5 B3) ─────────────────────────────────────────────────
    def _prev_word(self, li, wi):
        if wi > 0:
            return (li, wi - 1)
        for pli in range(li - 1, -1, -1):
            words = self._transcript.lines[pli].words
            if words:
                return (pli, len(words) - 1)
        return None

    def _next_word(self, li, wi):
        words = self._transcript.lines[li].words
        if wi + 1 < len(words):
            return (li, wi + 1)
        for nli in range(li + 1, len(self._transcript.lines)):
            if self._transcript.lines[nli].words:
                return (nli, 0)
        return None

    def _goto_word(self, li, wi):
        """Select + seek + scroll into view + drop a real cursor - the shared
        "jump to this word" primitive for keyboard nav, confidence review, and
        the suggested-gap jump (6.5 B5)."""
        self._select_word_in_place(li, wi)
        offset_ms = self.app.config.get("subtitles", {}).get("timing_offset_ms", 0)
        word = self._transcript.lines[li].words[wi]
        self._seek_to(subtitles.apply_offset(word.start, offset_ms))
        widget = self._transcript_text
        rng = self._live_tag_range(widget, li, wi)
        if rng:
            widget.see(rng[0])
            widget.mark_set("insert", rng[0])

    def _goto_gap(self, t):
        """Seek + select whichever word now sits nearest time `t` - used after a
        delete leaves a gap with no word of its own to select, and by the
        suggested-gap jump (6.5 B4/B5)."""
        target = self._nearest_word_at_or_after(t)
        if target is None:
            target = self._nearest_word_before(t)
        if target is not None:
            self._goto_word(*target)
        else:
            self._seek_to(t)   # nothing left in the transcript at all

    def _nearest_word_at_or_after(self, t):
        best = None
        for li, line in enumerate(self._transcript.lines):
            for wi, w in enumerate(line.words):
                if w.start >= t and (best is None or w.start < best[2]):
                    best = (li, wi, w.start)
        return (best[0], best[1]) if best else None

    def _nearest_word_before(self, t):
        best = None
        for li, line in enumerate(self._transcript.lines):
            for wi, w in enumerate(line.words):
                if w.start < t and (best is None or w.start > best[2]):
                    best = (li, wi, w.start)
        return (best[0], best[1]) if best else None

    def _clear_selection(self):
        self._sel = None
        self._sel_char_offset = 0

    # ── Selection bar (word/event text + timing, review controls) ────────────────────
    def _render_selection_bar(self):
        """The toolbar's context strip: only what the CURRENT selection needs.

        A word's text and timing are edited in the transcript and on the
        timeline, so neither has a field here any more (6.5c). What is left has
        nowhere else to live - a flagged word's keep/delete, a suggested gap's
        confirm/dismiss, and a non-speech entry's label."""
        for w in self._sel_bar.winfo_children():
            w.destroy()
        self._sel_text_entry = None
        self._sel_time_vars = None
        self._highlight_effect_btns()
        if self._sel is None or self._transcript is None:
            return
        if self._sel[0] == "word":
            self._render_word_selection_bar()
        else:
            self._render_event_selection_bar()

    def _render_word_selection_bar(self):
        _, li, wi = self._sel
        if li >= len(self._transcript.lines) or wi >= len(self._transcript.lines[li].words):
            self._clear_selection()
            return
        word = self._transcript.lines[li].words[wi]
        cutoff = self.app.config.get("subtitles", {}).get("flag_confidence", 0.6)
        if not subtitles.is_flagged(word, cutoff):
            return
        bar = self._sel_bar
        tk.Label(bar, text=f"{int(word.conf * 100)}% shaky", bg=PALETTE["deep"],
                 fg=PALETTE["yellow"], font=ui_font(9)).pack(side="left", padx=(0, 6))
        themed_button(bar, "Keep", self._review_keep, kind="neutral").pack(
            side="left", padx=(0, 4))
        themed_button(bar, "Delete", self._review_delete, kind="danger").pack(
            side="left")

    def _render_event_selection_bar(self):
        _, idx = self._sel
        if idx >= len(self._transcript.events):
            self._clear_selection()
            return
        ev = self._transcript.events[idx]
        bar = self._sel_bar
        if ev.kind == "gap_candidate":
            tk.Label(bar, text="Suggested gap", bg=PALETTE["deep"],
                     fg=PALETTE["text_mute"], font=ui_font(9)).pack(side="left")
            themed_button(bar, "Confirm", self._on_confirm_gap, kind="primary").pack(
                side="left", padx=(8, 4))
            themed_button(bar, "Dismiss", self._on_dismiss_gap, kind="neutral").pack(
                side="left")
            return
        # A confirmed non-speech entry is not word-tagged text in the pane, so
        # its label still needs somewhere to be typed.
        self._sel_text_var = tk.StringVar(value=ev.text)
        entry = ttk.Entry(bar, textvariable=self._sel_text_var, width=18)
        entry.pack(side="left", padx=(0, 4))
        entry.bind("<Return>", self._commit_event_text)
        entry.bind("<FocusOut>", self._commit_event_text)
        self._sel_text_entry = entry
        themed_button(bar, "Delete", self._delete_selected_event, kind="danger").pack(
            side="left")

    def _selected_effects_owner(self):
        """The Word or Event the effect buttons act on, or None."""
        if self._sel is None or self._transcript is None:
            return None
        if self._sel[0] == "word":
            _, li, wi = self._sel
            if li < len(self._transcript.lines):
                words = self._transcript.lines[li].words
                if wi < len(words):
                    return words[wi]
        else:
            _, idx = self._sel
            if idx < len(self._transcript.events):
                return self._transcript.events[idx]
        return None

    def _highlight_effect_btns(self):
        """Light the effect buttons that the current selection carries, and grey
        them out when nothing is selected."""
        owner = self._selected_effects_owner()
        for key, btn in getattr(self, "_effect_btns", {}).items():
            if owner is None:
                btn.configure(bg=PALETTE["card"], fg=PALETTE["text_faint"],
                             activebackground=PALETTE["card"],
                             activeforeground=PALETTE["text_faint"])
                continue
            on = key in (owner.effects or [])
            bg = PALETTE["green"] if on else PALETTE["card"]
            fg = PALETTE["deep"] if on else PALETTE["text_dim"]
            btn.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)

    def _toggle_selected_effect(self, key):
        """Tag or untag an effect on whatever is selected - word or non-speech
        entry alike (6.5c: events animate too)."""
        owner = self._selected_effects_owner()
        if owner is None:
            self._say("Click a word or a non-speech entry first.")
            return
        effects = list(owner.effects or [])
        if key in effects:
            effects.remove(key)
        else:
            effects.append(key)
        self._push_undo()
        owner.effects = effects
        self._highlight_effect_btns()
        self._debounced_save()
        self._render_preview_frame(self._playhead_t)

    def _commit_event_text(self, _e=None):
        if self._sel is None or self._sel[0] != "event":
            return
        _, idx = self._sel
        ev = self._transcript.events[idx]
        text = self._sel_text_var.get()
        if text == ev.text:
            return
        self._push_undo()
        ev.text = text
        self._after_edit()

    # ── Toolbar actions ────────────────────────────────────────────────────────────────
    def _after_edit(self):
        self._rebuild_flagged_queue()
        self._render_editor()
        self._debounced_save()

    def _do_delete(self):
        """Removes the selected word and leaves a timing gap (delete_word never
        reflows neighbours), then jumps to whichever word now sits nearest that
        gap so it's easy to fix up right away (6.5 B4)."""
        if self._sel is None or self._sel[0] != "word":
            return self._say("Select a word first.")
        _, li, wi = self._sel
        gap_t = self._transcript.lines[li].words[wi].start
        self._push_undo()
        subtitles.delete_word(self._transcript, li, wi)
        self._clear_selection()
        self._after_edit()
        self._goto_gap(gap_t)

    def _do_insert(self):
        """Inserts a new word relative to the CURRENT CURSOR position (6.5
        bugfix: reading self._sel/_sel_char_offset alone went stale the moment
        you clicked into the untagged gap AFTER a word - no click handler fires
        there, so it silently reused whichever word you clicked last). Prefers
        the live text-cursor position in whichever pane has focus; only falls
        back to the last explicit word selection if focus isn't in a pane at
        all. Squeezes room out of the neighbours if they're touching, then
        jumps straight to the new word, selected and ready to type - no dialog."""
        target = self._insert_anchor()
        if target is None:
            return self._say("Select a word first.")
        li, target_wi = target
        self._push_undo()
        subtitles.insert_word_squeeze(self._transcript, li, target_wi)
        self._after_edit()
        self._goto_word(li, target_wi)

    def _insert_anchor(self):
        """(li, target_wi) to insert a new word before - the live cursor
        position in whichever text pane has focus, so clicking in the gap
        between words (or moving the cursor with arrow-nav) is respected, not
        just the last word explicitly clicked. Strictly inside a word -> before
        or after it depending which half the cursor sits in; in a gap ->
        right after the nearest word before it (or before the first word if the
        cursor sits before everything)."""
        widget = self.focus_get()
        if widget is self._transcript_text:
            self._resync_all_word_tags(widget)
            idx = widget.index("insert")
            hit = self._word_tag_containing(widget, idx)
            if hit is not None:
                li, wi = hit
                start = self._live_tag_range(widget, li, wi)[0]
                count = widget.count(start, idx, "chars")
                offset = count[0] if count else 0
                word = self._transcript.lines[li].words[wi]
                return (li, wi if offset <= len(word.text) // 2 else wi + 1)
            before = self._word_at_or_before(widget, idx)
            if before is not None:
                li, wi = before
                return (li, wi + 1)
            after = self._word_at_or_after(widget, idx)
            if after is not None:
                return after
        if self._sel is not None and self._sel[0] == "word":
            _, li, wi = self._sel
            word = self._transcript.lines[li].words[wi]
            return (li, wi if self._sel_char_offset <= len(word.text) // 2 else wi + 1)
        return None

    def _word_at_or_before(self, widget, idx):
        """(li, wi) of the word tag ending at or before `idx` whose end is
        CLOSEST to it, or None."""
        best, best_end = None, None
        for li, wi in self._word_ranges:
            rng = self._live_tag_range(widget, li, wi)
            if rng is None or not widget.compare(rng[1], "<=", idx):
                continue
            if best_end is None or widget.compare(rng[1], ">", best_end):
                best, best_end = (li, wi), rng[1]
        return best

    def _word_at_or_after(self, widget, idx):
        """(li, wi) of the word tag starting at or after `idx` whose start is
        CLOSEST to it, or None."""
        best, best_start = None, None
        for li, wi in self._word_ranges:
            rng = self._live_tag_range(widget, li, wi)
            if rng is None or not widget.compare(rng[0], ">=", idx):
                continue
            if best_start is None or widget.compare(rng[0], "<", best_start):
                best, best_start = (li, wi), rng[0]
        return best

    def _do_add_nonspeech(self):
        t0 = self._playhead_t
        self._open_word_form("Add non-speech entry", "[*laughter*]", t0, t0 + 1.5,
                             self._add_event_commit)

    def _add_event_commit(self, text, s, e):
        if not text.strip():
            return
        self._push_undo()
        subtitles.add_event(self._transcript, text.strip(), s, e, kind="manual")
        self._clear_selection()
        self._after_edit()

    def _on_confirm_gap(self):
        if self._sel is None or self._sel[0] != "event":
            return
        _, idx = self._sel
        ev = self._transcript.events[idx]
        self._open_word_form(
            "Label this stretch", "[*laughter*]", ev.start, ev.end,
            lambda text, s, e: self._confirm_gap_commit(idx, text, s, e))

    def _confirm_gap_commit(self, idx, text, s, e):
        if not text.strip() or idx >= len(self._transcript.events):
            return
        self._push_undo()
        subtitles.confirm_gap(self._transcript, idx, text.strip())
        ev = self._transcript.events[idx]
        ev.start, ev.end = s, e
        self._clear_selection()
        self._after_edit()

    def _on_dismiss_gap(self):
        if self._sel is None or self._sel[0] != "event":
            return
        _, idx = self._sel
        self._push_undo()
        subtitles.delete_event(self._transcript, idx)
        self._clear_selection()
        self._after_edit()

    def _delete_selected_event(self):
        if self._sel is None or self._sel[0] != "event":
            return
        _, idx = self._sel
        self._push_undo()
        subtitles.delete_event(self._transcript, idx)
        self._clear_selection()
        self._after_edit()

    def _open_word_form(self, title, default_text, default_start, default_end, on_submit):
        top = tk.Toplevel(self)
        top.title(title)
        top.configure(bg=PALETTE["bg"])
        top.resizable(False, False)
        top.transient(self.winfo_toplevel())

        wrap = tk.Frame(top, bg=PALETTE["bg"])
        wrap.pack(fill="both", expand=True, padx=16, pady=14)
        tk.Label(wrap, text=title, bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=display_font(13)).pack(anchor="w", pady=(0, 8))

        text_var = tk.StringVar(value=default_text)
        entry = ttk.Entry(wrap, textvariable=text_var, width=28)
        entry.pack(fill="x", pady=(0, 8))
        entry.focus_set()
        entry.selection_range(0, "end")

        times = tk.Frame(wrap, bg=PALETTE["bg"])
        times.pack(fill="x", pady=(0, 10))
        tk.Label(times, text="Start", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(side="left")
        start_var = tk.StringVar(value=self._fmt_mmssff(default_start))
        ttk.Entry(times, textvariable=start_var, width=9, font=mono_font(9)).pack(
            side="left", padx=(4, 12))
        tk.Label(times, text="End", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(side="left")
        end_var = tk.StringVar(value=self._fmt_mmssff(default_end))
        ttk.Entry(times, textvariable=end_var, width=9, font=mono_font(9)).pack(
            side="left", padx=(4, 0))

        def submit(_e=None):
            s = self._parse_mmssff(start_var.get())
            e = self._parse_mmssff(end_var.get())
            if s is None or e is None:
                return
            top.destroy()
            on_submit(text_var.get(), s, e)

        btnrow = tk.Frame(wrap, bg=PALETTE["bg"])
        btnrow.pack(fill="x")
        themed_button(btnrow, "Cancel", top.destroy, kind="neutral").pack(
            side="right", padx=(6, 0))
        themed_button(btnrow, "Add", submit, kind="primary").pack(side="right")
        entry.bind("<Return>", submit)
        top.bind("<Escape>", lambda _e: top.destroy())

        top.update_idletasks()
        try:
            px, py = self.winfo_rootx(), self.winfo_rooty()
            pw, ph = self.winfo_width(), self.winfo_height()
            w, h = top.winfo_width(), top.winfo_height()
            top.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")
        except tk.TclError:
            pass
        top.grab_set()

    # ── Confidence review ──────────────────────────────────────────────────────────────
    def _on_review_clicked(self):
        if not self._flagged_queue:
            return
        self._review_idx = (self._review_idx + 1) % len(self._flagged_queue)
        li, wi = self._flagged_queue[self._review_idx]
        # 6.5 B5: the same seek+select+scroll-into-view path arrow-nav uses -
        # this used to only seek, so a flagged word off-screen in the text pane
        # never actually scrolled into view.
        self._goto_word(li, wi)

    def _review_keep(self):
        """Keep = "I checked this word, it's right." Sets conf to the same 1.0
        sentinel a hand-inserted word gets - it stops flagging everywhere (render,
        queue, timeline) immediately AND for good (persisted in the saved
        transcript), not just for this session."""
        if self._sel is not None and self._sel[0] == "word":
            _, li, wi = self._sel
            self._push_undo()
            self._transcript.lines[li].words[wi].conf = 1.0
            self._debounced_save()
        self._clear_selection()
        self._continue_review_if_any()

    def _review_delete(self):
        if self._sel is None or self._sel[0] != "word":
            return
        _, li, wi = self._sel
        self._push_undo()
        subtitles.delete_word(self._transcript, li, wi)
        self._clear_selection()
        self._rebuild_flagged_queue()
        self._render_editor()
        self._debounced_save()
        self._continue_review_if_any()

    def _continue_review_if_any(self):
        self._rebuild_flagged_queue()
        self._render_editor()
        if self._flagged_queue:
            self._review_idx = -1
            self._on_review_clicked()
        else:
            self._say("All flagged words reviewed.", "green")

    # ── Undo / redo ────────────────────────────────────────────────────────────────────
    def _push_undo(self):
        if self._transcript is None:
            return
        snap = self._transcript.to_dict()
        # A typing burst already snapshotted this exact state when its first key
        # went down; the structural edit that key then triggers (a split, a
        # merge) must not add a second, identical step.
        if self._undo_stack and self._undo_stack[-1] == snap:
            self._redo_stack.clear()
            return
        self._undo_stack.append(snap)
        if len(self._undo_stack) > _UNDO_CAP:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _do_undo(self):
        # Typing still waiting on its debounce is part of the current state:
        # write it in first, or redo would lose it and the re-render would paste
        # it onto the restored transcript.
        self._flush_pending_typing()
        self._typing_burst = False
        if not self._undo_stack or self._transcript is None:
            return
        self._redo_stack.append(self._transcript.to_dict())
        prev = self._undo_stack.pop()
        self._transcript = subtitles.Transcript.from_dict(prev)
        self._clear_selection()
        self._rebuild_flagged_queue()
        self._render_editor()
        self._debounced_save()

    def _do_redo(self):
        self._flush_pending_typing()
        self._typing_burst = False
        if not self._redo_stack or self._transcript is None:
            return
        self._undo_stack.append(self._transcript.to_dict())
        nxt = self._redo_stack.pop()
        self._transcript = subtitles.Transcript.from_dict(nxt)
        self._clear_selection()
        self._rebuild_flagged_queue()
        self._render_editor()
        self._debounced_save()

    # ── Debounced per-clip save (same pattern as the editor's clip_edits autosave) ────
    def _commit_now(self):
        """Write any pending typing into the model and the model to disk, skipping
        both debounces. Anything that hands the transcript to another reader (the
        editor's render reads it from disk) or ends the session calls this first,
        or the last half second of edits would be missing."""
        try:
            self._flush_pending_typing()
        except tk.TclError:
            pass   # widget already gone during shutdown; the model is what counts
        if self._save_job is not None:
            self.after_cancel(self._save_job)
            self._save_job = None
        self._flush_save()

    def _debounced_save(self):
        if self._save_job is not None:
            self.after_cancel(self._save_job)
        self._save_job = self.after(_SAVE_DEBOUNCE_MS, self._flush_save)

    def _flush_save(self):
        self._save_job = None
        if self._transcript is None or self._clip_key is None:
            return
        existing = subtitles.load_transcript(self._clip_key)
        if subtitles.would_shrink_to_empty(self._transcript, existing):
            return
        subtitles.save_transcript(self._clip_key, self._transcript,
                                  cap=config_mod.clip_memory(self.app.config))

    # ── Current-word highlight + timeline strip ───────────────────────────────────────
    def _apply_cur_tag(self):
        """The current-word highlight, over the word's LIVE extent.

        It used to read the cached range from render time, which goes stale the
        moment typing changes that word's length - so after editing a word the
        green highlight covered only part of it until the next full re-render
        (6.5c). Same reason _apply_sel_tag re-queries the tag."""
        text = self._transcript_text
        text.tag_remove("cur", "1.0", "end")
        if self._cur_word_key is None:
            return
        rng = (self._live_tag_range(text, *self._cur_word_key)
               or self._word_ranges.get(self._cur_word_key))
        if rng is not None:
            text.tag_add("cur", rng[0], rng[1])

    def _highlight_current_word(self, t):
        """Highlights the current word in the transcript text. The preview's own
        caption is now the actual burned-in subtitle composited by
        _render_preview_frame (6.4b) - no separate plain-text caption label to
        keep in sync (it would show a different, unstyled look right next to the
        real one)."""
        key = None
        offset_ms = self.app.config.get("subtitles", {}).get("timing_offset_ms", 0)
        if self._transcript is not None:
            for li, line in enumerate(self._transcript.lines):
                for wi, w in enumerate(line.words):
                    s = subtitles.apply_offset(w.start, offset_ms)
                    e = subtitles.apply_offset(w.end, offset_ms)
                    if s <= t < e:
                        key = (li, wi)
                        break
                if key is not None:
                    break
        if key == self._cur_word_key:
            return
        self._cur_word_key = key
        self._apply_cur_tag()
        if key is not None:
            rng = self._word_ranges.get(key)
            if rng is not None:
                try:
                    self._transcript_text.see(rng[0])
                except tk.TclError:
                    pass

    # ── Timeline: whole-clip ticks, or a zoomed, draggable word/gap-neighborhood ─────
    def _flat_words(self):
        """Every word as (li, wi, Word), in time order - the document order already
        matches this for a normally-transcribed clip, but sorting keeps it correct
        after edits (e.g. a manually inserted word)."""
        if self._transcript is None:
            return []
        out = [(li, wi, w) for li, line in enumerate(self._transcript.lines)
              for wi, w in enumerate(line.words)]
        out.sort(key=lambda item: item[2].start)
        return out

    def _tl_zoom_center_t(self):
        """The zoom window's un-panned anchor TIME.

        While playing, that is ALWAYS the playhead - the window used to stay
        anchored on whatever word was selected, so the playhead ran straight off
        the edge, which is why it sometimes seemed not to move at all (6.5c).
        "Stay at word" does not change this: it only makes STOPPING return to
        the selected word, so you can play the same spot over and over without
        losing your place."""
        if self._playing or self._tl_scrubbing or self._tl_follow_playhead:
            return self._playhead_t
        if self._sel is not None and self._transcript is not None:
            if self._sel[0] == "word":
                _, li, wi = self._sel
                if (li < len(self._transcript.lines)
                        and wi < len(self._transcript.lines[li].words)):
                    return self._transcript.lines[li].words[wi].start
            elif self._sel[0] == "event":
                _, idx = self._sel
                if idx < len(self._transcript.events):
                    return self._transcript.events[idx].start
        return self._playhead_t

    def _tl_zoom_bounds(self):
        """(lo_t, hi_t) - a fixed _TL_ZOOM_WINDOW_S-second window centered on the
        selection/playhead, shifted by the continuous pan offset (seconds) and
        clamped to the clip's duration.

        A timing drag FREEZES the window (6.5c). Without that, the window
        re-centres on the selected word's start every redraw, so dragging that
        word moved the window under the mouse: the pixel-to-time mapping shifted
        between motion events, the word stayed pinned near the centre, and the
        stored time ratcheted away from the cursor. That is the real cause of a
        drag that looks like it did nothing."""
        if self._tl_drag_window is not None:
            return self._tl_drag_window
        center_t = self._tl_zoom_center_t() + self._tl_pan_offset
        half = _TL_ZOOM_WINDOW_S / 2.0
        lo_t, hi_t = center_t - half, center_t + half
        dur = self._cut_len_s
        if dur > 0:
            if hi_t > dur:
                hi_t, lo_t = dur, dur - _TL_ZOOM_WINDOW_S
            if lo_t < 0.0:
                lo_t, hi_t = 0.0, min(dur, _TL_ZOOM_WINDOW_S)
        elif lo_t < 0.0:
            lo_t, hi_t = 0.0, _TL_ZOOM_WINDOW_S
        return lo_t, hi_t

    def _zoom_window_words(self):
        """Every word overlapping the current zoom window's time range - the
        window itself is fixed-duration and time-based (see _tl_zoom_bounds),
        so which words fall inside it changes continuously as the window pans,
        not in discrete word-count jumps (6.5 live-test fix)."""
        if self._tl_window is None:
            return []
        lo_t, hi_t = self._tl_window
        return [(li, wi, w) for li, wi, w in self._flat_words()
               if w.end >= lo_t and w.start <= hi_t]

    def _zoom_window_events(self):
        """Confirmed (manual) events overlapping the current zoom window - drawn
        and draggable alongside words (6.5 C1: a confirmed gap used to be
        invisible and un-retimeable on the timeline). Unconfirmed gap_candidate
        suggestions stay off the timeline until confirmed, matching how they
        render nothing in the actual burn-in either."""
        if self._tl_window is None or self._transcript is None:
            return []
        lo_t, hi_t = self._tl_window
        return [(idx, ev) for idx, ev in enumerate(self._transcript.events)
               if ev.kind == "manual" and ev.end >= lo_t and ev.start <= hi_t]

    def _sync_zoom_ui(self):
        """Button label + the hint line, both derived from _tl_zoom so the initial
        build and the toggle can never disagree about which view is showing."""
        self._zoom_btn.configure(text=f"Zoom: {'on' if self._tl_zoom else 'off'}")
        self._tl_note_lbl.configure(
            text=("Drag a word to move it. Its start handle is top-left, its end "
                  "handle bottom-right. Drag the grey bar at the bottom to scrub "
                  "through the clip.")
                 if self._tl_zoom else
                 "Ticks mark word starts; the green line is the playhead.")

    def _toggle_zoom(self):
        self._tl_zoom = not self._tl_zoom
        self._tl_pan_offset = 0
        self._sync_zoom_ui()
        if self._tl_zoom:
            self._ensure_tl_wave_ready()
        self._redraw_timeline_strip(self._playhead_t)

    def _ensure_tl_wave_ready(self):
        """Proactively load the cut audio + compute waveform peaks for the
        timeline's waveform row (6.5 C1), reusing the same PCM cache the player
        uses - not gated behind pressing Play."""
        if self._transcript is None:
            return
        if (self._tl_wave_peaks is not None and self._tl_wave_for_key == self._clip_key
                and self._tl_wave_for_fp == self._transcript.cut_fingerprint):
            return
        self._ensure_wav_cache(self._tl_wave_from_pcm)

    def _tl_wave_from_pcm(self):
        if self._pcm is None or self._pcm_rate <= 0:
            return
        self._tl_wave_peaks = video_utils.peaks_from_pcm(self._pcm, buckets=2000)
        self._tl_wave_for_key, self._tl_wave_for_fp = self._wav_for_key, self._wav_for_fp
        if self._tl_zoom:
            self._redraw_timeline_strip(self._playhead_t)

    def _redraw_timeline_strip(self, t):
        c = self._timeline_canvas
        c.delete("all")
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        cutoff = self.app.config.get("subtitles", {}).get("flag_confidence", 0.6)
        if self._tl_zoom and self._transcript is not None and self._flat_words():
            lo_t, hi_t = self._tl_zoom_bounds()
            self._tl_window = (lo_t, hi_t)
            span = max(1e-6, hi_t - lo_t)
            style = subtitles.SubtitleStyle.from_dict(
                self.app.config.get("subtitles", {}).get("style"))
            self._tl_chip_photos = []
            self._tl_hits = []
            self._draw_pan_bar(c, w)
            for li, wi, word in self._zoom_window_words():
                x0 = (word.start - lo_t) / span * w
                x1 = (word.end - lo_t) / span * w
                self._tl_hits.append((("word", li, wi), x0, x1))
                flagged = subtitles.is_flagged(word, cutoff)
                is_sel = self._sel == ("word", li, wi)
                if getattr(word, "added", False):
                    color = PALETTE["green"]
                else:
                    color = PALETTE["yellow"] if flagged else PALETTE["blue"]
                self._draw_tl_handles(c, x0, x1, PALETTE["text"] if is_sel else color)
                c.create_rectangle(x0, _TL_BODY_Y0, max(x0 + 2, x1), _TL_BODY_Y1,
                                   fill=color,
                                   outline=(PALETTE["text"] if is_sel else color))
                # 6.4c: the word rendered in the CURRENT style on top of its
                # rectangle - timing edits are made against the real look, not
                # a flat color bar. The rectangle stays the drag hit-target;
                # this is purely a styled overlay (dragging is unchanged).
                self._draw_timing_word_chip(c, word, style, x0, x1,
                                           _TL_BODY_Y0, _TL_BODY_Y1)
            for idx, ev in self._zoom_window_events():
                x0 = (ev.start - lo_t) / span * w
                x1 = (ev.end - lo_t) / span * w
                self._tl_hits.append((("event", idx), x0, x1))
                is_sel = self._sel == ("event", idx)
                color = PALETTE["text"] if is_sel else PALETTE["purple"]
                self._draw_tl_handles(c, x0, x1, color)
                c.create_rectangle(x0, _TL_BODY_Y0, max(x0 + 2, x1), _TL_BODY_Y1,
                                   fill=PALETTE["purple"], outline=color)
                self._draw_timing_event_chip(c, ev, style, x0, x1,
                                            _TL_BODY_Y0, _TL_BODY_Y1)
            self._draw_wave_row(c, w, lo_t, hi_t)
            if lo_t <= t <= hi_t:
                x = (t - lo_t) / span * w
                c.create_line(x, 0, x, h, fill=PALETTE["green"], width=2)
            return
        self._tl_window = None
        self._tl_hits = []
        dur = self._cut_len_s
        if dur > 0 and self._transcript is not None:
            for word in self._transcript.words():
                x = int(min(1.0, max(0.0, word.start / dur)) * w)
                flagged = subtitles.is_flagged(word, cutoff)
                if getattr(word, "added", False):
                    color = PALETTE["green"]
                else:
                    color = PALETTE["yellow"] if flagged else "#2f4a5e"
                c.create_line(x, 3, x, h - 3, fill=color, width=(2 if flagged else 1))
            for ev in self._transcript.events:
                x = int(min(1.0, max(0.0, ev.start / dur)) * w)
                c.create_line(x, 3, x, h - 3, fill=PALETTE["purple"], width=2)
        if dur > 0:
            x = int(min(1.0, max(0.0, t / dur)) * w)
            c.create_line(x, 0, x, h, fill=PALETTE["green"], width=2)

    def _draw_pan_bar(self, c, w):
        """The scrub bar along the BOTTOM (v1.1): the whole clip end to end, a
        thumb showing the zoomed window, and the playhead. Press or drag it to
        move the playhead anywhere in the clip; the window stays centred on the
        playhead while you do, exactly like during playback. Neutral grey with
        grip marks (blue read as a word chip), brighter while hovered/dragged."""
        y0, y1 = _TL_SCRUB_Y0, _TL_SCRUB_Y1
        round_rect(c, 1, y0, w - 1, y1, 6, fill=PALETTE["panel"], outline="")
        dur = self._cut_len_s
        if dur <= 0 or self._tl_window is None:
            return
        lo_t, hi_t = self._tl_window
        x0 = max(0.0, lo_t / dur) * w
        x1 = min(1.0, hi_t / dur) * w
        if x1 - x0 < 28:   # always big enough to see and grab
            mid = (x0 + x1) / 2.0
            x0, x1 = max(0.0, mid - 14), min(float(w), mid + 14)
        hot = self._tl_scrub_hover or self._tl_scrubbing
        thumb = "#d6d6d6" if hot else "#8c8c8c"
        round_rect(c, x0, y0 + 1, x1, y1 - 1, 6, fill=thumb, outline="")
        mid, ym = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        for dx in (-4, 0, 4):
            c.create_line(mid + dx, ym - 3, mid + dx, ym + 4, fill=PALETTE["deep"], width=1)
        px = min(1.0, max(0.0, self._playhead_t / dur)) * w
        c.create_line(px, y0 - 1, px, y1 + 1, fill=PALETTE["green"], width=2)

    def _tl_scrub_to(self, x):
        """Move the playhead to the clip time under x on the scrub bar. The
        target updates at once; the seek itself (timeline redraw, current-word
        highlight, preview decode) is throttled."""
        dur = self._cut_len_s
        if dur <= 0:
            return
        w = max(1, self._timeline_canvas.winfo_width())
        self._tl_scrub_t = min(dur, max(0.0, x / w * dur))
        if self._tl_scrub_job is None:
            self._tl_scrub_job = self.after(_TL_SCRUB_THROTTLE_MS, self._fire_tl_scrub)

    def _fire_tl_scrub(self):
        self._tl_scrub_job = None
        self._seek_to(self._tl_scrub_t)

    _TL_HANDLE_LEN = 8   # px each corner bracket runs along its own row

    def _draw_tl_handles(self, c, x0, x1, color):
        """A span's grab markers: START at its top-left, END at its bottom-right,
        on separate rows (6.5c). Each reads as a corner bracket on the chip, and
        two neighbouring spans can never stack their handles on top of one
        another the way one shared band allowed."""
        c.create_rectangle(x0, _TL_START_HANDLE_Y0, x0 + self._TL_HANDLE_LEN,
                          _TL_START_HANDLE_Y1, fill=color, outline="")
        c.create_rectangle(max(x0, x1 - self._TL_HANDLE_LEN), _TL_END_HANDLE_Y0, x1,
                          _TL_END_HANDLE_Y1, fill=color, outline="")

    def _draw_wave_row(self, c, w, lo_t, hi_t):
        """The cut clip's audio level under the zoomed word/gap row (6.5 C1),
        from the SAME PCM the player already caches - reads speech energy
        against the words while adjusting their timing."""
        peaks = self._tl_wave_peaks
        if peaks is None or self._pcm is None or self._pcm_rate <= 0:
            return
        total_dur = len(self._pcm) / self._pcm_rate
        if total_dur <= 0:
            return
        n = len(peaks)
        y_mid = (_TL_WAVE_Y0 + _TL_WAVE_Y1) / 2.0
        half_h = (_TL_WAVE_Y1 - _TL_WAVE_Y0) / 2.0 - 1
        span = max(1e-6, hi_t - lo_t)
        i0 = max(0, int(lo_t / total_dur * n))
        i1 = min(n, int(hi_t / total_dur * n) + 1)
        if i1 <= i0:
            return
        bar_w = max(1.0, w / (i1 - i0))
        for i in range(i0, i1):
            peak_t = i / n * total_dur
            x = (peak_t - lo_t) / span * w
            bar_h = max(2.0, float(peaks[i]) * half_h)
            c.create_rectangle(x, y_mid - bar_h, x + bar_w, y_mid + bar_h,
                               fill=PALETTE["text_dim"], outline="")

    def _draw_timing_word_chip(self, canvas, word, style, x0, x1, y0, y1):
        """A small render of `word` in the CURRENT style, centered on its
        rectangle (6.4c styled Timing view). Font size is capped
        (_TIMING_CHIP_SIZE_CAP) independent of the real style size, so the chip
        always fits the timeline strip regardless of the user's chosen text size.
        Any render failure just skips the chip - the rectangle underneath already
        conveys the word's span."""
        try:
            k = _TIMING_CHIP_SIZE_CAP / max(1, style.size)
            img = subtitles.render_word_chip(word, style, k=k)
            photo = ImageTk.PhotoImage(img)
        except Exception:  # noqa: BLE001 - a chip render must never break the timeline
            return
        self._tl_chip_photos.append(photo)
        canvas.create_image((x0 + x1) / 2, (y0 + y1) / 2, image=photo, anchor="center")

    def _draw_timing_event_chip(self, canvas, ev, style, x0, x1, y0, y1):
        """Same styled-chip treatment as a word, for a confirmed event's label
        text (6.5 C1) - reuses render_word_chip via a throwaway Word, the same
        trick shared.subtitles._event_at uses for the real burn-in."""
        w = subtitles.Word(text=ev.text, start=ev.start, end=ev.end, conf=1.0)
        self._draw_timing_word_chip(canvas, w, style, x0, x1, y0, y1)

    def _tl_hit_test(self, x, y):
        """What a press at (x, y) grabs in the zoomed timeline, as
        (target, "start"|"end"|"move") or None. `target` is ("word", li, wi) or
        ("event", idx). Geometry comes from the last redraw's hit cache, so the
        grab zones are exactly what is drawn (and no per-event re-sort of the
        whole transcript, which a hover handler would pay on every mouse move).

        Each row does exactly one job, so nothing is ambiguous: the top row
        grabs start handles, the bottom row grabs end handles, and the chip
        itself moves the whole span (6.5c)."""
        if _TL_START_HANDLE_Y0 <= y <= _TL_START_HANDLE_Y1:
            return self._nearest_edge(x, "start")
        if _TL_END_HANDLE_Y0 <= y <= _TL_END_HANDLE_Y1:
            return self._nearest_edge(x, "end")
        if _TL_BODY_Y0 <= y <= _TL_BODY_Y1:
            for target, x0, x1 in self._tl_hits:
                if x0 <= x <= x1:
                    return target, "move"
        return None

    def _nearest_edge(self, x, edge):
        """The closest `edge` handle to `x` within the grab tolerance, allowing
        for the bracket's own length so the whole drawn marker is clickable."""
        best, best_d = None, _TL_HANDLE_HIT_TOL + self._TL_HANDLE_LEN
        for target, x0, x1 in self._tl_hits:
            ex = x0 if edge == "start" else x1
            reach = (x - ex) if edge == "start" else (ex - x)
            d = abs(ex - x)
            if -_TL_HANDLE_HIT_TOL <= reach <= self._TL_HANDLE_LEN and d < best_d:
                best, best_d = (target, edge), d
        return best

    def _tl_time_at(self, x):
        """Canvas x -> time, against the window currently being drawn."""
        if self._tl_window is None:
            return None
        lo_t, hi_t = self._tl_window
        w = max(1, self._timeline_canvas.winfo_width())
        return lo_t + max(0, min(w, x)) / w * max(1e-6, hi_t - lo_t)

    def _tl_span_of(self, target):
        """(start, end) of a hit-cache target, or None if it no longer exists."""
        if self._transcript is None:
            return None
        if target[0] == "word":
            _, li, wi = target
            if li < len(self._transcript.lines) and wi < len(self._transcript.lines[li].words):
                w = self._transcript.lines[li].words[wi]
                return w.start, w.end
        else:
            _, idx = target
            if idx < len(self._transcript.events):
                ev = self._transcript.events[idx]
                return ev.start, ev.end
        return None

    def _tl_on_hover(self, event):
        """Cursor feedback so the two grab zones read differently: a resize
        cursor on an edge handle, a move cursor on a chip body."""
        if not self._tl_zoom or self._tl_drag is not None or self._tl_scrubbing:
            return
        on_scrub = _TL_SCRUB_Y0 - 2 <= event.y <= _TL_SCRUB_Y1 + 2
        if on_scrub != self._tl_scrub_hover:
            self._tl_scrub_hover = on_scrub
            self._schedule_tl_redraw()
        if on_scrub:
            cursor = "sb_h_double_arrow"
        else:
            hit = self._tl_hit_test(event.x, event.y)
            if hit is None:
                cursor = ""
            else:
                cursor = "sb_h_double_arrow" if hit[1] in ("start", "end") else "fleur"
        # (the two handle rows and the chip row each report their own cursor, so
        # which of the three things a press will do is visible before pressing)
        if cursor != self._tl_cursor:
            self._tl_cursor = cursor
            self._timeline_canvas.configure(cursor=cursor)

    def _tl_on_right_click(self, event):
        """Right-click the timeline to create something at that exact spot
        (6.5c) - the main way to add a word, a phrase, or a non-speech entry,
        because it puts the new item where it belongs on the clock instead of
        wherever the transcript happens to end."""
        if self._transcript is None:
            self._say("Transcribe the clip first.")
            return
        t = self._tl_time_at(event.x) if self._tl_zoom else self._tl_full_time_at(event.x)
        if t is None:
            return
        menu = tk.Menu(self, tearoff=0, bg=PALETTE["panel"], fg=PALETTE["text"],
                       activebackground=PALETTE["green"],
                       activeforeground=PALETTE["deep"],
                       bd=0, relief="flat", font=ui_font(9))
        menu.add_command(label=f"New word or phrase at {self._fmt_mmssff(t)}",
                        command=lambda: self._prompt_new_at(t, speech=True))
        menu.add_command(label=f"New non-speech at {self._fmt_mmssff(t)}",
                        command=lambda: self._prompt_new_at(t, speech=False))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _tl_full_time_at(self, x):
        """Canvas x -> time on the un-zoomed, whole-clip strip."""
        dur = self._cut_len_s
        if dur <= 0:
            return None
        w = max(1, self._timeline_canvas.winfo_width())
        return max(0.0, min(dur, x / w * dur))

    def _prompt_new_at(self, t, speech):
        """Ask what to add, then place it at `t` with a span suited to its
        length. One word makes a word; several make a phrase - each word its
        own, sharing the span, so the reveal style groups them like any other
        run of words."""
        title = "New word or phrase" if speech else "New non-speech entry"
        default = "" if speech else "[*laughter*]"
        self._open_text_prompt(
            title, default,
            lambda text: self._create_at(t, text, speech))

    def _create_at(self, t, text, speech):
        text = (text or "").strip()
        if not text:
            return
        self._push_undo()
        if not speech:
            span = subtitles.natural_span(text)
            subtitles.add_event(self._transcript, text, t, t + span)
            idx = next((i for i, ev in enumerate(self._transcript.events)
                       if ev.start == t and ev.text == text), None)
            self._after_edit()
            if idx is not None:
                self._select_event(idx)   # _select_event already scrolls to it
            self._say(f"Added {text!r} at {self._fmt_mmssff(t)}.", "green")
            return
        tokens = text.split()
        li, wi = self._insert_point_for(t)
        if li is None:
            self._transcript.lines.append(subtitles.Line(words=[]))
            li, wi = len(self._transcript.lines) - 1, 0
        cursor = t
        for n, token in enumerate(tokens):
            span = subtitles.natural_span(token)
            subtitles.insert_word(self._transcript, li, wi + n, token,
                                 cursor, cursor + span)
            self._transcript.lines[li].words[wi + n].added = True
            cursor += span
        self._after_edit()
        self._goto_word(li, wi)   # select it and scroll it into view
        self._seek_to(t)
        self._say(f"Added {len(tokens)} word(s) at {self._fmt_mmssff(t)}.", "green")

    def _insert_point_for(self, t):
        """(li, wi) where a word starting at `t` belongs, in time order. Returns
        (None, None) when there is no line to put it in yet."""
        best = None
        for li, line in enumerate(self._transcript.lines):
            for wi, word in enumerate(line.words):
                if word.start <= t:
                    best = (li, wi + 1)
                elif best is None:
                    return (li, wi)
        if best is not None:
            return best
        return (None, None)

    def _open_text_prompt(self, title, default, on_submit):
        """A one-field prompt. Times are not asked for - the new item is placed
        where the click was and sized from its text, then dragged on the
        timeline if it needs moving (6.5c: no more typing timecodes)."""
        top = tk.Toplevel(self)
        top.title(title)
        top.configure(bg=PALETTE["bg"])
        top.resizable(False, False)
        top.transient(self.winfo_toplevel())

        wrap = tk.Frame(top, bg=PALETTE["bg"])
        wrap.pack(fill="both", expand=True, padx=16, pady=14)
        tk.Label(wrap, text=title, bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=display_font(13)).pack(anchor="w", pady=(0, 8))
        var = tk.StringVar(value=default)
        entry = ttk.Entry(wrap, textvariable=var, width=30)
        entry.pack(fill="x", pady=(0, 4))
        entry.focus_set()
        entry.selection_range(0, "end")
        tk.Label(wrap, text="Its length is set from what you type. Drag it on the "
                            "timeline to move or resize it.",
                 bg=PALETTE["bg"], fg=PALETTE["text_faint"], font=ui_font(8),
                 wraplength=260, justify="left").pack(anchor="w", pady=(0, 10))

        def submit(_e=None):
            value = var.get()
            top.destroy()
            on_submit(value)

        btnrow = tk.Frame(wrap, bg=PALETTE["bg"])
        btnrow.pack(fill="x")
        themed_button(btnrow, "Add", submit, kind="primary").pack(side="right")
        themed_button(btnrow, "Cancel", top.destroy, kind="neutral").pack(
            side="right", padx=(0, 6))
        entry.bind("<Return>", submit)
        top.bind("<Escape>", lambda _e: top.destroy())
        top.grab_set()

    def _tl_on_press(self, event):
        if not self._tl_zoom or self._transcript is None:
            return
        if _TL_SCRUB_Y0 - 2 <= event.y <= _TL_SCRUB_Y1 + 2:
            self._tl_scrubbing = True
            self._tl_follow_playhead = True
            self._tl_pan_offset = 0
            self._tl_scrub_to(event.x)
            return
        hit = self._tl_hit_test(event.x, event.y)
        if hit is None:
            return
        target, mode = hit
        span = self._tl_span_of(target)
        if span is None:
            return
        t = self._tl_time_at(event.x)
        if t is None:
            return
        # Freeze the window BEFORE anything can change the selection: selecting
        # the dragged span would otherwise re-anchor the zoom window under the
        # cursor (see _tl_zoom_bounds).
        self._tl_drag_window = self._tl_window
        self._tl_drag_grab_dt = t - span[0]
        self._push_undo()
        self._tl_drag = (target, mode)
        self._tl_select_target(target)

    def _tl_select_target(self, target):
        """Select the span being dragged, cheaply: the selection bar's Start/End
        fields are the live numeric readout for the drag, so they have to be
        showing THIS span. Deliberately not _select_word (a full re-render) or
        _select_word_in_place (which zeroes the pan offset and would jump the
        window mid-press)."""
        if self._sel == target:
            return
        self._sel = target
        self._sel_char_offset = 0
        self._render_selection_bar()
        self._apply_sel_tag()

    def _tl_on_drag(self, event):
        if self._tl_scrubbing:
            self._tl_scrub_to(event.x)
            return
        if self._tl_drag is None or self._tl_window is None:
            return
        t = self._tl_time_at(event.x)
        if t is None:
            return
        target, mode = self._tl_drag
        kind = target[0]
        if kind == "word":
            _, li, wi = target
            if mode == "move":
                subtitles.move_word_time(self._transcript, li, wi,
                                        t - self._tl_drag_grab_dt)
            elif mode == "start":
                subtitles.nudge_word_time(self._transcript, li, wi, start=t)
            else:
                subtitles.nudge_word_time(self._transcript, li, wi, end=t)
        else:
            _, idx = target
            if mode == "move":
                subtitles.move_event_time(self._transcript, idx,
                                         t - self._tl_drag_grab_dt)
            elif mode == "start":
                subtitles.nudge_event_time(self._transcript, idx, start=t)
            else:
                subtitles.nudge_event_time(self._transcript, idx, end=t)
        # The model is already updated; only the DRAWING is throttled.
        self._schedule_tl_redraw()
        self._schedule_tl_preview()

    def _schedule_tl_redraw(self):
        if self._tl_redraw_job is not None:
            return   # throttle: an in-flight job repaints the LATEST state when it fires
        self._tl_redraw_job = self.after(_TL_DRAG_REDRAW_MS, self._fire_tl_redraw)

    def _fire_tl_redraw(self):
        self._tl_redraw_job = None
        self._redraw_timeline_strip(self._playhead_t)

    def _schedule_tl_preview(self):
        # Its own timer slot, never shared with the redraw throttle - sharing one
        # was what silently capped the editor's preview during drags.
        if self._tl_preview_job is not None:
            self.after_cancel(self._tl_preview_job)
        self._tl_preview_job = self.after(_TL_DRAG_SETTLE_MS, self._fire_tl_preview)

    def _fire_tl_preview(self):
        self._tl_preview_job = None
        self._highlight_current_word(self._playhead_t)
        self._render_preview_frame(self._playhead_t)

    def _tl_on_release(self, _event):
        if self._tl_scrubbing:
            self._tl_scrubbing = False
            if self._tl_scrub_job is not None:
                self.after_cancel(self._tl_scrub_job)
                self._tl_scrub_job = None
            self._seek_to(self._tl_scrub_t)   # land exactly where the mouse let go
            return
        if self._tl_drag is None:
            return
        self._tl_drag = None
        self._tl_drag_window = None
        if self._tl_redraw_job is not None:
            self.after_cancel(self._tl_redraw_job)
            self._tl_redraw_job = None
        if self._tl_preview_job is not None:
            self.after_cancel(self._tl_preview_job)
            self._tl_preview_job = None
        job = getattr(self, "_sash_restore_job", None)
        if job is not None:
            self.after_cancel(job)
            self._sash_restore_job = None
        self._rebuild_flagged_queue()
        self._render_editor()
        self._debounced_save()
        self._highlight_current_word(self._playhead_t)
        self._render_preview_frame(self._playhead_t)

    # ── Playback (self-contained: own cut-audio cache + own sounddevice stream) ──────
    def _on_stay_toggle(self):
        self._tl_stay_at_word = bool(self._stay_var.get())
        self._tl_pan_offset = 0
        self._redraw_timeline_strip(self._playhead_t)

    def _toggle_play(self):
        if self._playing:
            self._stop_play()
        else:
            self._start_play()

    def _start_play(self):
        if self._transcript is None or self._subject is None:
            self._say("Load a clip and transcribe it first.")
            return
        self._ensure_wav_cache(self._start_play_after_wav)

    def _start_play_after_wav(self):
        if self._pcm is None or self._pcm_rate <= 0:
            self._say("No audio to play.")
            return
        dur = len(self._pcm) / self._pcm_rate
        if self._playhead_t >= dur:
            self._playhead_t = 0.0
        start_sample = int(self._playhead_t * self._pcm_rate)
        self._audio_pos = start_sample
        self._audio_start_sample = start_sample
        self._audio_seek = None
        self._play_t0 = self._playhead_t
        self._play_clock0 = perf_counter()
        self._playing = True
        self._play_btn.configure(text="⏸")
        self._start_audio()
        self._play_tick()

    def _snap_back_to_word(self):
        """With "stay at word" on, stopping returns the playhead to the selected
        word so the next play starts from the same place."""
        if not self._tl_stay_at_word or self._sel is None:
            return
        span = self._tl_span_of(self._sel)
        if span is not None:
            self._seek_to(span[0])

    def _stop_play(self):
        self._stop_audio()
        if self._play_job is not None:
            self.after_cancel(self._play_job)
            self._play_job = None
        self._playing = False
        if hasattr(self, "_play_btn"):
            self._play_btn.configure(text="▶")
        self._snap_back_to_word()

    def _play_tick(self):
        """6.4b: ticks at the clip's own fps (was a fixed ~15fps) and renders the
        preview frame EVERY tick - true-speed, audio-clock-driven, exactly like the
        editor's playback tick. What actually made the old preview choppy was the
        per-frame threading.Thread(extract_frame) opening+seeking a FRESH capture
        every call (the real cost is GOP-bound seek time); the
        persistent-capture read this now calls is cheap enough to run every tick
        without throttling. A gap between ticks bigger than one frame naturally
        "drops" the skipped frames - the clock decides `t`, `_render_preview_frame`
        maps it straight to whatever source frame that is."""
        self._play_job = None
        if not self._playing:
            return
        if self._stream is not None and self._pcm_rate > 0:
            t = self._play_t0 + (self._audio_pos - self._audio_start_sample) / self._pcm_rate
        else:
            t = self._play_t0 + (perf_counter() - self._play_clock0)
        dur = (len(self._pcm) / self._pcm_rate
              if (self._pcm is not None and self._pcm_rate > 0) else self._cut_len_s)
        if dur > 0 and t >= dur:
            self._playhead_t = dur
            self._stop_play()
            self._update_playhead_ui(dur)
            return
        self._playhead_t = t
        self._update_playhead_ui(t)
        tick_ms = max(1, int(1000 / self._fps)) if self._fps > 0 else _TICK_MS
        self._play_job = self.after(tick_ms, self._play_tick)

    def _update_playhead_ui(self, t):
        self._pos_lbl.configure(text=f"{self._fmt_tc(t)} / {self._fmt_tc(self._cut_len_s)}")
        self._highlight_current_word(t)
        self._redraw_timeline_strip(t)
        self._render_preview_frame(t)

    def _seek_to(self, t):
        self._playhead_t = max(0.0, t)
        if self._playing:
            if self._stream is not None and self._pcm_rate > 0:
                sample = int(self._playhead_t * self._pcm_rate)
                self._audio_seek = sample
                self._audio_start_sample = sample
            self._play_t0 = self._playhead_t
            self._play_clock0 = perf_counter()
        self._update_playhead_ui(self._playhead_t)

    def _ensure_wav_cache(self, then):
        if self._transcript is None or self._subject is None:
            return
        if (self._pcm is not None and self._wav_for_key == self._clip_key
                and self._wav_for_fp == self._transcript.cut_fingerprint):
            then()
            return
        cache_path = os.path.join(paths.get_path("transcripts"), self._clip_key + ".wav")
        subject, segs, fps, n = self._subject, self._segs_frames, self._fps, self._total_frames
        key, fp = self._clip_key, self._transcript.cut_fingerprint
        threading.Thread(
            target=self._wav_cache_worker,
            args=(cache_path, subject, segs, fps, n, key, fp, then),
            daemon=True).start()

    def _wav_cache_worker(self, cache_path, subject, segs, fps, n, key, fp, then):
        if fps > 0 and n > 0 and segs:
            whole = (len(segs) == 1 and segs[0][0] <= 0 and segs[0][1] >= n - 1)
            secs = None if whole else [(s / fps, (e + 1) / fps) for s, e in segs]
            video_utils.extract_cut_audio(subject, secs, cache_path)
        decoded = self._read_wav(cache_path)
        self._marshal(self._wav_cache_ready, key, fp, decoded, then)

    def _wav_cache_ready(self, key, fp, decoded, then):
        if (key != self._clip_key or self._transcript is None
                or fp != self._transcript.cut_fingerprint):
            return   # a newer clip/transcript superseded this extraction
        if decoded is None:
            self._pcm, self._pcm_rate = None, 0
            self._say("No audio to play.")
            return
        self._pcm, self._pcm_rate = decoded
        self._wav_for_key, self._wav_for_fp = key, fp
        then()

    @staticmethod
    def _read_wav(wav_path):
        """16-bit PCM WAV -> (int16 array (n, ch), rate), or None. >2 channels
        downmix to stereo (even->L, odd->R means); mono stays mono. Mirrors the
        editor's own _read_wav - kept local since modules never import each other."""
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

    def _start_audio(self):
        """Open a fresh OutputStream at the playhead (one per play). ANY failure -
        no sounddevice, no device, bad rate - degrades to silent playback (the tick
        falls back to the wall clock); a sound failure can never break the tab."""
        self._stop_audio()
        if sd is None or self._pcm is None or self._pcm_rate <= 0:
            return
        token = object()
        try:
            stream = sd.OutputStream(
                samplerate=self._pcm_rate, channels=self._pcm.shape[1], dtype="float32",
                callback=self._audio_cb,
                finished_callback=lambda: self._marshal(self._audio_died, token))
            stream.start()
        except Exception as ex:  # noqa: BLE001 - device/PortAudio failures of any kind
            print(f"[subtitle_studio] audio unavailable: {ex}")
            self._say("No audio device - playing silent.")
            return
        self._stream = stream
        self._audio_token = token

    def _stop_audio(self):
        self._audio_token = None
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _audio_cb(self, outdata, frames, _time_info, _status):
        """PortAudio's own thread - plain attribute/numpy-slice reads only, no
        tkinter, no lock (GIL-atomic plain reads/writes)."""
        pcm = self._pcm
        if pcm is None:
            outdata.fill(0.0)
            return
        pos = self._audio_pos
        seek = self._audio_seek
        if seek is not None:
            self._audio_seek = None
            pos = seek
        chunk = pcm[pos:pos + frames]
        out = chunk.astype(np.float32) / 32768.0
        if len(out) < frames:
            out = np.vstack([out, np.zeros((frames - len(out), pcm.shape[1]), np.float32)])
        outdata[:] = out
        self._audio_pos = pos + frames

    def _audio_died(self, token):
        if token is not self._audio_token or self._stream is None:
            return
        self._stop_audio()
        if self._playing:
            self._play_clock0 = perf_counter() - (self._playhead_t - self._play_t0)
            self._say("Audio device lost - continuing silent.")

    def _on_preview_frame_configure(self, _event=None):
        """Debounced re-render on pane/window resize (dragging the sash, resizing
        the app window) - a static preview must rescale live, not just on the next
        seek/play tick."""
        if self._preview_resize_job is not None:
            self.after_cancel(self._preview_resize_job)
        self._preview_resize_job = self.after(80, self._preview_resize_fire)

    def _preview_resize_fire(self):
        self._preview_resize_job = None
        self._render_preview_frame(self._playhead_t)

    def _render_preview_frame(self, t_out):
        """Composite + show ONE preview frame at time `t_out` (finished-clip
        seconds), SYNCHRONOUSLY on the UI thread (6.4b - replaces the old
        threading.Thread-per-frame worker). This mirrors the editor's own
        _render_play_frame: a persistent capture + cheap sequential reads make a
        direct decode-and-composite fast enough to run every tick without
        blocking Tk, and it is what actually fixes the fluidity - the old
        per-frame thread's real cost was opening+seeking a FRESH VideoCapture
        every call, not the compositing itself. Any failure degrades to leaving
        the last shown frame in place - a preview must never crash the tab."""
        if not self._subject or self._fps <= 0:
            return
        frame_idx = subtitles.source_frame_for_output_time(
            self._segs_frames, self._fps, t_out)
        frame = self._preview_frame_for(frame_idx)
        if frame is None:
            return
        pw = max(20, self._preview_frame.winfo_width())
        ph = max(20, self._preview_frame.winfo_height())
        out_w, out_h = self._contain_fit(pw, ph)
        self._preview_out_wh = (out_w, out_h)   # 6.5 C4: for drag-to-reposition math
        composed = self._composite_preview_frame(frame, frame_idx, out_w, out_h, t_out)
        photo = self._frame_to_photo(composed)
        self._preview_photo = photo   # keep a ref - Tk GCs the image without one
        self._preview_img_lbl.configure(image=photo)
        self._preview_placeholder_lbl.place_forget()

    def _preview_frame_for(self, frame_idx):
        """The decoded source frame for `frame_idx`, holding the last one.

        Re-compositing the SAME playhead frame is common - a style slider, a
        timing drag, a subtitle reposition - and each of those would otherwise
        pay a full cap.set seek, because the capture has already advanced one
        frame past what it just read. Safe to hold: composite_frame is pure and
        never writes into its input. Dropped whenever the capture changes."""
        cached = self._preview_frame_cache
        if cached is not None and cached[0] == frame_idx:
            return cached[1]
        frame = self._read_source_frame(frame_idx)
        if frame is not None:
            self._preview_frame_cache = (frame_idx, frame)
        return frame

    @staticmethod
    def _contain_fit(pw, ph):
        """The largest 9:16 (compositor.OUT_W x OUT_H ratio) size that fits inside
        a `pw`x`ph` box - "contain", never cropped or distorted."""
        fit = min(pw / compositor.OUT_W, ph / compositor.OUT_H)
        return max(2, int(compositor.OUT_W * fit)), max(2, int(compositor.OUT_H * fit))

    @staticmethod
    def _denorm_box(box, w, h):
        if not box or len(box) != 4:
            return None
        return [box[0] * w, box[1] * h, box[2] * w, box[3] * h]

    def _subtitles_payload(self, t_out):
        """The LIVE Style-tab preview payload, or None. Deliberately independent of
        the per-clip "enabled" flag (shared.subtitles.subtitles_for_clip governs
        the RENDER pickup, not the preview) - the whole point of this tab is to
        show what subtitles would look like before the user commits to them, and
        it must reflect style edits immediately, not a frozen enable-time snapshot."""
        if self._transcript is None or not any(ln.words for ln in self._transcript.lines):
            return None
        style = subtitles.SubtitleStyle.from_dict(
            self.app.config.get("subtitles", {}).get("style"))
        return {"transcript": self._transcript, "style": style, "t": t_out,
                "offset_ms": subtitles.timing_offset_ms(self.app.config)}

    def _composite_preview_frame(self, frame, frame_idx, out_w, out_h, t_out):
        """Best-effort REAL 9:16 composite (crop/stack/seam-melt/border/watermark +
        subtitles) using the editor's saved layout, via the shared pure compositor
        - never imports clip_editor. Branding image overlays and highlight PiPs are
        not composited yet (deferred - they need file-reload/caching machinery
        that doesn't change whether the crop/timing looks right, the actual ask).
        Any failure (malformed saved data, an unexpected exception) falls back to
        the plain source frame scaled to fit - this is a preview, never allowed to
        crash the tab."""
        layout_info = subtitles.layout_for(self.app.config, self._subject)
        sub_payload = self._subtitles_payload(t_out)
        if layout_info is None:
            try:
                return cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)
            except Exception:  # noqa: BLE001
                return frame
        try:
            h, w = frame.shape[:2]
            lay = layout_info["layout"]
            animated = interpolate_layout(layout_info["keyframes"], frame_idx) or lay
            game_box = self._denorm_box(animated.get("game_box") or lay.get("game_box"), w, h)
            cam_box = self._denorm_box(animated.get("cam_box") or lay.get("cam_box"), w, h)
            if game_box is None or cam_box is None:
                return cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)
            split_ratio = int(animated.get("split_ratio", lay.get("split_ratio", 50)))
            wm_cfg = lay.get("watermark") or {}
            watermark = None
            if wm_cfg.get("on") and wm_cfg.get("text"):
                watermark = {
                    "text": wm_cfg.get("text", ""),
                    "scale": float(wm_cfg.get("scale", 1.0)),
                    "text_bgr": compositor._hex_to_bgr(wm_cfg.get("text_color"),
                                                       (255, 255, 255)),
                    "plate_bgr": compositor._hex_to_bgr(wm_cfg.get("plate_color"),
                                                        (12, 12, 12)),
                    "position": wm_cfg.get("position", "center"),
                    # Same font as the subtitles (the editor's plate does the same).
                    "font": (self.app.config.get("subtitles", {}).get("style") or {})
                    .get("font") or subtitles.SubtitleStyle().font,
                }
            return compositor.composite_frame(
                frame, out_w=out_w, out_h=out_h, scale_x=1.0, scale_y=1.0,
                game_box=game_box, cam_box=cam_box,
                split_ratio=split_ratio, melt=bool(lay.get("melt", True)),
                melt_px=int(lay.get("melt_px", 50)),
                solid_border=bool(lay.get("solid_border", False)),
                border_color=compositor._hex_to_bgr(lay.get("border_color"), (15, 15, 15)),
                border_px=int(lay.get("border_px", 12)),
                watermark=watermark,
                single_panel=bool(lay.get("single_panel", False)),
                subtitles=sub_payload)
        except Exception:  # noqa: BLE001 - a bad saved layout must never break the preview
            try:
                return cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)
            except Exception:  # noqa: BLE001
                return frame

    @staticmethod
    def _frame_to_photo(frame):
        rgb = frame[:, :, ::-1]   # BGR -> RGB via a numpy slice
        return ImageTk.PhotoImage(Image.fromarray(rgb))

    # ── The engine (threaded) ─────────────────────────────────────────────────────────
    def _on_transcribe(self, confirm=True):
        """`confirm=True` (the Transcribe/Re-transcribe buttons) asks before
        replacing an EXISTING transcript for this clip - a manual click is the
        destructive path. `confirm=False` (auto-transcribe/keep-in-sync) skips the
        prompt, since the user already opted into that running unattended."""
        if self._transcribing:
            return
        installed = assets.installed_model_ids()
        if not installed:
            return
        subject, dtype = self._pick_source()
        if not subject:
            return
        if confirm and subtitles.load_transcript(paths.clip_key(subject)) is not None:
            if not messagebox.askyesno(
                    "Replace transcript?",
                    "This clip already has a transcript. Transcribing again "
                    "replaces it - this can't be undone. Continue?", parent=self):
                return
        meta = video_utils.probe_video(subject)
        if meta is None:
            self._status_lbl.configure(text="Could not read this clip.",
                                       fg=PALETTE["red_soft"])
            return
        _w, _h, fps, n = meta
        if fps <= 0 or n <= 0:
            self._status_lbl.configure(text="Could not read this clip.",
                                       fg=PALETTE["red_soft"])
            return
        segs_frames = self._segments_for(subject, dtype, n)

        scfg = self.app.config.get("subtitles", {})
        model_id = scfg.get("model") or installed[0]
        settings = {
            "model_id": model_id,
            "language": scfg.get("language", "auto"),
            "quality": scfg.get("quality", "balanced"),
            "device": scfg.get("device", "auto"),
            "game_terms": bool(scfg.get("game_terms", False)),
            "game_terms_text": scfg.get("game_terms_text", ""),
            "second_language": scfg.get("second_language", "") or "",
            "second_language_weight": int(scfg.get("second_language_weight", 50)),
            "vad": bool(scfg.get("vad", False)),
            "reduce_hallucinations": bool(scfg.get("reduce_hallucinations", False)),
            "snap_word_starts": bool(scfg.get("snap_word_starts", False)),
            "expert": dict(scfg.get("expert", {})),
        }

        self._cancel_event = threading.Event()
        self._transcribing_key = paths.clip_key(subject)
        self._set_transcribing_ui(True)
        threading.Thread(
            target=self._transcribe_worker,
            args=(subject, segs_frames, fps, n, settings, self._cancel_event),
            daemon=True).start()

    def _on_cancel_transcribe(self):
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._cancel_btn.configure(text="Canceling...", state="disabled")

    def _set_transcribing_ui(self, running):
        self._transcribing = running
        if running:
            self._transcribe_btn.pack_forget()
            self._progress_frame.pack(fill="x", pady=(4, 0))
            self._progress_bar["value"] = 0
            self._progress_lbl.configure(text="Starting...")
            self._cancel_btn.configure(text="Cancel", state="normal")
            self._status_lbl.configure(text="")
        else:
            self._tx = None
            if getattr(self, "_tx_job", None) is not None:
                self.after_cancel(self._tx_job)
                self._tx_job = None
            self._progress_frame.pack_forget()
            self._transcribe_btn.pack(fill="x", pady=(4, 0))
        self._apply_settings_lock(running)

    def _apply_settings_lock(self, locked):
        combo_state = "disabled" if locked else "readonly"
        widget_state = "disabled" if locked else "normal"
        self._model_combo.configure(state=combo_state)
        self._device_combo.configure(state=combo_state)
        for btn in self._quality_btns.values():
            btn.configure(state=widget_state)
        self._game_terms_chk.configure(state=widget_state)
        self._game_terms_entry.configure(state=widget_state)
        self._second_lang_chk.configure(state=widget_state)
        self._second_weight_scale.configure(state=widget_state)
        self._auto_chk.configure(state=widget_state)
        for picker in (self._lang_picker, self._lang2_picker):
            if picker is not None:
                picker["set_locked"](locked)

    def _get_whisper_model(self, model_id, device, compute):
        """Return the cached WhisperModel for (model_id, device, compute), building
        it only when that key differs from what's cached - the old model (if any)
        is dropped first so its VRAM/RAM is released before the new one loads.
        Safe from any thread. Construction is serialized by a separate build
        lock: a Transcribe clicked while the background warm-up is still loading
        waits for that load and reuses it, instead of building a second copy
        (which doubled RAM/VRAM and could run a GPU out of memory). The cache
        lock itself is never held during the slow load."""
        key = (model_id, device, compute)
        with self._whisper_build_lock:
            with self._whisper_model_lock:
                if self._whisper_model is not None and self._whisper_model_key == key:
                    return self._whisper_model
                self._whisper_model = None
                self._whisper_model_key = None
            WhisperModel = whisper_engine.whisper_model_class()
            model = WhisperModel(assets.model_dir(model_id), device=device,
                                 compute_type=compute)
            with self._whisper_model_lock:
                self._whisper_model = model
                self._whisper_model_key = key
            return model

    def _dispose_whisper_model(self):
        """Drop the cached model - called when the model or device setting changes,
        so a stale GPU/CPU instance isn't held onto once it can never be reused."""
        with self._whisper_model_lock:
            self._whisper_model = None
            self._whisper_model_key = None

    def _maybe_warm_model(self):
        """Background-load the currently selected model as soon as the tab is
        shown and a model is installed, so the FIRST real transcribe isn't also
        the cold weight-load + CUDA/cuDNN init (6.5 A1). Never blocks the UI;
        a no-op if already cached/in-flight or nothing is installed."""
        if self._transcribing or self._closing:
            return
        installed = assets.installed_model_ids()
        if not installed:
            return
        scfg = self.app.config.get("subtitles", {})
        model_id = scfg.get("model") or installed[0]
        if model_id not in installed:
            return
        device, compute = subtitles.resolve_device_compute(
            scfg.get("device", "auto"), scfg.get("quality", "balanced"))
        key = (model_id, device, compute)
        if key == self._whisper_model_key or key == self._warm_key_inflight:
            return
        self._warm_key_inflight = key
        threading.Thread(target=self._warm_worker, args=(model_id, device, compute),
                         daemon=True).start()

    def _warm_worker(self, model_id, device, compute):
        try:
            if device == "cuda":
                subtitles.ensure_cuda_on_path()
            self._get_whisper_model(model_id, device, compute)
        except Exception:  # noqa: BLE001 - a failed warm-up just means the first
            pass           # real transcribe pays the cold-load cost instead
        finally:
            self._warm_key_inflight = None

    def _transcribe_worker(self, subject, segs_frames, fps, total_frames, settings,
                           cancel_event):
        tmp_fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
        os.close(tmp_fd)
        try:
            whole_clip = (len(segs_frames) == 1 and segs_frames[0][0] <= 0
                         and segs_frames[0][1] >= total_frames - 1)
            secs = None if whole_clip else [(s / fps, (e + 1) / fps)
                                            for s, e in segs_frames]
            self._marshal(self._transcribe_stage, "Preparing audio...")
            wav = video_utils.extract_cut_audio(subject, secs, tmp_wav)
            try:
                samples = whisper_engine.load_audio(wav) if wav else None
            except (OSError, EOFError, ValueError, wave.Error):
                samples = None
            if samples is None:
                self._marshal(self._transcribe_failed,
                             "Could not read the clip's audio.")
                return

            device, compute = subtitles.resolve_device_compute(
                settings["device"], settings["quality"])
            if device == "cuda":
                subtitles.ensure_cuda_on_path()

            # Timing diagnostic (6.5 handoff item 2 - "cache speedup not observed"):
            # split out model acquisition from the actual inference loop below, so a
            # real run tells us whether the cache is hitting and where the time
            # actually goes, instead of guessing again.
            cache_hit = (self._whisper_model_key == (settings["model_id"], device, compute))
            if not cache_hit:
                self._marshal(self._transcribe_stage, "Loading the speech model...")
            t_model0 = perf_counter()
            try:
                model = self._get_whisper_model(settings["model_id"], device, compute)
            except Exception:  # noqa: BLE001 - any GPU construct failure falls back
                if device != "cuda":
                    raise
                self._marshal(self._transcribe_notice,
                             "GPU unavailable, using the processor.")
                device = "cpu"
                compute = "float32" if compute == "float32" else "int8"
                cache_hit = (self._whisper_model_key == (settings["model_id"], device, compute))
                t_model0 = perf_counter()
                model = self._get_whisper_model(settings["model_id"], device, compute)
            t_model = perf_counter() - t_model0
            print(f"[subtitle_studio] model acquire: {t_model:.2f}s "
                  f"({'cache hit' if cache_hit else 'built fresh'}, "
                  f"{settings['model_id']}/{device}/{compute})")

            prompt = subtitles.build_initial_prompt(
                settings["language"], settings["game_terms"],
                settings["game_terms_text"], settings["second_language"],
                settings["second_language_weight"])
            hotwords = (subtitles.build_hotwords(settings["game_terms_text"])
                       if settings["game_terms"] else None)
            kwargs = subtitles.build_transcribe_kwargs(
                settings["language"], prompt, hotwords, settings["quality"],
                settings["expert"], settings["vad"], settings["reduce_hallucinations"])
            t_infer0 = perf_counter()
            segments_iter, info = model.transcribe(samples, **kwargs)

            # 6.5 A3: collect the FLAT word stream across every segment - line/
            # sentence structure is rebuilt from this deterministically below,
            # independent of Whisper's own (unstable) segment boundaries.
            # NOTE: faster-whisper's transcribe() returns a lazy generator - the
            # actual inference happens while iterating segments_iter below, not on
            # the call above, so the inference clock spans this loop.
            all_words = []
            clip_duration = total_frames / fps if fps > 0 else 0.0
            self._marshal(self._transcribe_infer_start, info.duration or clip_duration,
                          device)
            for seg in segments_iter:
                if cancel_event.is_set():
                    break
                all_words.extend(
                    subtitles.Word(text=w.word.strip(), start=w.start, end=w.end,
                                   conf=float(getattr(w, "probability", 1.0)))
                    for w in (seg.words or []))
                total_dur = info.duration or clip_duration or 1.0
                pct = max(0.0, min(1.0, seg.end / total_dur))
                self._marshal(self._transcribe_progress, pct)
            self._marshal(self._transcribe_finishing)
            t_infer = perf_counter() - t_infer0
            print(f"[subtitle_studio] inference: {t_infer:.2f}s "
                  f"for {clip_duration:.1f}s of audio")

            if cancel_event.is_set():
                self._marshal(self._transcribe_canceled)
                return

            transcript = subtitles.Transcript(
                lines=subtitles.rebuild_lines(all_words),
                language=info.language, model_id=settings["model_id"],
                source_fps=fps,
                cut_fingerprint=subtitles.cut_fingerprint(segs_frames, fps),
                created=int(time.time()))
            if settings["snap_word_starts"]:
                decoded = self._read_wav(wav)
                if decoded is not None:
                    pcm, rate = decoded
                    subtitles.snap_word_starts(transcript, pcm, rate)
            subtitles.detect_gap_candidates(wav, transcript)
            transcript.source_hash = subtitles.content_hash(transcript)
            subtitles.save_transcript(paths.clip_key(subject), transcript,
                                      cap=config_mod.clip_memory(self.app.config))
            try:
                cache_path = os.path.join(paths.get_path("transcripts"),
                                         paths.clip_key(subject) + ".wav")
                shutil.copyfile(wav, cache_path)
            except OSError:
                pass
            self._marshal(self._transcribe_done, transcript, subject)
        except Exception as ex:  # noqa: BLE001 - never crash the tab on a bad run
            self._marshal(self._transcribe_failed, str(ex))
        finally:
            try:
                os.remove(tmp_wav)
            except OSError:
                pass

    # Progress (v1.1). faster-whisper decodes 30-second windows and hands back a
    # window's segments all at once, so the real position arrives in big steps
    # (a 40s clip read 0% -> 75% -> 100%). Between real reports the bar advances
    # on an estimate - the speed measured so far, or a first guess per device -
    # capped short of the next window so it never runs ahead of reality, and it
    # never moves backwards.
    _WINDOW_S = 30.0
    _FIRST_GUESS_RATE = {"cuda": 8.0, "cpu": 1.0}   # audio seconds per wall second

    def _transcribe_stage(self, msg):
        self._progress_lbl.configure(text=msg)

    def _transcribe_finishing(self):
        self._tx = None
        self._progress_bar["value"] = 100
        self._progress_lbl.configure(text="Finishing up...")

    def _transcribe_infer_start(self, duration, device):
        self._tx = {"t0": time.monotonic(), "dur": max(0.1, float(duration or 0.0)),
                    "rate": self._FIRST_GUESS_RATE.get(device, 1.0),
                    "reported_s": 0.0, "shown": 0.0}
        self._tx_tick()

    def _transcribe_progress(self, pct):
        tx = getattr(self, "_tx", None)
        if tx is None:
            self._show_tx_pct(pct)
            return
        pos = pct * tx["dur"]
        elapsed = max(0.05, time.monotonic() - tx["t0"])
        tx["reported_s"] = max(tx["reported_s"], pos)
        tx["rate"] = max(0.05, tx["reported_s"] / elapsed)
        self._show_tx_pct(max(tx["shown"], pct))

    def _tx_tick(self):
        tx = getattr(self, "_tx", None)
        if tx is None or not self._transcribing or self._closing:
            return
        elapsed = time.monotonic() - tx["t0"]
        est_s = min(elapsed * tx["rate"], tx["reported_s"] + self._WINDOW_S * 0.95,
                    tx["dur"] * 0.97)
        self._show_tx_pct(max(tx["shown"], est_s / tx["dur"]))
        self._tx_job = self.after(100, self._tx_tick)

    def _show_tx_pct(self, pct):
        pct = max(0.0, min(1.0, pct))
        tx = getattr(self, "_tx", None)
        if tx is not None:
            tx["shown"] = pct
        self._progress_bar["value"] = pct * 100
        self._progress_lbl.configure(text=f"Transcribing... {int(pct * 100)}%")

    def _transcribe_notice(self, msg):
        self._progress_lbl.configure(text=msg)

    def _transcribe_done(self, transcript, subject):
        self._set_transcribing_ui(False)
        if subject == self._subject:
            self._hide_stale_banner()
            self._loaded_for_key = paths.clip_key(subject)
            self._load_transcript_editor(transcript)
            self._status_lbl.configure(text="Subtitles ready.", fg=PALETTE["green"])
        else:
            # Another clip is open now; the result was saved under its own clip.
            self._status_lbl.configure(
                text=f"Subtitles ready for {os.path.basename(subject)}.",
                fg=PALETTE["green"])
        show_toast(self.app.root, "Subtitles ready.",
                  "Your transcript is ready to view.")

    def _transcribe_failed(self, msg):
        self._set_transcribing_ui(False)
        self._status_lbl.configure(text=f"Transcription failed: {msg}",
                                   fg=PALETTE["red_soft"])

    def _transcribe_canceled(self):
        self._set_transcribing_ui(False)
        self._status_lbl.configure(text="Canceled. Previous transcript kept.",
                                   fg=PALETTE["text_mute"])

    # ── Tutorial (v1.1 tour: transcribe, edit, style, render) ───────────────────────
    def maybe_show_tutorial(self):
        """Hub hook (main.py's <<NotebookTabChanged>> dispatch): show the Subtitle
        Studio intro popup on first visit, once tutorials have been asked about and
        are on."""
        tcfg = self.app.config.get("tutorial", {})
        if not tcfg.get("asked") or not tcfg.get("enabled"):
            return
        if tcfg.get("seen", {}).get("subtitle_studio"):
            return
        self._offer_tutorial()

    def _offer_tutorial(self):
        tcfg = self.app.config.setdefault("tutorial", {})
        tcfg.setdefault("seen", {})["subtitle_studio"] = True
        self.app.save_config()
        TutorialIntro(
            self, title="Welcome to Subtitle Studio",
            body=("Turn your clip's speech into subtitles here, on the finished cut "
                  "you made in the editor."),
            on_show_me=self._start_walkthrough)

    def _tour_scroll_to(self, target_fn):
        """on_show helper: bring a step's target into view in the scrolling left
        column before its ring is drawn (the settings form is taller than the
        column - see _build_left)."""
        w = target_fn()
        if w is not None:
            try:
                self._left_scroll.scroll_to(w)
            except Exception:   # noqa: BLE001 - a bad scroll target must never crash the tour
                pass

    def _tour_step(self, target_fn, title, body, subtab=None):
        def on_show():
            if subtab is not None and subtab != self._left_subtab:
                self._show_subtab(subtab)
                self.update_idletasks()
            self._tour_scroll_to(target_fn)
        return {"target": target_fn, "on_show": on_show, "title": title, "body": body}

    def _start_walkthrough(self):
        steps = [
            self._tour_step(
                lambda: getattr(self, "_model_combo", None), "Model",
                "Pick which downloaded speech model transcribes your clip. Get "
                "more models in the Downloads tab.", subtab="transcript"),
            self._tour_step(
                lambda: getattr(self, "_lang_picker", {}).get("collapsed"),
                "Language",
                "Click to search languages, or leave it on Auto-detect. 'More "
                "options' below holds spelling hints for game terms or a second "
                "language, plus quality and GPU settings. The defaults are fine "
                "for most clips.", subtab="transcript"),
            self._tour_step(
                lambda: getattr(self, "_transcribe_btn", None), "Transcribe",
                "Turns your clip's finished (cut) audio into text. Tick 'keep in "
                "sync' to have new clips transcribe themselves and refresh when "
                "you change the cuts. If you fixed words by hand, it asks "
                "first.", subtab="transcript"),
            self._tour_step(
                lambda: getattr(self, "_transcript_text", None), "Edit like a text file",
                "Click a word and type to fix it. Space splits a word, Backspace "
                "at the start of a word joins it to the one before, Enter starts "
                "a new row. Select across rows to delete or retype. Ctrl+Z and "
                "Ctrl+Y undo and redo. Green words are ones you added, yellow "
                "ones the model was unsure about."),
            self._tour_step(
                lambda: getattr(self, "_review_btn", None), "Review & effects",
                "'Review flagged' walks the unsure words one by one. Shake and "
                "Pop add emphasis to the selected word."),
            self._tour_step(
                lambda: getattr(self, "_timeline_canvas", None), "Timeline",
                "Drag a word to move it, or drag its edges to change when it "
                "starts and ends. Right-click to add a word or a non-speech label "
                "such as [laughs]. Drag the grey bar at the bottom to scrub "
                "through the clip."),
            self._tour_step(
                lambda: getattr(self, "_preview_frame", None), "Preview",
                "Exactly what gets burned into the video. Drag the subtitles to "
                "move them up or down."),
            self._tour_step(
                lambda: getattr(self, "_style_tab_btn", None), "Style",
                "Presets, font, size, and color up top; outline, highlight, and "
                "animation fold out below. Each clip remembers its own style, "
                "and new clips start from the last one you used.",
                subtab="style"),
            self._tour_step(
                lambda: getattr(self, "_apply_btn", None), "Apply & Render",
                "Turns subtitles on for this clip and renders it in the Clip "
                "Editor. Later style changes reach this clip on their own."),
            self._tour_step(
                lambda: getattr(self, "_config_tab_btn", None), "Advanced",
                "Deeper tuning: noise filtering, the unsure-word cutoff, a timing "
                "offset if every subtitle is a little early or late, and expert "
                "settings. Nothing here is required.", subtab="config"),
        ]
        TutorialWalkthrough(self.app.root, steps).start()

    # ── Worker->UI marshalling (queue + pump pattern) ────────────────────────────────
    def _marshal(self, fn, *args):
        if not self._closing:
            self._ui_queue.put((fn, args))

    def _ui_pump(self):
        self._ui_pump_job = self.after(50, self._ui_pump)
        while not self._closing:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            fn(*args)

    # ── Hub lifecycle ─────────────────────────────────────────────────────────────────
    def has_unsaved(self):
        return "a transcription is in progress" if self._transcribing else None

    def on_close(self):
        self._closing = True
        if self._ui_pump_job is not None:
            self.after_cancel(self._ui_pump_job)
            self._ui_pump_job = None
        if self._preview_resize_job is not None:
            self.after_cancel(self._preview_resize_job)
            self._preview_resize_job = None
        if self._tl_redraw_job is not None:
            self.after_cancel(self._tl_redraw_job)
            self._tl_redraw_job = None
        if self._tl_preview_job is not None:
            self.after_cancel(self._tl_preview_job)
            self._tl_preview_job = None
        if self._tx_job is not None:
            self.after_cancel(self._tx_job)
            self._tx_job = None
        if self._tl_scrub_job is not None:
            self.after_cancel(self._tl_scrub_job)
            self._tl_scrub_job = None
        for attr in ("_regroup_job", "_style_save_job", "_sash_restore_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        pending_edits = (self._type_reconcile_job is not None
                         or self._save_job is not None)
        if pending_edits:
            self._commit_now()
        self._stop_play()
        self._release_preview_capture()
