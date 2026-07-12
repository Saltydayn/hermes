"""Import tab - entry point for media (Phase 1: file import + URL import).

Loads a clip either from a local file browser or from a URL (Twitch/YouTube/etc. via
yt-dlp), validates it is a real, decodable video with cv2, shows basic info plus one
preview frame, and publishes it on the bus as `raw_clip`. This is the start of the whole
app's media loop.

`_load_clip(path)` is the single, reusable load entry point used by BOTH paths: file
import calls it directly; URL import downloads on a worker thread then calls it. Imports
ONLY from shared/, stdlib, and third-party - never from other modules. yt-dlp is imported
lazily inside the worker so plain file import still works even if it's missing.
"""

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
from PIL import Image, ImageTk

from shared import paths, video_utils
from shared.ui_helpers import (
    HelpDialog, PALETTE, ScrollableFrame, Tooltip, TutorialIntro, TutorialReplayButton,
    TutorialWalkthrough, display_font, mono_font, themed_button, ui_font,
)

PREVIEW_BOX = (320, 180)  # max preview size (16:9 fits in this box, aspect kept)
_QUALITY_CHOICES = ["Auto", "1440p", "1080p", "936p", "720p"]  # Auto = native, no cap
_HEIGHT_CAP = {"1440p": 1440, "1080p": 1080, "936p": 936, "720p": 720}
_URL_PLACEHOLDER = "Paste a Twitch / YouTube / Kick / … link"

_LIST_MAX = 5  # cap on the RECENT and NEW CLIPS lists (both capped + collapsible)
_VIDEO_EXT_ORDER = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".ts")
_VIDEO_EXTS = set(_VIDEO_EXT_ORDER)
_THUMB_BOX = (72, 40)  # small "explorer style" thumbnail for RECENT / NEW CLIPS rows

# Long-file guardrail thresholds in seconds (5.5.2c). Each boundary belongs to the
# LOWER tier (<=), so 90/300/600 are still tiers 0/1/2 respectively.
_TIER_90S, _TIER_5MIN, _TIER_10MIN = 90.0, 300.0, 600.0


def duration_tier(duration_s):
    """Long-file warning tier for a clip length in seconds (5.5.2c):
    0: <= 90s (normal, no warning)
    1: 90s to 5min (one confirm)
    2: 5min to 10min (two confirms)
    3: > 10min (two blunt confirms).
    Boundaries (exactly 90s / 300s / 600s) belong to the LOWER tier."""
    if duration_s <= _TIER_90S:
        return 0
    if duration_s <= _TIER_5MIN:
        return 1
    if duration_s <= _TIER_10MIN:
        return 2
    return 3


def _mmss(duration_s):
    """Minutes:seconds with the word 'minutes' for the guardrail dialogs
    (e.g. 7:24 minutes). Guardrail input is always > 90s, so minutes are always shown."""
    m, s = divmod(int(round(duration_s)), 60)
    return f"{m}:{s:02d} minutes"


class _Cancelled(Exception):
    """Raised inside the progress hook to abort a yt-dlp download on user cancel."""


def _short_reason(ex):
    """Trim a yt-dlp/network exception to one readable line for the status label."""
    text = str(ex).strip().split("\n")[0].replace("ERROR:", "").strip()
    return text[:200] if text else ex.__class__.__name__


class ImporterModule(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.bus = app.bus

        self._current_path = None     # last successfully loaded clip
        self._preview_imgtk = None    # keep a ref or Tk garbage-collects the image
        self._cancel_flag = None      # threading.Event for the active download (or None)
        self._downloading = False
        self._pb_pulsing = False      # is the progressbar in indeterminate (pulse) mode?
        self._url_placeholder_on = False
        self._closing = False
        self._detect_scanned = False  # 5.5.5: guards the one-shot new-clip scan per launch
        self._new_clips_shown = []    # 5.5.5 follow-up: session display list, see _scan_new_clips
        self._clip_preview_cache = {}  # path -> (PhotoImage or None, meta text); session-cached
        self._ui_queue = queue.Queue()    # worker→UI results (see _marshal)
        self._ui_pump_job = None
        self._build()
        # Worker→UI results pump (download progress/done - see _marshal).
        self._ui_pump_job = self.after(50, self._ui_pump)
        self.bind("<Map>", self._on_map)

    # ── UI construction ──────────────────────────────────────────────────────────────
    def _build(self):
        # 1.3 adds a URL section below, so host everything in a ScrollableFrame now.
        self.scroll = ScrollableFrame(self)
        self.scroll.pack(fill="both", expand=True)
        body = self.scroll.body

        pad = {"padx": 26}

        # Header row.
        header = tk.Frame(body, bg=PALETTE["bg"])
        header.pack(fill="x", pady=(22, 0), **pad)
        tk.Label(header, text="Import", bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=display_font(20)).pack(anchor="w")
        bar = tk.Frame(header, bg=PALETTE["blue"], width=66, height=3)  # module accent
        bar.pack(anchor="w", pady=(7, 0))
        bar.pack_propagate(False)
        tk.Label(header, text="Bring a clip in - from a file on disk or from a link.",
                 bg=PALETTE["bg"], fg=PALETTE["text_mute"], font=ui_font(10)).pack(
                     anchor="w", pady=(8, 0))

        # File-import row: button + chosen-path label.
        self._from_file_lbl = tk.Label(
            body, text="FROM FILE", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
            font=ui_font(9))
        self._from_file_lbl.pack(anchor="w", pady=(18, 6), **pad)
        row = tk.Frame(body, bg=PALETTE["bg"])
        row.pack(fill="x", **pad)
        self._choose_file_btn = themed_button(row, "Choose file…", self._choose_file,
                                              kind="primary")
        self._choose_file_btn.pack(side="left")
        self._path_label = tk.Label(row, text="no file loaded", bg=PALETTE["bg"],
                                    fg=PALETTE["text_mute"], font=mono_font(9), anchor="w")
        self._path_label.pack(side="left", fill="x", expand=True, padx=(14, 0))

        # Default import folder (5.5.5): remembered start dir for the file picker. Falls
        # back to the app's own shorts_import folder when set (5.5.5 follow-up), mirroring
        # the exporter's shorts_export.
        self._folder_row = folder_row = tk.Frame(body, bg=PALETTE["bg"])
        folder_row.pack(fill="x", pady=(8, 0), **pad)
        themed_button(folder_row, "Default folder…", self._choose_default_folder,
                     kind="neutral").pack(side="left")
        themed_button(folder_row, "Clear", self._clear_default_folder,
                     kind="neutral").pack(side="left", padx=(6, 0))
        self._create_folder_btn = themed_button(
            folder_row, "Create 'shorts_import' folder", self._create_import_folder,
            kind="neutral", disabledforeground=PALETTE["text_faint"])
        self._create_folder_btn.pack(side="left", padx=(6, 0))
        self._folder_label = tk.Label(folder_row, text="", bg=PALETTE["bg"],
                                      fg=PALETTE["text_mute"], font=mono_font(9), anchor="w")
        self._folder_label.pack(side="left", fill="x", expand=True, padx=(14, 0))

        # Recent files (5.5.5): hidden entirely when the list is empty (see _refresh_recent).
        # Capped + collapsible (follow-up) so it never floods the tab.
        self._recent_wrap = tk.Frame(body, bg=PALETTE["bg"])
        self._recent_row, self._recent_title_lbl = self._make_collapsible_row(
            self._recent_wrap, "RECENT", "recent_collapsed")

        # Drag-and-drop hint (text filled in by _setup_dnd only if DnD is available).
        self._dnd_hint = tk.Label(body, text="", bg=PALETTE["bg"], fg=PALETTE["text_faint"],
                                  font=ui_font(9))
        self._dnd_hint.pack(anchor="w", pady=(6, 0), **pad)

        # ── Detect new clips (5.5.5) ─────────────────────────────────────────────────
        self._detect_lbl = tk.Label(
            body, text="DETECT NEW CLIPS", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
            font=ui_font(9))
        self._detect_lbl.pack(anchor="w", pady=(18, 6), **pad)

        detect_row = tk.Frame(body, bg=PALETTE["bg"])
        detect_row.pack(fill="x", **pad)
        detect_on = bool(self.app.config.get("importer", {}).get("detect_new_clips", False))
        self._detect_var = tk.BooleanVar(value=detect_on)
        detect_cb = ttk.Checkbutton(detect_row, text="Detect new clips in my default folder",
                                    variable=self._detect_var, command=self._toggle_detect)
        detect_cb.pack(side="left")
        help_btn = themed_button(detect_row, "?", command=self._show_detect_help,
                                 kind="neutral", width=2)
        help_btn.pack(side="left", padx=(8, 0))
        themed_button(detect_row, "Reload", self._scan_new_clips,
                     kind="neutral").pack(side="right")

        tk.Label(body, text="When on, HERMES checks your default folder for new clips the "
                             "first time you open this tab after starting the app. Nothing "
                             "is uploaded or moved; it just lists them here for one click.",
                 bg=PALETTE["bg"], fg=PALETTE["text_faint"], font=ui_font(9), justify="left",
                 wraplength=560).pack(anchor="w", pady=(6, 0), **pad)

        self._detect_status = tk.Label(body, text="", bg=PALETTE["bg"],
                                       fg=PALETTE["text_mute"], font=ui_font(9), anchor="w")
        self._detect_status.pack(anchor="w", pady=(6, 0), **pad)

        # New-clips list (5.5.5): hidden entirely when empty (see _scan_new_clips).
        # Capped + collapsible (follow-up) so it never floods the tab.
        self._new_clips_wrap = tk.Frame(body, bg=PALETTE["bg"])
        self._new_clips_row, self._new_clips_title_lbl = self._make_collapsible_row(
            self._new_clips_wrap, "NEW CLIPS", "new_clips_collapsed")

        # Info / preview card.
        card = tk.Frame(body, bg=PALETTE["card"], highlightbackground=PALETTE["border"],
                        highlightthickness=1, bd=0)
        card.pack(fill="x", pady=(18, 0), **pad)
        inner = tk.Frame(card, bg=PALETTE["card"])
        inner.pack(fill="x", padx=14, pady=14)

        self._preview_label = tk.Label(
            inner, text="preview will appear here", bg=PALETTE["deep"],
            fg=PALETTE["text_faint"], font=ui_font(9), width=44, height=11)
        self._preview_label.pack(side="left")

        self._meta_label = tk.Label(
            inner, text="No clip loaded.", bg=PALETTE["card"], fg=PALETTE["text_dim"],
            font=mono_font(10), justify="left", anchor="nw")
        self._meta_label.pack(side="left", fill="both", expand=True, padx=(16, 0))

        # ── Import-from-URL section ──────────────────────────────────────────────────
        self._url_separator = ttk.Separator(body, orient="horizontal")
        self._url_separator.pack(fill="x", pady=(20, 0), **pad)
        self._from_url_lbl = tk.Label(
            body, text="FROM URL", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
            font=ui_font(9))
        self._from_url_lbl.pack(anchor="w", pady=(14, 6), **pad)

        url_row = tk.Frame(body, bg=PALETTE["bg"])
        url_row.pack(fill="x", **pad)
        self._url_entry = tk.Entry(
            url_row, font=mono_font(10), bg=PALETTE["panel"], fg=PALETTE["text"],
            insertbackground=PALETTE["text"], relief="flat", highlightthickness=1,
            highlightbackground=PALETTE["border"], highlightcolor=PALETTE["green"])
        self._url_entry.pack(fill="x", ipady=4)
        self._url_entry.bind("<Return>", lambda _e: self._start_download())
        self._url_entry.bind("<FocusIn>", self._url_focus_in)
        self._url_entry.bind("<FocusOut>", self._url_focus_out)
        self._show_url_placeholder()

        ctrl = tk.Frame(body, bg=PALETTE["bg"])
        ctrl.pack(fill="x", pady=(10, 0), **pad)
        tk.Label(ctrl, text="Quality:", bg=PALETTE["bg"], fg=PALETTE["text_dim"],
                 font=ui_font(10)).pack(side="left")
        self._quality = ttk.Combobox(ctrl, state="readonly", width=8,
                                     values=_QUALITY_CHOICES)
        self._quality.set("Auto")
        self._quality.pack(side="left", padx=(8, 0))
        self._download_btn = themed_button(ctrl, "Download", self._start_download,
                                           kind="primary")
        self._download_btn.pack(side="left", padx=(12, 0))
        self._cancel_btn = themed_button(ctrl, "Cancel", self._cancel, kind="danger",
                                         disabledforeground=PALETTE["text_faint"])
        self._cancel_btn.configure(state="disabled")
        self._cancel_btn.pack(side="left", padx=(8, 0))

        prog = tk.Frame(body, bg=PALETTE["bg"])
        prog.pack(fill="x", pady=(10, 0), **pad)
        self._progress = ttk.Progressbar(prog, orient="horizontal",
                                         mode="determinate", maximum=100)
        self._progress.pack(side="left", fill="x", expand=True)
        self._progress_label = tk.Label(prog, text="", bg=PALETTE["bg"],
                                        fg=PALETTE["text_mute"], font=mono_font(9),
                                        width=24, anchor="w")
        self._progress_label.pack(side="left", padx=(10, 0))

        # Status line (shared by file import and URL import).
        self._status = tk.Label(body, text="", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                                font=ui_font(10), anchor="w")
        self._status.pack(fill="x", pady=(16, 18), **pad)

        self._setup_dnd()
        self._refresh_folder_label()
        self._refresh_create_folder_btn()
        self._refresh_recent()

        # Always-present per-tab tutorial replay button (bottom-right corner, 5.5.4b).
        TutorialReplayButton(self, self._start_walkthrough,
                             "New here? Replay the tour.")

    # ── Tutorial (5.5.4b) ────────────────────────────────────────────────────────────
    def maybe_show_tutorial(self):
        """Hub hook (main.py's <<NotebookTabChanged>> dispatch, see 5.5.4a): show the
        Import intro popup on first visit, once tutorials have been asked about and
        are on."""
        tcfg = self.app.config.get("tutorial", {})
        if not tcfg.get("asked") or not tcfg.get("enabled"):
            return
        if tcfg.get("seen", {}).get("importer"):
            return
        self._offer_tutorial()

    def _offer_tutorial(self):
        """Show Import's intro popup. Marks "seen" the moment it's shown - skipping
        and touring both count, so the intro never repeats either way."""
        tcfg = self.app.config.setdefault("tutorial", {})
        tcfg.setdefault("seen", {})["importer"] = True
        self.app.save_config()
        TutorialIntro(
            self, title="Welcome to Import",
            body=("Import is the entry point - bring in a clip from a file or a "
                  "URL, and it'll carry into the rest of the app."),
            on_show_me=self._start_walkthrough)

    def _tour_scroll_to(self, attr):
        """on_show helper: bring a named widget into view inside self.scroll before its
        step's ring is drawn (5.5.4b beta fix - a target scrolled out of the viewport
        still counts as "mapped" in Tk, so the ring landed on stale off-screen
        coordinates without this)."""
        w = getattr(self, attr, None)
        if w is not None:
            try:
                self.scroll.scroll_to(w)
            except Exception:   # noqa: BLE001 - a bad scroll target must never crash the tour
                pass

    def _start_walkthrough(self):
        """Import's 6-step tour, in on-screen top-to-bottom order. Also the entry
        point for the replay button."""
        steps = [
            {"target": lambda: getattr(self, "_choose_file_btn", None),
             "on_show": lambda: self._tour_scroll_to("_choose_file_btn"),
             "title": "From file",
             "body": "Browse for a local video file - or drag one straight onto "
                     "this tab, drag-and-drop works too."},
            {"target": lambda: getattr(self, "_folder_row", None),
             "on_show": lambda: self._tour_scroll_to("_folder_row"),
             "title": "Default folder",
             "body": "Set a folder the file browser opens to by default. 'Create "
                     "shorts_import folder' makes one for you in HERMES' own data "
                     "folder and sets it as the default in one click."},
            {"target": lambda: getattr(self, "_recent_row", None),
             "on_show": lambda: self._tour_scroll_to("_recent_row"),
             "title": "Recent clips",
             "body": "Your last few imported files, for a quick reload. Click a "
                     "row to load it again."},
            {"target": lambda: getattr(self, "_detect_lbl", None),
             "on_show": lambda: self._tour_scroll_to("_detect_lbl"),
             "title": "Detect new clips",
             "body": "Optional - watches your default folder and lists clips "
                     "added since you last checked."},
            {"target": lambda: getattr(self, "_url_entry", None),
             "on_show": lambda: self._tour_scroll_to("_url_entry"),
             "title": "From URL",
             "body": "Paste a Twitch or YouTube link here to download a clip "
                     "straight into HERMES. Needs ffmpeg."},
            {"target": lambda: getattr(self, "_quality", None),
             "on_show": lambda: self._tour_scroll_to("_quality"),
             "title": "Quality",
             "body": "Caps the download resolution. Auto (the default) keeps the "
                     "source quality and is the right choice for most clips."},
        ]
        TutorialWalkthrough(self.app.root, steps).start()

    def _setup_dnd(self):
        """Enable drag-and-drop of a video file onto the tab. Optional: if tkinterdnd2
        (or its tkdnd Tcl lib) isn't available, file import via the button still works."""
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD
            TkinterDnD._require(self.winfo_toplevel())  # load tkdnd into the live root
            for target in (self, self._preview_label):  # whole tab + the preview box
                target.drop_target_register(DND_FILES)
                target.dnd_bind("<<Drop>>", self._on_drop)
            self._dnd_hint.configure(text="…or drag a video file onto this tab.")
        except Exception as ex:  # noqa: BLE001 - DnD is a nicety; never break the tab
            print(f"[importer] drag-and-drop unavailable: {ex}")

    def _on_drop(self, event):
        """A file was dropped. Confirm before loading (5.5.1e: a mis-drop must not
        replace the current clip silently); only the first file loads. The picker
        path stays confirmation-free - choosing in a dialog is already deliberate.
        Folds in the swap warning (5.5.5) so a drop with a clip already loaded only
        asks once."""
        paths = self.tk.splitlist(event.data)  # handles {braced paths with spaces}
        if not paths:
            return
        msg = f"Load this file?\n\n{os.path.basename(paths[0])}"
        if len(paths) > 1:
            msg += f"\n({len(paths)} files dropped; only the first loads.)"
        if self._current_path and os.path.abspath(paths[0]) != os.path.abspath(self._current_path):
            msg += (f"\n\n{os.path.basename(self._current_path)} is currently loaded. "
                     f"Its edits are saved either way, but this will swap it out.")
        if messagebox.askyesno("Load clip?", msg, parent=self):
            self._load_clip(paths[0], confirm_swap=False)

    # ── Actions ──────────────────────────────────────────────────────────────────────
    def _choose_file(self):
        initial = self._effective_default_folder() or None
        pattern = " ".join(f"*{ext}" for ext in _VIDEO_EXT_ORDER)
        path = filedialog.askopenfilename(
            title="Choose a video clip",
            initialdir=initial,
            filetypes=[("Video files", pattern), ("All files", "*.*")],
        )
        if path:
            self._load_clip(path)

    def _load_clip(self, path, confirm_swap=True):
        """Validate, display, and publish a clip. The single load entry point - SPEC 1.3
        calls this after a download; SPEC 5.5.5 also feeds it from recent-files and
        detected clips. Returns True on success, False on validation failure or a
        declined swap. Never raises: a bad file must not crash the hub."""
        # 0. Swap warning (5.5.5): a friendly heads-up before replacing an already
        #    loaded clip. Edits autosave regardless (2.8.2), so nothing is lost either
        #    way - this just guards against an accidental swap. Callers that already
        #    asked (the drop handler) pass confirm_swap=False to avoid a double dialog.
        if confirm_swap and self._current_path and \
                os.path.abspath(path) != os.path.abspath(self._current_path):
            if not self._confirm_swap(path):
                return False

        # 1. Validate decodability with its own capture (distinct messages per failure).
        cap = cv2.VideoCapture(path)
        try:
            if not cap.isOpened():
                return self._fail("Couldn't open that file as a video.")
            ok, _frame = cap.read()
            if not ok:
                return self._fail("That file opened but couldn't be decoded.")
        finally:
            cap.release()

        # 2. Metadata via the shared probe (its own capture). Validation above already
        #    opened + decoded the file, so None here is effectively unreachable.
        meta = video_utils.probe_video(path)
        if meta is None:
            return self._fail("Couldn't read that file's metadata.")
        w, h, fps, n = meta

        duration = n / fps if fps > 0 and n > 0 else None

        # 2b. Long-file guardrail (5.5.2c): warn tiered by duration BEFORE any preview
        #     or publish. Never a hard block - a determined user can always confirm
        #     through. Skipped when duration is unknown (broken files already handled).
        if duration is not None and not self._guardrail_ok(duration):
            self._status.configure(text=f"Load cancelled ({_mmss(duration)} clip).",
                                   fg=PALETTE["text_mute"])
            return False

        # 3. Preview frame (shared helper opens/releases its own capture).
        self._set_preview(path)

        # 4. Metadata block.
        self._meta_label.configure(
            text=self._format_meta(path, w, h, fps, n, duration),
            fg=PALETTE["text"])
        self._current_path = path
        self._path_label.configure(text=self._elide(path), fg=PALETTE["text_dim"])

        # 5. Publish on the bus.
        self.bus.publish("raw_clip", path)

        # 6. Recent files (5.5.5): success only, so a failed load never pollutes it.
        self._record_recent(path)

        # 7. Status.
        self._status.configure(text="Loaded ✓ - published as raw_clip", fg=PALETTE["green"])
        return True

    def _confirm_swap(self, path):
        """5.5.5: ask before swapping out an already-loaded clip. True = proceed."""
        current = os.path.basename(self._current_path)
        incoming = os.path.basename(path)
        msg = (f"{current} is currently loaded. Its edits are saved either way, but "
               f"loading {incoming} will swap it out. Continue?")
        return messagebox.askyesno("Swap loaded clip?", msg, parent=self)

    def _guardrail_ok(self, duration):
        """Long-file confirmation gate (5.5.2c). Returns True to proceed, False if the
        user declines any confirmation. Tier 0 proceeds silently; tiers 1-3 ask one or
        two askyesno dialogs (parented on the tab). Never blocks - even tier 3 is two
        confirmations, not a wall."""
        tier = duration_tier(duration)
        if tier == 0:
            return True
        mmss = _mmss(duration)

        # First dialog: tiers 1-2 share the standard warning; tier 3 is blunt.
        if tier == 3:
            title1 = "Very long file"
            first = (
                f"This file is {mmss} long. HERMES is made for short clips. A file "
                f"this size can freeze the app and may be more than your RAM can hold. "
                f"Is this really the right file?")
        else:
            title1 = "Long clip"
            first = (
                f"This clip is {mmss} long. HERMES is built for short clips, and "
                f"editing may be laggy at this length right now. Long-clip performance "
                f"improves in a later update. Load it anyway?")
        if not messagebox.askyesno(title1, first, parent=self):
            return False
        if tier == 1:
            return True

        # Second dialog (tiers 2-3 only): either No aborts.
        if tier == 2:
            title2 = "Unusual length"
            second = (
                f"{mmss} is well past what the editor is tuned for. Expect slow "
                f"seeking and high memory use. Really load it?")
        else:   # tier 3
            title2 = "Last check"
            second = f"Load {mmss} of video anyway?"
        return messagebox.askyesno(title2, second, parent=self)

    def _set_preview(self, path):
        """Decode frame 0 and show a downscaled thumbnail. Leaves a placeholder if the
        frame can't be decoded (metadata validation already passed - not a failure)."""
        frame = video_utils.extract_frame(path, 0)
        if frame is None:
            self._preview_imgtk = None
            self._preview_label.configure(image="", text="no preview",
                                          fg=PALETTE["text_faint"])
            return
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        img.thumbnail(PREVIEW_BOX)
        self._preview_imgtk = ImageTk.PhotoImage(img)  # keep ref on self or it GCs
        self._preview_label.configure(image=self._preview_imgtk, text="")

    def _fail(self, msg):
        """Red status, no publish, leaves _current_path unchanged. Returns False."""
        self._status.configure(text=msg, fg=PALETTE["red"])
        return False

    # ── URL import (threaded yt-dlp download → _load_clip) ────────────────────────────
    def _format_for(self, choice):
        """yt-dlp format string for a quality choice. Auto = native res, prefer mp4/h264."""
        if choice == "Auto" or choice not in _HEIGHT_CAP:
            return "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
        n = _HEIGHT_CAP[choice]
        return (f"bv*[height<={n}][ext=mp4]+ba[ext=m4a]/"
                f"bv*[height<={n}]+ba/b[height<={n}]/b")

    def _show_url_placeholder(self):
        self._url_entry.delete(0, "end")
        self._url_entry.insert(0, _URL_PLACEHOLDER)
        self._url_entry.configure(fg=PALETTE["text_faint"])
        self._url_placeholder_on = True

    def _url_focus_in(self, _e):
        if self._url_placeholder_on:
            self._url_entry.delete(0, "end")
            self._url_entry.configure(fg=PALETTE["text"])
            self._url_placeholder_on = False

    def _url_focus_out(self, _e):
        if not self._url_entry.get().strip():
            self._show_url_placeholder()

    def _url_value(self):
        """The real URL, or '' while the grey placeholder is showing."""
        return "" if self._url_placeholder_on else self._url_entry.get().strip()

    def _start_download(self):
        """Main thread: snapshot URL + quality, pre-flight ffmpeg, spawn the worker."""
        if self._downloading:
            return
        url = self._url_value()                       # snapshot widget state on main thread
        fmt = self._format_for(self._quality.get())
        if not url:
            self._status.configure(text="Paste a URL first.", fg=PALETTE["red"])
            return
        if video_utils.find_ffmpeg() is None:
            self._status.configure(
                text="URL import needs ffmpeg (merging audio+video). "
                     "Put it on PATH or in assets/ffmpeg.exe.", fg=PALETTE["red"])
            return

        # 5.5.5 follow-up: downloads land in the effective default folder (an explicit
        # choice, or the assumed shorts_import) when one is set, else data/Downloads as
        # before. Resolved here (main thread, config is plain data - no widget reads)
        # and passed in, matching the snapshot-before-spawning convention.
        dest_dir = self._effective_default_folder() or paths.get_path("Downloads")

        self._cancel_flag = threading.Event()
        self._downloading = True
        self._download_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._reset_progress()
        self._progress_label.configure(text="")
        self._status.configure(text="Starting…", fg=PALETTE["text_dim"])
        threading.Thread(target=self._download_worker, args=(url, fmt, dest_dir),
                         daemon=True).start()

    def _download_worker(self, url, fmt, dest_dir):
        """Worker thread - NO tkinter here. Runs yt-dlp; results marshalled via _marshal."""
        import yt_dlp  # lazy: keeps file import working even if yt-dlp is absent

        ffmpeg = video_utils.find_ffmpeg()  # already checked non-None on the main thread
        outtmpl = os.path.join(dest_dir, "%(title).80s [%(id)s].%(ext)s")
        opts = {
            "format": fmt,
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",                 # mp4 container when merging
            "ffmpeg_location": os.path.dirname(ffmpeg),   # use OUR ffmpeg (PATH/assets)
            "noplaylist": True,                           # single video, never a playlist
            "restrictfilenames": True,                    # safe cross-platform filenames
            "quiet": True, "no_warnings": True,           # we drive our own UI
            "noprogress": True,                           # ...incl. our own progress bar
            "progress_hooks": [self._progress_hook],
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                # Resolve the FINAL path (post-merge/remux ext may differ from source).
                rd = (info or {}).get("requested_downloads") or []
                path = rd[0].get("filepath") if rd else ydl.prepare_filename(info)
            self._marshal(self._download_done, path, None)
        except _Cancelled:
            self._marshal(self._download_done, None, "Cancelled.")
        except Exception as ex:  # noqa: BLE001 - any failure must not crash the hub
            print(f"[importer] download failed: {ex}")
            self._marshal(self._download_done, None, _short_reason(ex))

    def _progress_hook(self, d):
        """Worker thread: cancel-check + marshal progress. Never touches a widget."""
        if self._cancel_flag is not None and self._cancel_flag.is_set():
            raise _Cancelled()
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            pct = (done / total * 100) if total else None
            self._marshal(self._set_progress, pct, d.get("speed"), d.get("eta"))
        elif status == "finished":
            # Download of a stream finished; merge/remux (if any) runs next.
            self._marshal(self._set_progress, 100, None, None)

    def _set_progress(self, pct, speed, eta):
        """Main thread: update the bar + the %/speed/ETA label."""
        if pct is None:
            if not self._pb_pulsing:
                self._progress.configure(mode="indeterminate")
                self._progress.start(12)
                self._pb_pulsing = True
            self._progress_label.configure(text="downloading…")
            return
        if self._pb_pulsing:
            self._progress.stop()
            self._progress.configure(mode="determinate")
            self._pb_pulsing = False
        self._progress.configure(value=pct)
        parts = [f"{pct:.0f}%"]
        if speed:
            parts.append(f"{speed / 1e6:.1f} MB/s")
        if eta:
            parts.append(f"ETA {int(eta) // 60}:{int(eta) % 60:02d}")
        self._progress_label.configure(text="  ·  ".join(parts))

    def _reset_progress(self):
        if self._pb_pulsing:
            self._progress.stop()
            self._pb_pulsing = False
        self._progress.configure(mode="determinate", value=0)

    def _download_done(self, path, err):
        """Main thread: re-enable controls, then load the file or show the error."""
        self._downloading = False
        self._download_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        self._reset_progress()
        self._progress_label.configure(text="")

        if err == "Cancelled.":
            self._status.configure(text="Cancelled.", fg=PALETTE["text_mute"])
            return
        if err:
            self._status.configure(text=f"Download failed: {err}", fg=PALETTE["red"])
            return
        if not path or not os.path.exists(path):
            self._status.configure(text="Download finished but the file wasn't found.",
                                   fg=PALETTE["red"])
            return
        self._status.configure(text="Downloaded ✓ - loading...", fg=PALETTE["text_dim"])
        self._load_clip(path)  # validates + metadata + preview + publishes raw_clip

    def _cancel(self):
        """Signal the worker to abort at the next progress tick."""
        if self._cancel_flag is not None:
            self._cancel_flag.set()
        self._status.configure(text="Cancelling…", fg=PALETTE["text_mute"])

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

    # ── Import QoL: recent files, default folder, detect new clips (5.5.5) ─────────────
    def _make_collapsible_row(self, parent, title, config_key):
        """A small collapsible list header (chevron + title) toggling a row frame,
        persisted under importer.<config_key> (missing/False = expanded). Mirrors the
        editor's inspector `_section` chevron pattern. Returns (row_frame, title_label)
        - callers rebuild the row's children and update the title's count text."""
        header = tk.Frame(parent, bg=PALETTE["bg"], cursor="hand2")
        header.pack(fill="x", pady=(10, 4), padx=26)
        chevron = tk.Label(header, text="▼", bg=PALETTE["bg"], fg=PALETTE["text_dim"],
                           font=ui_font(11), cursor="hand2")
        chevron.pack(side="left")
        title_lbl = tk.Label(header, text=title, bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                             font=ui_font(9), cursor="hand2")
        title_lbl.pack(side="left", padx=(4, 0))
        row = tk.Frame(parent, bg=PALETTE["bg"])

        state = {"open": not bool(self.app.config.get("importer", {}).get(config_key, False))}

        def set_open(open_):
            state["open"] = open_
            if open_:
                row.pack(fill="x", padx=26, after=header)
                chevron.configure(text="▼")
            else:
                row.pack_forget()
                chevron.configure(text="▶")

        def toggle(_e=None):
            set_open(not state["open"])
            cfg = self.app.config.setdefault("importer", {})
            cfg[config_key] = not state["open"]
            self.app.save_config()

        for w in (header, chevron, title_lbl):
            w.bind("<Button-1>", toggle)
        set_open(state["open"])
        return row, title_lbl

    def _effective_default_folder(self):
        """The folder the picker/scan/download actually use (5.5.5 follow-up): the
        explicitly configured one if it still exists, else the app's own
        shorts_import if that has been created, else ''. Mirrors the exporter's
        shorts_export fallback. Never auto-creates - only _create_import_folder does
        that, with the user's confirmation."""
        saved = self.app.config.get("importer", {}).get("default_folder", "")
        if saved and os.path.isdir(saved):
            return saved
        shorts_import = paths.get_path("import", create=False)
        return shorts_import if os.path.isdir(shorts_import) else ""

    def _record_recent(self, path):
        """Push a successfully loaded clip to the front of recent_files, deduped
        case-insensitively, capped at _LIST_MAX. Called only from the _load_clip
        success path so a failed load never pollutes the list."""
        abspath = os.path.abspath(path)
        cfg = self.app.config.setdefault("importer", {})
        recent = [p for p in cfg.get("recent_files", []) if p.lower() != abspath.lower()]
        recent.insert(0, abspath)
        cfg["recent_files"] = recent[:_LIST_MAX]
        self.app.save_config()
        self._refresh_recent()

    def _refresh_recent(self):
        """Rebuild the RECENT list from config (capped at _LIST_MAX); hide the
        section entirely when empty."""
        for child in self._recent_row.winfo_children():
            child.destroy()
        recent = self.app.config.get("importer", {}).get("recent_files", [])[:_LIST_MAX]
        if not recent:
            self._recent_wrap.pack_forget()
            return
        self._recent_title_lbl.configure(text=f"RECENT ({len(recent)})")
        for path in recent:
            self._build_clip_row(self._recent_row, path)
        self._recent_wrap.pack(fill="x", before=self._dnd_hint)

    def _set_default_folder_value(self, folder):
        """Persist a new default_folder (explicit pick, clear, or the shorts_import
        create flow) and reset the detect baseline - a different folder means a
        fresh 'what have I already seen' start, avoiding a stale name-collision
        against whatever the old folder had."""
        cfg = self.app.config.setdefault("importer", {})
        cfg["default_folder"] = folder
        cfg["detect_known_files"] = []
        self.app.save_config()
        self._new_clips_shown = []
        self._refresh_folder_label()
        self._refresh_create_folder_btn()

    def _choose_default_folder(self):
        current = self.app.config.get("importer", {}).get("default_folder", "")
        chosen = filedialog.askdirectory(initialdir=current or None,
                                         title="Choose a default import folder")
        if chosen:
            self._set_default_folder_value(chosen)

    def _clear_default_folder(self):
        self._set_default_folder_value("")

    def _create_import_folder(self):
        """Create 'shorts_import' and set it as the default (5.5.5 follow-up) -
        mirrors the exporter's 'Create shorts_export folder' button, including the
        confirm prompt. No folder picker."""
        target = paths.get_path("import", create=False)
        if not messagebox.askyesno(
                "Create import folder",
                f"Create this folder and set it as your default import location?\n\n{target}",
                parent=self):
            return
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as ex:
            self._status.configure(text=f"Couldn't create folder: {ex}", fg=PALETTE["red"])
            return
        self._set_default_folder_value(target)
        self._status.configure(text=f"Created and set default: {target}", fg=PALETTE["green"])

    def _refresh_folder_label(self):
        explicit = self.app.config.get("importer", {}).get("default_folder", "")
        if explicit:
            self._folder_label.configure(text=self._elide(explicit), fg=PALETTE["text_mute"])
            return
        effective = self._effective_default_folder()
        if effective:
            self._folder_label.configure(
                text=f"{self._elide(effective)} (assumed)", fg=PALETTE["text_faint"])
        else:
            self._folder_label.configure(text="no default folder set",
                                         fg=PALETTE["text_mute"])

    def _refresh_create_folder_btn(self):
        """Grey out 'Create shorts_import folder' once it already exists on disk -
        clicking Create again would be a no-op."""
        exists = os.path.isdir(paths.get_path("import", create=False))
        self._create_folder_btn.configure(state="disabled" if exists else "normal")

    def _show_detect_help(self):
        HelpDialog(
            self, title="Detect new clips",
            steps=[
                "Counts as a clip: the same video types the file picker accepts "
                "(mp4, mov, mkv, webm, avi, m4v, flv, ts).",
                "Scans only the top level of your default folder, not subfolders.",
                "Compares the files that are there against what HERMES has already "
                "seen, by name - not by date, since copying a file preserves its "
                "original modified time rather than when it arrived in the folder.",
                "Runs once automatically the first time you open this tab after "
                "starting HERMES. Use Reload to check again any time.",
                "Never deletes, moves, or uploads anything - it only lists files "
                "here for a one-click import.",
                "Uses your default folder above - either one you pick, or "
                "shorts_import once you create it.",
            ])

    def _toggle_detect(self):
        cfg = self.app.config.setdefault("importer", {})
        cfg["detect_new_clips"] = bool(self._detect_var.get())
        self.app.save_config()
        if cfg["detect_new_clips"] and not self._effective_default_folder():
            self._detect_status.configure(
                text="Pick or create a default folder above, then use Reload to scan.",
                fg=PALETTE["yellow"])
        else:
            self._detect_status.configure(text="", fg=PALETTE["text_mute"])

    def _scan_new_clips(self):
        """Synchronous scan (5.5.5; name-set comparison per the follow-up): compares
        the folder's current file NAMES against a persisted 'known' set instead of
        modification time. A file copied into the folder keeps its original mtime on
        Windows (only its creation time is new), so an mtime cutoff silently misses
        files that were authored earlier and copied in later - name-set comparison
        sidesteps that entirely. New clips found are folded into the persisted known
        set immediately (so a later scan or the next launch won't re-flag them unless
        the folder's contents actually change) and added to the session's display
        list, which stays visible for the rest of the run even across scans that find
        nothing further. No background thread - a single scandir pass is cheap."""
        cfg = self.app.config.setdefault("importer", {})
        for child in self._new_clips_row.winfo_children():
            child.destroy()
        self._new_clips_wrap.pack_forget()

        if not cfg.get("detect_new_clips"):
            self._detect_status.configure(text="Detection is off.", fg=PALETTE["text_mute"])
            return
        folder = self._effective_default_folder()
        if not folder:
            self._detect_status.configure(text="No default folder set.",
                                          fg=PALETTE["text_mute"])
            return
        if not os.path.isdir(folder):
            self._detect_status.configure(text="That folder isn't available.",
                                          fg=PALETTE["text_mute"])
            return

        current = {}   # basename -> full path
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    if os.path.splitext(entry.name)[1].lower() not in _VIDEO_EXTS:
                        continue
                    current[entry.name] = entry.path
        except OSError:
            self._detect_status.configure(text="Couldn't read that folder.",
                                          fg=PALETTE["text_mute"])
            return

        known = set(cfg.get("detect_known_files", []))
        new_names = set(current) - known
        found_new = bool(new_names)
        if found_new:
            def _ctime(name):
                try:
                    return os.path.getctime(current[name])
                except OSError:
                    return 0
            fresh = [current[n] for n in sorted(new_names, key=_ctime, reverse=True)]
            carried = [p for p in self._new_clips_shown
                      if os.path.basename(p) not in new_names and os.path.exists(p)]
            self._new_clips_shown = fresh + carried
            cfg["detect_known_files"] = sorted(current)
            self.app.save_config()
        else:
            # Nothing new this scan - keep whatever was already shown this session
            # instead of clearing it out from under the user.
            self._new_clips_shown = [p for p in self._new_clips_shown if os.path.exists(p)]

        # Cap the DISPLAY to the most recent _LIST_MAX (fresh finds sort first, so a
        # cap here keeps the newest ones) - the persisted known-files set above is
        # never capped, only this session-only display list.
        self._new_clips_shown = self._new_clips_shown[:_LIST_MAX]

        if not self._new_clips_shown:
            self._detect_status.configure(text="No new clips found.", fg=PALETTE["text_mute"])
            return

        verb = "new clip(s) found" if found_new else "clip(s) still waiting to import"
        self._detect_status.configure(text=f"{len(self._new_clips_shown)} {verb}.",
                                      fg=PALETTE["green"] if found_new else PALETTE["text_mute"])
        self._new_clips_title_lbl.configure(text=f"NEW CLIPS ({len(self._new_clips_shown)})")
        for path in self._new_clips_shown:
            self._build_clip_row(self._new_clips_row, path)
        self._new_clips_wrap.pack(fill="x", before=self._url_separator)

    def _on_map(self, _e=None):
        """First-visit trigger (5.5.5): scan once, the first time this tab becomes
        visible after launch, if detection is on and a default folder is set. Later
        visits are a no-op; the Reload button covers on-demand re-scans."""
        if self._closing or self._detect_scanned:
            return
        cfg = self.app.config.get("importer", {})
        if cfg.get("detect_new_clips") and self._effective_default_folder():
            self._scan_new_clips()
            self._detect_scanned = True

    # ── Mini preview: thumbnail + duration/frames/size for a recent/detected clip ──────
    def _clip_preview(self, path):
        """(thumbnail PhotoImage or None, one-line meta string) for `path`, decoded
        once and cached for the session - recent/detected files don't change while
        the app runs, and these lists rebuild on every load and every scan. Never
        raises: a missing or unreadable file just yields a blank preview."""
        cached = self._clip_preview_cache.get(path)
        if cached is not None:
            return cached
        frame = video_utils.extract_frame(path, 0)
        img = None
        if frame is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil = Image.fromarray(rgb)
            pil.thumbnail(_THUMB_BOX)
            img = ImageTk.PhotoImage(pil)
        try:
            size_b = os.path.getsize(path)
        except OSError:
            size_b = 0
        size_s = self._fmt_size(size_b)
        meta = video_utils.probe_video(path)
        if meta is None:
            meta_s = size_s
        else:
            _w, _h, fps, n = meta
            duration = n / fps if fps > 0 and n > 0 else None
            dur_s = self._fmt_duration(duration)
            frames_s = str(n) if n > 0 else "unknown"
            meta_s = f"{dur_s} · {frames_s}f · {size_s}"
        result = (img, meta_s)
        self._clip_preview_cache[path] = result
        return result

    def _build_clip_row(self, parent, path):
        """One RECENT/NEW-CLIPS row: a small thumbnail + filename + duration/frames/
        size, click anywhere to load. Reduces friction versus opening the folder to
        check what a file is before importing it."""
        img, meta_s = self._clip_preview(path)

        row = tk.Frame(parent, bg=PALETTE["card"], highlightbackground=PALETTE["border"],
                       highlightthickness=1, bd=0, cursor="hand2")
        row.pack(fill="x", pady=(0, 4))
        inner = tk.Frame(row, bg=PALETTE["card"])
        inner.pack(fill="x", padx=8, pady=6)

        thumb_box = tk.Frame(inner, bg=PALETTE["deep"], width=_THUMB_BOX[0],
                             height=_THUMB_BOX[1])
        thumb_box.pack_propagate(False)
        thumb_box.pack(side="left")
        thumb_lbl = tk.Label(thumb_box, bg=PALETTE["deep"])
        thumb_lbl.pack(fill="both", expand=True)
        if img is not None:
            thumb_lbl.configure(image=img)
        else:
            thumb_lbl.configure(text="no preview", fg=PALETTE["text_faint"], font=ui_font(7))

        text_col = tk.Frame(inner, bg=PALETTE["card"])
        text_col.pack(side="left", fill="x", expand=True, padx=(10, 0))
        tk.Label(text_col, text=os.path.basename(path), bg=PALETTE["card"],
                 fg=PALETTE["text"], font=ui_font(9), anchor="w").pack(fill="x")
        tk.Label(text_col, text=meta_s, bg=PALETTE["card"], fg=PALETTE["text_mute"],
                 font=mono_font(8), anchor="w").pack(fill="x")

        def _click(_e=None, p=path):
            self._load_clip(p)

        def _on_enter(_e=None):
            for w in (row, inner, thumb_box, text_col):
                w.configure(bg=PALETTE["card_hover"])
            thumb_lbl.configure(bg=PALETTE["card_hover"] if img is not None else PALETTE["deep"])
            for lbl in text_col.winfo_children():
                lbl.configure(bg=PALETTE["card_hover"])

        def _on_leave(_e=None):
            for w in (row, inner, text_col):
                w.configure(bg=PALETTE["card"])
            for lbl in text_col.winfo_children():
                lbl.configure(bg=PALETTE["card"])

        for w in (row, inner, thumb_box, thumb_lbl, text_col, *text_col.winfo_children()):
            w.bind("<Button-1>", _click)
            w.bind("<Enter>", _on_enter)
            w.bind("<Leave>", _on_leave)
        Tooltip(row, path)
        return row

    # ── Helpers ──────────────────────────────────────────────────────────────────────
    @staticmethod
    def _format_meta(path, w, h, fps, n, duration):
        fps_s = f"{fps:.1f}" if fps > 0 else "unknown"
        dur_s = ImporterModule._fmt_duration(duration)
        frames_s = str(n) if n > 0 else "unknown"
        return (
            f"file      {os.path.basename(path)}\n"
            f"size      {w}×{h}\n"
            f"fps       {fps_s}\n"
            f"duration  {dur_s}\n"
            f"frames    {frames_s}"
        )

    @staticmethod
    def _fmt_duration(duration):
        if duration is None:
            return "unknown"
        if duration >= 60:
            m, s = divmod(int(round(duration)), 60)
            return f"{m}:{s:02d}"
        return f"{duration:.1f}s"

    @staticmethod
    def _elide(path, maxlen=64):
        if len(path) <= maxlen:
            return path
        head = maxlen // 2 - 2
        tail = maxlen - head - 3
        return f"{path[:head]}...{path[-tail:]}"

    @staticmethod
    def _fmt_size(size_b):
        if size_b <= 0:
            return "size unknown"
        if size_b >= 1_000_000:
            return f"{size_b / 1_000_000:.1f} MB"
        return f"{size_b / 1000:.0f} KB"

    # ── Lifecycle ────────────────────────────────────────────────────────────────────
    def has_unsaved(self):
        """Hub close-guard hook (2.8.3)."""
        return "download in progress" if self._downloading else None

    def on_close(self):
        """Abort a running download (worker is a daemon, so it won't block exit) and
        cancel the pump job (a pending after firing post-destroy throws a Tcl error)."""
        self._closing = True
        if self._cancel_flag is not None:
            self._cancel_flag.set()
        if self._ui_pump_job is not None:
            self.after_cancel(self._ui_pump_job)
            self._ui_pump_job = None
