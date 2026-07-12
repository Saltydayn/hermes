"""Export tab: exit point for media (Phase 1 basic export; 3.2 polish; 5.5.1c clarity).

Takes the current clip, the MOST RECENTLY published bus artifact (edited render or raw
import, by the bus publish sequence; 5.5.1c), pulled lazily when this tab becomes
visible, and writes it to the default export folder as a validated copy (optionally a
user-typed name, else a timestamped one), then publishes `final_file`. The clip card
shows which source is loaded, in red when it is the raw unrendered import; exporting a
raw clip while the editor is enabled asks first. No editing or re-encoding; that comes
in later phases.

3.2 lays the tab out in two columns: the pipeline (current clip, destination, export) on
the left, and a Share + Copy-slots panel on the right. Share is HANDOFF ONLY - a clicked
share button opens the site's upload page in the browser AND reveals the exported file in
the file manager to drag in. No API, account, OAuth, or upload (that is Phase 9); all of it
sits behind the one `_share` seam so a real uploader can slot in later. Test-watch Preview
opens the finished file in the OS player; copy slots hold up to 10 click-to-copy snippets.

Reaches Import's output ONLY through `shared/bus.py` (never imports another module). The
copy runs on a daemon thread; its result is marshalled back via the queue + _ui_pump
pattern (workers never touch tkinter, not even after()). Share/preview/reveal are quick
stdlib OS hand-offs run on the UI thread (no worker).
"""

import datetime
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import cv2

from shared import registry, video_utils
from shared.ui_helpers import (
    PALETTE, HelpDialog, ScrollableFrame, Tooltip, TutorialIntro, TutorialReplayButton,
    TutorialWalkthrough, bind_scale_jump, display_font, mono_font, open_url, show_toast,
    themed_button, ui_font,
)

# Value-moment nudge (4.4): one warm, dismissable thank-you after the user's first N completed
# exports. Fires once ever. Copy is terse, no em dash, support mentioned last and optional.
NUDGE_AFTER = 10
_NUDGE_TITLE = "10 shorts done. Nice work."
_NUDGE_BODY = (
    "You've turned 10 clips into finished Shorts with HERMES. That is exactly what this tool "
    "is for, and you are getting good at it. If you ever want to see what else I make, my "
    "streams and links are on the About tab. Totally optional. Thanks for using HERMES."
)


def _kofi_url(app):
    return (app.config.get("links", {}).get("kofi") or "").strip()


def _kofi_action_label(app):
    """'Support on Ko-fi' only when a Ko-fi URL is set, else None (no dangling button)."""
    return "Support on Ko-fi" if _kofi_url(app) else None


def _kofi_action(app):
    url = _kofi_url(app)
    return (lambda: open_url(url)) if url else None

# Defaults for the editable quick-share buttons. Kept here so the in-tab "Reset to
# defaults" action and shared/config.py's DEFAULTS agree (a copy of the same three).
DEFAULT_SHARE_BUTTONS = [
    {"label": "YouTube",   "url": "https://www.youtube.com/upload"},
    {"label": "TikTok",    "url": "https://www.tiktok.com/upload"},
    {"label": "Instagram", "url": "https://www.instagram.com/"},
]
SNIPPET_MIN = 5   # text-snippet floor (5.5.2e: extendable, no max, mirrors layout slots)


def snippet_delete_check(slots, minimum=SNIPPET_MIN):
    """Can the LAST text snippet be deleted (5.5.2e)? Returns
    (allowed, needs_confirm, preview):
    - allowed=False at the `minimum` floor.
    - needs_confirm=True when the last slot holds non-empty text (preview = a short
      form of it) so the UI can ask before discarding it.
    Pure: reads the slot list only."""
    count = max(minimum, len(slots))
    if count <= minimum:
        return False, False, None
    last = slots[count - 1] if count - 1 < len(slots) else ""
    if last and str(last).strip():
        text = str(last).replace("\n", " ")
        preview = text if len(text) <= 40 else text[:39] + "…"
        return True, True, preview
    return True, False, None


class ExporterModule(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.bus = app.bus

        self._current_path = None   # the clip we'd export (pulled from the bus)
        self._current_dtype = None  # its bus data type (raw_clip / edited_clip / ...)
        self._last_dest = None      # folder of the most recent successful export
        self._last_export_path = None  # the most recent successful export FILE (share/preview/reveal target)
        self._open_shown = False    # whether the "Open folder" button is packed yet
        self._closing = False       # set on shutdown so the worker won't touch dead widgets
        self._exporting = False     # copy thread in flight (close guard asks about it)
        self._compressing = False   # compress thread in flight (close guard, 5.5.1d)
        self._cur_size_b = 0        # current clip's file size (compress info line)
        self._cur_dur = None        # current clip's duration in seconds, or None
        self._comp_pct_echo = None  # last programmatic slider value (echo filter)
        self._unlocked_slot = None  # index of the copy slot currently open for editing (None = all locked)
        self._share_btns = []       # share buttons, for enable/disable
        self._preview_btn = None
        self._reveal_btn = None
        self._ui_queue = queue.Queue()    # worker→UI results (see _marshal)
        self._ui_pump_job = None
        self._build()

        # Lazy pull: load the latest raw_clip only when the user switches to this tab.
        self.bind("<Visibility>", self._on_visible)

        # Worker→UI results pump (copy result - see _marshal).
        self._ui_pump_job = self.after(50, self._ui_pump)

    # ── UI construction ──────────────────────────────────────────────────────────────
    def _build(self):
        # Header (full width, above both columns).
        header = tk.Frame(self, bg=PALETTE["bg"])
        header.pack(fill="x", pady=(22, 0), padx=26)
        tk.Label(header, text="Export", bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=display_font(20)).pack(anchor="w")
        bar = tk.Frame(header, bg=PALETTE["orange"], width=66, height=3)  # module accent
        bar.pack(anchor="w", pady=(7, 0))
        bar.pack_propagate(False)
        tk.Label(header, text="Import a clip, render your edit in the editor, then "
                              "export or share the result here.", bg=PALETTE["bg"],
                 fg=PALETTE["text_mute"], font=ui_font(10)).pack(anchor="w", pady=(8, 0))

        # Two columns (Vision B): left = pipeline, right = share + copy slots. The left is a
        # touch wider; the right scrolls so its buttons + 10 slots never clip in a short window.
        cols = tk.Frame(self, bg=PALETTE["bg"])
        cols.pack(fill="both", expand=True, padx=22, pady=(14, 0))
        cols.grid_columnconfigure(0, weight=23, uniform="col")
        cols.grid_columnconfigure(1, weight=20, uniform="col")
        cols.grid_rowconfigure(0, weight=1)
        left = tk.Frame(cols, bg=PALETTE["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._right_scroll = right_wrap = ScrollableFrame(cols)
        right_wrap.grid(row=0, column=1, sticky="nsew")
        right = right_wrap.body

        self._build_left(left)
        self._build_right(right)
        self._refresh_default_label()
        self._refresh_create_folder_btn()
        self._build_share_buttons()
        self._build_copy_slots()
        self._refresh_share_enabled()

        # Always-present per-tab tutorial replay button (bottom-right corner, 5.5.4b).
        TutorialReplayButton(self, self._start_walkthrough,
                             "Need a refresher? Replay the tour.")

    # ── Tutorial (5.5.4b) ────────────────────────────────────────────────────────────
    def maybe_show_tutorial(self):
        """Hub hook (main.py's <<NotebookTabChanged>> dispatch, see 5.5.4a): show the
        Export intro popup on first visit, once tutorials have been asked about and
        are on."""
        tcfg = self.app.config.get("tutorial", {})
        if not tcfg.get("asked") or not tcfg.get("enabled"):
            return
        if tcfg.get("seen", {}).get("exporter"):
            return
        self._offer_tutorial()

    def _offer_tutorial(self):
        """Show Export's intro popup. Marks "seen" the moment it's shown - skipping
        and touring both count, so the intro never repeats either way."""
        tcfg = self.app.config.setdefault("tutorial", {})
        tcfg.setdefault("seen", {})["exporter"] = True
        self.app.save_config()
        TutorialIntro(
            self, title="Welcome to Export",
            body=("Export is the exit point - pick up the rendered clip from the "
                  "editor, save it, and share it."),
            on_show_me=self._start_walkthrough)

    def _tour_scroll_to(self, attr):
        """on_show helper: bring a named widget in the right (scrolled) column into
        view before its step's ring is drawn (5.5.4b beta fix, same as importer's)."""
        w = getattr(self, attr, None)
        if w is not None:
            try:
                self._right_scroll.scroll_to(w)
            except Exception:   # noqa: BLE001 - a bad scroll target must never crash the tour
                pass

    def _start_walkthrough(self):
        """Export's 5-step tour: CURRENT CLIP, DESTINATION, COMPRESS FOR SHARING,
        SHARE, TEXT SNIPPETS. Also the entry point for the replay button."""
        steps = [
            {"target": lambda: getattr(self, "_cur_clip_card", None),
             "title": "Current clip",
             "body": "What's currently loaded - the render from the editor if "
                     "there is one, otherwise the raw import."},
            {"target": lambda: getattr(self, "_dest_block", None),
             "title": "Destination",
             "body": "Where the exported file saves. Pick a folder once and it "
                     "sticks."},
            {"target": lambda: getattr(self, "_compress_lbl", None),
             "title": "Compress for sharing",
             "body": "Optional - shrink a copy to fit a size-limited platform "
                     "like Discord. The normal export is untouched."},
            {"target": lambda: getattr(self, "_share_lbl", None),
             "on_show": lambda: self._tour_scroll_to("_share_lbl"),
             "title": "Share",
             "body": "One click opens the upload page and reveals the file so "
                     "you can drag it in."},
            {"target": lambda: getattr(self, "_snip_lbl", None),
             "on_show": lambda: self._tour_scroll_to("_snip_lbl"),
             "title": "Text snippets",
             "body": "Reusable copy-paste text - a title, tags, whatever you "
                     "reuse often. Click a slot to copy it."},
        ]
        TutorialWalkthrough(self.app.root, steps).start()

    def _build_left(self, left):
        """The pipeline column: current clip (+ Preview), destination, name, export, status."""
        self._cur_clip_lbl = tk.Label(
            left, text="CURRENT CLIP", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
            font=ui_font(9))
        self._cur_clip_lbl.pack(anchor="w", pady=(0, 6))
        self._cur_clip_card = cur = tk.Frame(
            left, bg=PALETTE["card"], highlightbackground=PALETTE["border"],
            highlightthickness=1, bd=0)
        cur.pack(fill="x")
        cur_in = tk.Frame(cur, bg=PALETTE["card"])
        cur_in.pack(fill="x", padx=14, pady=12)
        self._file_label = tk.Label(cur_in, text="Nothing loaded - import a clip first.",
                                    bg=PALETTE["card"], fg=PALETTE["text_mute"],
                                    font=mono_font(10), anchor="w")
        self._file_label.pack(fill="x")
        self._meta_label = tk.Label(cur_in, text="", bg=PALETTE["card"],
                                    fg=PALETTE["text_mute"], font=mono_font(9), anchor="w")
        self._meta_label.pack(fill="x", pady=(4, 2))
        self._source_label = tk.Label(cur_in, text="", bg=PALETTE["card"],
                                      fg=PALETTE["text_faint"], font=ui_font(8),
                                      anchor="w")
        self._source_label.pack(fill="x", pady=(0, 8))
        cur_btns = tk.Frame(cur_in, bg=PALETTE["card"])
        cur_btns.pack(anchor="w")
        themed_button(cur_btns, "Refresh", self._refresh, kind="neutral").pack(side="left")
        self._preview_btn = themed_button(cur_btns, "Preview", self._preview, kind="neutral",
                                          disabledforeground=PALETTE["text_faint"])
        self._preview_btn.pack(side="left", padx=(8, 0))
        Tooltip(self._preview_btn, "Watch the exported file (or the current clip) in your "
                                   "default video player.")

        # Destination panel.
        self._dest_lbl = tk.Label(
            left, text="DESTINATION", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
            font=ui_font(9))
        self._dest_lbl.pack(anchor="w", pady=(16, 6))
        self._dest_block = dest = tk.Frame(left, bg=PALETTE["bg"])
        dest.pack(fill="x")
        tk.Label(dest, text="Default export folder:", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(10)).pack(anchor="w")
        self._default_label = tk.Label(dest, text="", bg=PALETTE["bg"],
                                       fg=PALETTE["text_mute"], font=mono_font(9), anchor="w")
        self._default_label.pack(anchor="w", pady=(2, 8))
        dest_btns = tk.Frame(dest, bg=PALETTE["bg"])
        dest_btns.pack(anchor="w")
        themed_button(dest_btns, "Set default folder…", self._set_default_folder,
                      kind="neutral").pack(side="left")
        self._create_folder_btn = themed_button(
            dest_btns, "Create 'shorts_export' folder", self._create_shorts_folder,
            kind="neutral", disabledforeground=PALETTE["text_faint"])
        self._create_folder_btn.pack(side="left", padx=(10, 0))

        # Optional custom file name: blank = the automatic timestamped name (keeps today's
        # behavior). A typed name is sanitized and used as-is (source extension appended).
        name_row = tk.Frame(left, bg=PALETTE["bg"])
        name_row.pack(fill="x", pady=(16, 0))
        tk.Label(name_row, text="Save as (optional):", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(10)).pack(side="left")
        self._name_var = tk.StringVar(value="")
        ttk.Entry(name_row, textvariable=self._name_var, width=24).pack(
            side="left", padx=(8, 0))
        tk.Label(name_row, text="(blank = automatic name)", bg=PALETTE["bg"],
                 fg=PALETTE["text_mute"], font=ui_font(8)).pack(anchor="w", pady=(2, 0))

        # Export row.
        row = tk.Frame(left, bg=PALETTE["bg"])
        row.pack(fill="x", pady=(18, 0))
        self._export_btn = themed_button(row, "Export", self._export, kind="primary",
                                         disabledforeground=PALETTE["text_faint"])
        self._export_btn.configure(state="disabled")
        self._export_btn.pack(side="left")
        self._open_btn = themed_button(row, "Open folder", None, kind="neutral")  # shown on success

        # Share compressor (5.5.1d): a smaller re-encoded copy for size-limited
        # platforms. A side artifact - the normal export path is untouched.
        self._compress_lbl = tk.Label(
            left, text="COMPRESS FOR SHARING", bg=PALETTE["bg"],
            fg=PALETTE["text_mute"], font=ui_font(9))
        self._compress_lbl.pack(anchor="w", pady=(16, 6))
        comp_row = tk.Frame(left, bg=PALETTE["bg"])
        comp_row.pack(fill="x")
        tk.Label(comp_row, text="Target size (MB)", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(10)).pack(side="left")
        self._comp_mb = ttk.Spinbox(comp_row, from_=5, to=500, increment=5, width=6,
                                    command=self._on_comp_mb_change)
        self._comp_mb.set(10)
        self._comp_mb.pack(side="left", padx=(8, 0))
        self._comp_mb.bind("<KeyRelease>", lambda _e: self._on_comp_mb_change())
        Tooltip(self._comp_mb, "File-size budget for the compressed copy.")
        self._comp_btn = themed_button(comp_row, "Compress for sharing",
                                       self._compress, kind="neutral",
                                       disabledforeground=PALETTE["text_faint"])
        self._comp_btn.pack(side="left", padx=(10, 0))
        Tooltip(self._comp_btn, "Re-encode a copy of the current clip to fit the "
                                "target size.")
        # Percent slider: the same target expressed as % of the source file size.
        # The two controls adjust each other (echo-filtered - a tk.Scale fires its
        # command via an idle callback, so a plain guard flag would not hold).
        pct_row = tk.Frame(left, bg=PALETTE["bg"])
        pct_row.pack(fill="x", pady=(4, 0))
        tk.Label(pct_row, text="Size vs source (%)", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(10)).pack(side="left")
        self._comp_pct = tk.Scale(
            pct_row, from_=1, to=100, orient="horizontal", resolution=1, showvalue=1,
            command=self._on_comp_pct, bg=PALETTE["panel"],
            troughcolor=PALETTE["deep"], activebackground=PALETTE["green"],
            fg=PALETTE["text_dim"], highlightthickness=0, bd=0, sliderrelief="flat",
            sliderlength=18, width=12, font=ui_font(8))
        self._comp_pct.pack(side="left", fill="x", expand=True, padx=(8, 0))
        bind_scale_jump(self._comp_pct)
        Tooltip(self._comp_pct, "Target size as a share of the source file size. "
                                "Moves with the MB field.")
        self._comp_info = tk.Label(left, text="", bg=PALETTE["bg"],
                                   fg=PALETTE["text_faint"], font=mono_font(9),
                                   anchor="w")
        self._comp_info.pack(fill="x", pady=(2, 0))
        tk.Label(left, text="Makes a smaller copy for size-limited sharing, for "
                            "example Discord. The normal export is untouched.",
                 bg=PALETTE["bg"], fg=PALETTE["text_faint"], font=ui_font(8),
                 anchor="w", justify="left", wraplength=380).pack(fill="x",
                                                                  pady=(4, 0))

        # Status line.
        self._status = tk.Label(left, text="", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                                font=ui_font(10), anchor="w", justify="left", wraplength=380)
        self._status.pack(fill="x", pady=(16, 8))

    def _build_right(self, right):
        """The share + copy-slots column. Controls that act on a file stay disabled until one
        exists; copy slots are always usable (plain text snippets)."""
        share_head = tk.Frame(right, bg=PALETTE["bg"])
        share_head.pack(fill="x", pady=(0, 6))
        self._share_lbl = tk.Label(share_head, text="SHARE", bg=PALETTE["bg"],
                                   fg=PALETTE["text_mute"], font=ui_font(9))
        self._share_lbl.pack(side="left")
        themed_button(share_head, "Edit", self._edit_share_buttons, kind="neutral",
                      width=5).pack(side="right")
        tk.Label(right, text="Opens the upload page and reveals the file to drag in.",
                 bg=PALETTE["bg"], fg=PALETTE["text_faint"], font=ui_font(8),
                 anchor="w", justify="left", wraplength=300).pack(fill="x", pady=(0, 6))
        self._share_frame = tk.Frame(right, bg=PALETTE["bg"])   # rebuilt by _build_share_buttons
        self._share_frame.pack(fill="x")
        self._reveal_btn = themed_button(right, "Reveal file in folder", self._reveal_file,
                                         kind="neutral",
                                         disabledforeground=PALETTE["text_faint"])
        self._reveal_btn.pack(fill="x", pady=(8, 0))

        snip_head = tk.Frame(right, bg=PALETTE["bg"])
        snip_head.pack(fill="x", pady=(18, 2))
        self._snip_lbl = tk.Label(snip_head, text="TEXT SNIPPETS", bg=PALETTE["bg"],
                                  fg=PALETTE["text_mute"], font=ui_font(9))
        self._snip_lbl.pack(side="left")
        snip_help = themed_button(
            snip_head, "?",
            lambda: HelpDialog(self, title="Text snippets", body=self._HELP_SNIPPETS),
            kind="neutral", width=2)
        snip_help.pack(side="right")
        Tooltip(snip_help, "What text snippets are for")
        tk.Label(right, text="Reusable text, for example your video title or tags. "
                             "Click a row to copy it.",
                 bg=PALETTE["bg"], fg=PALETTE["text_faint"], font=ui_font(8),
                 anchor="w", justify="left", wraplength=300).pack(fill="x", pady=(0, 6))
        self._slots_frame = tk.Frame(right, bg=PALETTE["bg"])   # rebuilt by _build_copy_slots
        self._slots_frame.pack(fill="x")
        # Add / delete row: two buttons 50/50, delete LEFT, add RIGHT (mirrors the
        # editor's layout slots, 5.5.2e).
        snip_ad = tk.Frame(right, bg=PALETTE["bg"])
        snip_ad.pack(fill="x", pady=(6, 0))
        snip_ad.grid_columnconfigure(0, weight=1, uniform="snipad")
        snip_ad.grid_columnconfigure(1, weight=1, uniform="snipad")
        snip_del = themed_button(snip_ad, "Delete last slot",
                                 self._delete_last_snippet, kind="danger")
        snip_del.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        Tooltip(snip_del, "Remove the last snippet slot (minimum five). Asks first if "
                          "it holds text.")
        snip_add = themed_button(snip_ad, "Add slot", self._add_snippet, kind="neutral")
        snip_add.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        Tooltip(snip_add, "Append an empty snippet slot.")

    # ── Pull the current clip from the bus ───────────────────────────────────────────
    def _pick_source(self):
        """Most RECENTLY published artifact wins, by the bus publish sequence
        (5.5.1c). This closes the stale-render trap: render clip A, then import
        clip B and never render it - a fixed type priority would still offer A's
        old render; recency offers B's raw import (and says so in red).
        `subtitled_clip` participates when Phase 3 lands. (None, None) when the bus
        has nothing; callers keep the empty state, never crash."""
        best_path, best_dtype, best_seq = None, None, 0
        for dtype in ("subtitled_clip", "edited_clip", "raw_clip"):
            path, seq = self.bus.latest_info(dtype)
            if path and seq > best_seq:
                best_path, best_dtype, best_seq = path, dtype, seq
        return best_path, best_dtype

    def _on_visible(self, _e):
        path, dtype = self._pick_source()
        if path and path != self._current_path:
            self._set_current(path, dtype)

    def _refresh(self):
        path, dtype = self._pick_source()
        if path:
            self._set_current(path, dtype)
        else:
            self._status.configure(text="Nothing to pick up yet - import a clip (or "
                                        "render an edit) first.",
                                   fg=PALETTE["text_mute"])

    def _set_current(self, path, dtype=None):
        """Adopt `path` as the current clip: show light metadata, which bus artifact
        it is (edited/raw), and enable Export + the file-dependent share controls.
        5.5.1c: a raw import reads as a red warning while the editor is enabled
        (skipping the render is then almost certainly a mistake); with the editor
        disabled a raw export is deliberate and stays neutral."""
        self._current_path = path
        self._current_dtype = dtype
        label = (dtype or "").replace("_clip", "")
        if dtype == "raw_clip" and registry.is_enabled("editor", self.app.config):
            self._source_label.configure(
                text="NOT RENDERED - this is the raw import, not an edited Short",
                fg=PALETTE["red_soft"])
        elif dtype == "edited_clip":
            self._source_label.configure(text="source: edited", fg=PALETTE["green"])
        else:
            self._source_label.configure(text=f"source: {label}" if label else "",
                                         fg=PALETTE["text_faint"])
        self._file_label.configure(text=os.path.basename(path), fg=PALETTE["text"])
        try:
            self._cur_size_b = os.path.getsize(path)
        except OSError:
            self._cur_size_b = 0
        self._cur_dur = None
        meta = video_utils.probe_video(path)
        if meta is None:
            self._meta_label.configure(text="(couldn't read video metadata)",
                                       fg=PALETTE["text_mute"])
        else:
            w, h, fps, n = meta
            fps_s = f"{fps:.1f}fps" if fps > 0 else "fps unknown"
            self._cur_dur = n / fps if fps > 0 and n > 0 else None
            dur_s = self._fmt_duration(self._cur_dur)
            self._meta_label.configure(text=f"{w}×{h} · {fps_s} · {dur_s}",
                                       fg=PALETTE["text_dim"])
        self._on_comp_mb_change()       # compress slider + info follow the new clip
        self._export_btn.configure(state="normal")
        self._refresh_share_enabled()   # a clip is loaded → preview/share/reveal are live

    # ── Destination handling ─────────────────────────────────────────────────────────
    def _default_dir(self):
        """The effective default export folder: the configured one when it still
        exists, else the app's data/Shorts (5.5.1c fallback - Export never opens a
        folder picker)."""
        saved = (self.app.config.get("output_dirs", {}) or {}).get("Shorts", "")
        if saved and os.path.isdir(saved):
            return saved
        return self.app.paths.get_path("Shorts")

    def _refresh_default_label(self):
        self._default_label.configure(text=self._default_dir(),
                                      fg=PALETTE["text_dim"])

    def _refresh_create_folder_btn(self):
        """Grey out 'Create shorts_export folder' once it already exists on disk -
        clicking Create again would be a no-op (5.5.5 follow-up, mirrored in Import)."""
        exists = os.path.isdir(self.app.paths.get_path("export", create=False))
        self._create_folder_btn.configure(state="disabled" if exists else "normal")

    def _set_default(self, folder):
        self.app.config.setdefault("output_dirs", {})["Shorts"] = folder
        self.app.save_config()
        self._refresh_default_label()

    def _set_default_folder(self):
        chosen = filedialog.askdirectory(title="Choose the default export folder")
        if chosen:
            self._set_default(chosen)

    def _create_shorts_folder(self):
        """Create `shorts_export` in the HERMES data folder and set it as the default -
        after asking the user's permission. No folder picker. (In dev that's beside
        main.py; in an installed build it's under %LOCALAPPDATA%\\HERMES.)"""
        target = self.app.paths.get_path("export", create=False)
        if not messagebox.askyesno(
                "Create export folder",
                f"Create this folder and set it as your default export location?\n\n{target}"):
            return
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as ex:
            self._status.configure(text=f"Couldn't create folder: {ex}", fg=PALETTE["red"])
            return
        self._set_default(target)
        self._refresh_create_folder_btn()
        self._status.configure(text=f"Created and set default: {target}", fg=PALETTE["green"])

    def _dest_dir(self):
        """Folder to export into. The configured default wins; without one the app's
        data/Shorts is used and persisted as the default (5.5.1c: Export never opens
        a surprise folder picker - "Set default folder…" is the only picker)."""
        dest = self._default_dir()
        saved = (self.app.config.get("output_dirs", {}) or {}).get("Shorts", "")
        if dest != saved:
            self._set_default(dest)   # first export: the fallback becomes the default
        return dest

    # ── Export (threaded validated copy) ─────────────────────────────────────────────
    def _export(self):
        if not self._current_path:
            self._status.configure(text="Nothing to export yet - import a clip first.",
                                   fg=PALETTE["text_mute"])
            return
        # 5.5.1c: exporting the raw import while the editor is enabled is usually a
        # missed render - ask once. Editor disabled = raw export is deliberate.
        if (self._current_dtype == "raw_clip"
                and registry.is_enabled("editor", self.app.config)):
            if not messagebox.askyesno(
                    "Export raw clip?",
                    "This clip has not been rendered in the editor yet. "
                    "Export the raw version?"):
                self._status.configure(text="Export cancelled.",
                                       fg=PALETTE["text_mute"])
                return
        dest = self._dest_dir()
        if not dest:
            self._status.configure(text="Export cancelled.", fg=PALETTE["text_mute"])
            return

        src_stem, ext = os.path.splitext(os.path.basename(self._current_path))
        ext = ext or ".mp4"
        custom = self._sanitize_filename(self._name_var.get())
        if custom:
            name = f"{custom}{ext}"                 # user-named: exact name, no timestamp
        else:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            name = f"{src_stem}_{ts}{ext}"          # automatic timestamped name
        # unique_path still guards against clobbering an existing file (adds a suffix).
        out_path = self.app.paths.unique_path(os.path.join(dest, name))

        src = self._current_path  # snapshot for the worker (no widget reads off-thread)
        self._exporting = True
        self._export_btn.configure(state="disabled")
        self._status.configure(text="Exporting…", fg=PALETTE["text_dim"])
        threading.Thread(target=self._copy_worker, args=(src, out_path),
                         daemon=True).start()

    def _copy_worker(self, src, out_path):
        """Background thread: copy + validate. NO tkinter here - result marshalled back."""
        ok, err = False, ""
        try:
            shutil.copy2(src, out_path)  # preserves bytes + mtime; no re-encode
            cap = cv2.VideoCapture(out_path)
            try:
                if not cap.isOpened():
                    err = "copied file couldn't be opened"
                else:
                    read_ok, _ = cap.read()
                    ok = bool(read_ok)
                    if not ok:
                        err = "copied file couldn't be decoded"
            finally:
                cap.release()
        except Exception as ex:  # noqa: BLE001 - never crash the hub on a copy failure
            ok, err = False, str(ex)

        self._marshal(self._copy_done, ok, out_path, err)

    def _copy_done(self, ok, out_path, err):
        """Main thread: report result, publish on success, re-enable the button."""
        self._exporting = False
        self._export_btn.configure(state="normal")
        if not ok:
            self._status.configure(text=f"Export failed: {err}", fg=PALETTE["red"])
            return
        self._last_dest = os.path.dirname(out_path)
        self._last_export_path = out_path   # share/preview/reveal now target the finished file
        self._refresh_share_enabled()
        self._status.configure(text=f"Exported ✓ {out_path} - published as final_file",
                               fg=PALETTE["green"])
        self.bus.publish("final_file", out_path)

        # Value-moment nudge (4.4): count COMPLETED exports; one-shot thank-you at NUDGE_AFTER.
        # Incremented ONLY here (on success), never on the failure branch and never in the editor.
        stats = self.app.config.setdefault("stats", {})
        stats["full_exports"] = stats.get("full_exports", 0) + 1
        nudges = self.app.config.setdefault("nudges", {})
        if stats["full_exports"] >= NUDGE_AFTER and not nudges.get("export_thankyou_shown"):
            nudges["export_thankyou_shown"] = True
            show_toast(self.app.root, _NUDGE_TITLE, _NUDGE_BODY,
                       action_label=_kofi_action_label(self.app),
                       action=_kofi_action(self.app))
        self.app.save_config()

        self._show_open_folder(self._last_dest)

    def _show_open_folder(self, dest):
        """Point the Open-folder button at `dest` and pack it if it isn't yet
        (export success and compress success share this)."""
        if hasattr(os, "startfile"):
            self._open_btn.configure(command=lambda d=dest: os.startfile(d))
            if not self._open_shown:
                self._open_btn.pack(side="left", padx=(10, 0))
                self._open_shown = True

    # ── Share compressor (5.5.1d: single-pass target-size re-encode) ─────────────────
    def _comp_target_mb(self):
        """The target-size field as a positive int, falling back to 10 on garbage."""
        try:
            mb = int(float(self._comp_mb.get()))
        except (tk.TclError, ValueError):
            mb = 10
        return mb if mb > 0 else 10

    def _on_comp_pct(self, val):
        """Slider moved: % of the source file size -> target MB. A programmatic
        slider write echoes through here via an idle callback - drop it."""
        pct = int(float(val))
        if pct == self._comp_pct_echo:
            self._comp_pct_echo = None
            return
        src_mb = self._cur_size_b / 1_000_000
        if src_mb > 0:
            self._comp_mb.set(max(1, round(src_mb * pct / 100.0)))
        self._refresh_comp_info()

    def _on_comp_mb_change(self):
        """Target MB changed (spinbox arrows, typing, or a new clip): move the %
        slider to match and refresh the info line."""
        src_mb = self._cur_size_b / 1_000_000
        if src_mb > 0:
            pct = max(1, min(100, round(self._comp_target_mb() / src_mb * 100)))
            if pct != int(self._comp_pct.get()):
                self._comp_pct_echo = pct
                self._comp_pct.set(pct)
        self._refresh_comp_info()

    def _refresh_comp_info(self):
        """The compress info line: source size + the approximate output size. The
        encoder is budgeted at 92% of the target and rate-capped there, so real
        files land at or under the estimate (a size-limited upload never bounces)."""
        if not hasattr(self, "_comp_info"):
            return   # slider callback during construction, before the label exists
        if not self._current_path or self._cur_size_b <= 0:
            self._comp_info.configure(text="")
            return
        src_mb = self._cur_size_b / 1_000_000
        mb = self._comp_target_mb()
        if self._cur_dur and video_utils.compress_kbps(mb, self._cur_dur) is None:
            self._comp_info.configure(
                text=f"Source: {src_mb:.1f} MB · target too small for this clip "
                     f"length", fg=PALETTE["red_soft"])
            return
        self._comp_info.configure(
            text=f"Source: {src_mb:.1f} MB · expected output: about "
                 f"{mb * 0.92:.1f} MB or less", fg=PALETTE["text_faint"])

    def _compress(self):
        """Compress the CURRENT clip to the target size as a separate copy. A side
        artifact by design: not published on the bus and not adopted as the current
        clip - the normal export stays untouched."""
        if not self._current_path:
            self._status.configure(text="Nothing to compress yet - import a clip "
                                        "first.", fg=PALETTE["text_mute"])
            return
        if self._compressing:
            self._status.configure(text="A compression is already running.",
                                   fg=PALETTE["text_mute"])
            return
        mb = self._comp_target_mb()
        meta = video_utils.probe_video(self._current_path)
        if meta is None or meta[2] <= 0 or meta[3] <= 0:
            self._status.configure(text="Couldn't read the clip length - can't "
                                        "compress.", fg=PALETTE["red"])
            return
        _w, _h, fps, n = meta
        kbps = video_utils.compress_kbps(mb, n / fps)
        if kbps is None:
            self._status.configure(text="Target too small for this clip length. "
                                        "Raise the target size.", fg=PALETTE["red"])
            return
        # A typed "Save as" name carries over to the compressed copy; the target-size
        # suffix always distinguishes it from the normal export.
        stem = (self._sanitize_filename(self._name_var.get())
                or os.path.splitext(os.path.basename(self._current_path))[0])
        out_path = self.app.paths.unique_path(
            os.path.join(self._dest_dir(), f"{stem}_{mb}MB.mp4"))
        src = self._current_path   # snapshot for the worker (no widget reads off-thread)
        self._compressing = True
        self._comp_btn.configure(state="disabled")
        self._status.configure(text=f"Compressing… target {mb} MB",
                               fg=PALETTE["text_dim"])
        threading.Thread(target=self._compress_worker, args=(src, out_path, kbps, mb),
                         daemon=True).start()

    def _compress_worker(self, src, out_path, kbps, mb):
        """Background thread: re-encode. NO tkinter here - result marshalled back.
        A failed encode removes its partial file before reporting."""
        ok, err = video_utils.compress_to_size(src, out_path, kbps)
        if not ok:
            try:
                os.remove(out_path)
            except OSError:
                pass
        self._marshal(self._compress_done, ok, out_path, err, mb)

    def _compress_done(self, ok, out_path, err, mb):
        """Main thread: report the result and re-enable the button. Keeps
        _compressing truthful on every exit path (close guard, 2.8.3)."""
        self._compressing = False
        self._comp_btn.configure(state="normal")
        if not ok:
            self._status.configure(text=f"Compression failed: {err}",
                                   fg=PALETTE["red"])
            return
        try:
            size_b = os.path.getsize(out_path)
        except OSError:
            size_b = 0
        size_mb = size_b / 1_000_000
        if size_b <= mb * 1_000_000:
            self._status.configure(text=f"Compressed to {size_mb:.1f} MB - "
                                        f"{out_path}", fg=PALETTE["green"])
        else:
            self._status.configure(text=f"Compressed to {size_mb:.1f} MB - over the "
                                        f"{mb} MB target - {out_path}",
                                   fg=PALETTE["yellow"])
        self._show_open_folder(os.path.dirname(out_path))

    # ── Share / preview / reveal (3.2) - all UI-thread, stdlib OS hand-offs ───────────
    def _has_file(self):
        """True when there's something to preview/share/reveal: a finished export or the
        currently-loaded clip."""
        return bool(self._last_export_path or self._current_path)

    def _refresh_share_enabled(self):
        """Enable the file-dependent controls (preview, share buttons, reveal) only when a
        file exists; copy slots are independent and stay live."""
        state = "normal" if self._has_file() else "disabled"
        if self._preview_btn is not None:
            self._preview_btn.configure(state=state)
        if self._reveal_btn is not None:
            self._reveal_btn.configure(state=state)
        for btn in self._share_btns:
            btn.configure(state=state)

    def _share(self, url):
        """V1.0 quick-share: open the target site's upload page AND reveal the exported file
        for drag-in. The ONE seam a real uploader (Phase 9) slots behind - keep all share
        side effects here. No API, no account, no network beyond opening a browser tab."""
        if url:
            webbrowser.open(url, new=2)      # new tab, default browser
        self._reveal_file()                  # folder open, file selected

    def _preview(self):
        """Test-watch the finished file (else the current clip) in the OS default player -
        no embedded player, no re-encode. Prefers the export so the user sees exactly what
        shipped (including editor sharpen)."""
        target = self._last_export_path or self._current_path
        if not target or not os.path.exists(target):
            self._status.configure(text="Nothing to preview yet.", fg=PALETTE["text_mute"])
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(target)         # Windows: default video player
            else:
                webbrowser.open(f"file://{target}")   # cross-platform fallback
        except OSError:
            self._status.configure(text="Couldn't open the player.", fg=PALETTE["red"])

    def _reveal_file(self):
        """Reveal the finished export in the file manager (selected) so the user can drag it
        into the upload page. Before any export, open the chosen DESTINATION folder instead
        (where the export will land) - never the source clip's location. Never raises (a
        failed reveal must not crash the hub)."""
        export = self._last_export_path
        if export and os.path.exists(export):
            target, select = export, True       # the exported file, selected in its folder
        else:
            dest = (self.app.config.get("output_dirs", {}) or {}).get("Shorts", "")
            if not (dest and os.path.isdir(dest)):
                self._status.configure(
                    text="No export yet - set a destination folder or export first.",
                    fg=PALETTE["text_mute"])
                return
            target, select = dest, False        # open the destination folder itself
        try:
            if os.name == "nt":
                norm = os.path.normpath(target)
                # explorer.exe is finicky: "/select," must sit OUTSIDE the quotes with only
                # the PATH quoted. Passing a LIST lets subprocess quote the whole
                # "/select,<path>" token whenever the path has spaces/brackets, which Explorer
                # can't parse - it silently opens Documents instead. A command STRING gives the
                # exact quoting we need. (Explorer returns a nonzero code even on success;
                # Popen doesn't wait, so we never see it.)
                cmd = f'explorer /select,"{norm}"' if select else f'explorer "{norm}"'
                subprocess.Popen(cmd)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", target] if select else ["open", target])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(target) if select else target])
        except Exception:   # noqa: BLE001 - a failed reveal must never crash the hub
            self._status.configure(text="Couldn't open the file manager.", fg=PALETTE["red"])

    # ── Editable share buttons (3.2) ─────────────────────────────────────────────────
    def _share_buttons_cfg(self):
        """The configured share buttons (defaults if the key is missing/invalid). An empty
        list is respected - the user may have removed them all."""
        cfg = (self.app.config.get("exporter") or {}).get("share_buttons")
        return cfg if isinstance(cfg, list) else [dict(b) for b in DEFAULT_SHARE_BUTTONS]

    def _build_share_buttons(self):
        """(Re)populate the share-button column from config - destroy + rebuild so Save/Reset
        in the edit dialog just call this again."""
        for w in self._share_frame.winfo_children():
            w.destroy()
        self._share_btns = []
        for b in self._share_buttons_cfg():
            label, url = b.get("label", "?"), b.get("url", "")
            btn = themed_button(self._share_frame, label,
                                lambda u=url: self._share(u), kind="neutral",
                                disabledforeground=PALETTE["text_faint"])
            btn.pack(fill="x", pady=2)
            self._share_btns.append(btn)
        if not self._share_btns:
            tk.Label(self._share_frame, text="No share buttons - add some with Edit.",
                     bg=PALETTE["bg"], fg=PALETTE["text_faint"], font=ui_font(8),
                     anchor="w").pack(fill="x")
        self._refresh_share_enabled()

    def _edit_share_buttons(self):
        """A small themed modal to add/remove/rename share buttons and change their URLs,
        plus reset to defaults. Save drops blank rows and rebuilds the column in place."""
        dlg = tk.Toplevel(self)
        dlg.title("Edit share buttons")
        dlg.configure(bg=PALETTE["bg"])
        dlg.transient(self.winfo_toplevel())
        dlg.resizable(False, False)
        wrap = tk.Frame(dlg, bg=PALETTE["bg"])
        wrap.pack(fill="both", expand=True, padx=16, pady=14)
        tk.Label(wrap, text="Share buttons", bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=display_font(14)).pack(anchor="w", pady=(0, 4))
        tk.Label(wrap, text="Each button opens its link and reveals the exported file to "
                            "drag in. Tip: Instagram has no web upload page, so its button "
                            "just opens instagram.com.", bg=PALETTE["bg"],
                 fg=PALETTE["text_mute"], font=ui_font(9), wraplength=420,
                 justify="left").pack(anchor="w", pady=(0, 10))
        rows_frame = tk.Frame(wrap, bg=PALETTE["bg"])
        rows_frame.pack(fill="x")
        rows = []   # list of [label_var, url_var, row_frame]

        def add_row(label="", url=""):
            if len(rows) >= 8:        # keep the column sane
                return
            rf = tk.Frame(rows_frame, bg=PALETTE["bg"])
            rf.pack(fill="x", pady=3)
            lv, uv = tk.StringVar(value=label), tk.StringVar(value=url)
            ttk.Entry(rf, textvariable=lv, width=12).pack(side="left")
            ttk.Entry(rf, textvariable=uv, width=32).pack(side="left", padx=(6, 6))
            entry = [lv, uv, rf]
            themed_button(rf, "Remove", lambda e=entry: remove_row(e),
                          kind="danger").pack(side="left")
            rows.append(entry)

        def remove_row(entry):
            entry[2].destroy()
            rows.remove(entry)

        def reset():
            for e in list(rows):
                e[2].destroy()
            rows.clear()
            for b in DEFAULT_SHARE_BUTTONS:
                add_row(b["label"], b["url"])

        def save():
            new = [{"label": lv.get().strip(), "url": uv.get().strip()}
                   for lv, uv, _rf in rows
                   if lv.get().strip() and uv.get().strip()]
            self.app.config.setdefault("exporter", {})["share_buttons"] = new
            self.app.save_config()
            self._build_share_buttons()
            dlg.destroy()

        for b in self._share_buttons_cfg():
            add_row(b.get("label", ""), b.get("url", ""))

        btns = tk.Frame(wrap, bg=PALETTE["bg"])
        btns.pack(fill="x", pady=(12, 0))
        themed_button(btns, "Add button", add_row, kind="neutral").pack(side="left")
        themed_button(btns, "Reset to defaults", reset, kind="neutral").pack(
            side="left", padx=(8, 0))
        themed_button(btns, "Save", save, kind="primary").pack(side="right")
        themed_button(btns, "Cancel", dlg.destroy, kind="neutral").pack(
            side="right", padx=(0, 8))
        dlg.update_idletasks()
        dlg.grab_set()
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

    # ── Text snippets (3.2; 5.5.2e: extendable, min 5) ───────────────────────────────
    def _snippet_count(self):
        """Current number of snippet slots: the SNIPPET_MIN floor or the saved list
        length, whichever is larger (5.5.2e). Existing configs (10) keep their rows;
        fresh configs start at five."""
        saved = (self.app.config.get("exporter") or {}).get("copy_slots") or []
        return max(SNIPPET_MIN, len(saved))

    def _get_copy_slots(self):
        """The copy-slot list, normalized to exactly _snippet_count() plain strings."""
        slots = list((self.app.config.get("exporter") or {}).get("copy_slots") or [])
        slots = [s if isinstance(s, str) else "" for s in slots]
        count = self._snippet_count()
        return (slots + [""] * count)[:count]

    _HELP_SNIPPETS = (
        "Reusable text snippets, for example your video title, description, hashtags "
        "or a standard reply.\n"
        "Click a filled row to copy it to the clipboard, then paste it on the upload "
        "page.\n"
        "Edit (or clicking an empty row) opens the row for typing; Save or Enter "
        "stores it. Add slot / Delete last slot change how many you keep (minimum "
        "five).\n"
        "Snippets are saved in your settings and survive restarts.")

    def _build_copy_slots(self):
        """(Re)render the slot rows from config. Locked rows click-to-copy; the open row (at
        _unlocked_slot) shows an Entry + Save."""
        for w in self._slots_frame.winfo_children():
            w.destroy()
        for i, text in enumerate(self._get_copy_slots()):
            row = tk.Frame(self._slots_frame, bg=PALETTE["bg"])
            row.pack(fill="x", pady=2)
            if i == self._unlocked_slot:
                var = tk.StringVar(value=text)
                ent = ttk.Entry(row, textvariable=var)
                ent.pack(side="left", fill="x", expand=True)
                ent.focus_set()
                ent.bind("<Return>", lambda _e, idx=i, v=var: self._save_slot(idx, v.get()))
                themed_button(row, "Save", lambda idx=i, v=var: self._save_slot(idx, v.get()),
                              kind="primary").pack(side="left", padx=(6, 0))
            else:
                edit = themed_button(row, "Edit", lambda idx=i: self._unlock_slot(idx),
                                     kind="neutral", width=4)
                edit.pack(side="left")
                if text:
                    lbl = tk.Label(row, text=self._truncate(text), bg=PALETTE["card"],
                                   fg=PALETTE["text_dim"], font=mono_font(9), anchor="w",
                                   cursor="hand2", padx=8, pady=4)
                    lbl.bind("<Button-1>", lambda _e, idx=i: self._copy_slot(idx))
                    Tooltip(lbl, "Click to copy to clipboard.")
                else:
                    lbl = tk.Label(row, text="empty slot - click to add", bg=PALETTE["bg"],
                                   fg=PALETTE["text_faint"], font=ui_font(9), anchor="w",
                                   cursor="hand2")
                    lbl.bind("<Button-1>", lambda _e, idx=i: self._unlock_slot(idx))
                    Tooltip(lbl, "Click to add a snippet.")
                lbl.pack(side="left", fill="x", expand=True, padx=(6, 0))

    def _truncate(self, text, n=40):
        text = text.replace("\n", " ")
        return text if len(text) <= n else text[:n - 1] + "…"

    def _copy_slot(self, i):
        """Copy slot i's full text to the clipboard (an empty slot opens for editing instead)."""
        text = self._get_copy_slots()[i]
        if not text:
            self._unlock_slot(i)
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._status.configure(text="Copied to clipboard.", fg=PALETTE["green"])

    def _unlock_slot(self, i):
        self._unlocked_slot = i
        self._build_copy_slots()

    def _save_slot(self, i, value):
        slots = self._get_copy_slots()
        slots[i] = value.strip()
        self.app.config.setdefault("exporter", {})["copy_slots"] = slots
        self.app.save_config()
        self._unlocked_slot = None
        self._build_copy_slots()

    def _add_snippet(self):
        """Append one empty snippet slot (5.5.2e). No maximum."""
        slots = self._get_copy_slots()
        slots.append("")
        self.app.config.setdefault("exporter", {})["copy_slots"] = slots
        self.app.save_config()
        self._build_copy_slots()
        self._status.configure(text=f"Added snippet slot {len(slots)}.",
                               fg=PALETTE["green"])

    def _delete_last_snippet(self):
        """Remove the last snippet slot (5.5.2e), refusing below the floor and
        confirming when it holds text."""
        slots = self._get_copy_slots()
        allowed, needs_confirm, preview = snippet_delete_check(slots)
        if not allowed:
            self._status.configure(text=f"{SNIPPET_MIN} slots minimum.",
                                   fg=PALETTE["text_mute"])
            return
        n = len(slots)
        if needs_confirm and not messagebox.askyesno(
                f"Delete slot {n}?",
                f"Slot {n} holds '{preview}'. Delete it?", parent=self):
            return
        slots.pop()
        # If the open editor was on the removed slot, close it.
        if self._unlocked_slot is not None and self._unlocked_slot >= len(slots):
            self._unlocked_slot = None
        self.app.config.setdefault("exporter", {})["copy_slots"] = slots
        self.app.save_config()
        self._build_copy_slots()
        self._status.configure(text=f"Deleted snippet slot {n}.", fg=PALETTE["green"])

    # ── Worker→UI marshalling (the editor's queue + pump pattern) ────────────────────
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

    # ── Helpers ──────────────────────────────────────────────────────────────────────
    @staticmethod
    def _sanitize_filename(name):
        """Reduce a user-supplied output name to a safe Windows filename stem: a typed video
        extension dropped, every reserved character (including path separators) removed, no
        control chars, no trailing dot/space. Returns '' when nothing usable remains (caller
        falls back to the auto name). Separators are stripped, not split on, so "fight 1/2"
        becomes "fight 12" rather than "2"."""
        name = name.strip()
        root, ext = os.path.splitext(name)
        if ext.lower() in (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"):
            name = root                                 # strip a typed video extension
        for ch in '<>:"/\\|?*':
            name = name.replace(ch, "")
        name = "".join(c for c in name if ord(c) >= 32)  # drop control characters
        return name.strip().rstrip(". ")

    @staticmethod
    def _fmt_duration(duration):
        if duration is None:
            return "duration unknown"
        if duration >= 60:
            m, s = divmod(int(round(duration)), 60)
            return f"{m}:{s:02d}"
        return f"{duration:.1f}s"

    # ── Hub contract / lifecycle ─────────────────────────────────────────────────────
    def has_unsaved(self):
        """Hub close-guard hook (2.8.3)."""
        if self._exporting:
            return "export in progress"
        if self._compressing:
            return "compression in progress"
        return None

    def receive_handoff(self, data_type, path):
        """Thin wrapper for the hub's push path (e.g. a future 'Send to Export').
        Adopts whatever it's handed and labels the source with the handed type."""
        if path:
            self._set_current(path, data_type)

    def on_close(self):
        """Signal the daemon copy thread to skip its UI callback; cancel the pump job
        (a pending after firing post-destroy throws a Tcl error)."""
        self._closing = True
        if self._ui_pump_job is not None:
            self.after_cancel(self._ui_pump_job)
            self._ui_pump_job = None
