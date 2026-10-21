"""Downloads tab - Phase 6.1.

Always-on module: the one place every downloadable asset lives - ffmpeg (moved out of
Home's settings drawer, 5.6.2), the Whisper speech models Subtitle Studio needs, and
the optional NVIDIA GPU runtime. Reuses the shared download/verify/progress-modal
plumbing (shared.downloader, shared.ui_helpers.run_download_with_modal) so no module
reimplements the download loop. Imports ONLY shared/ and stdlib - modules never import
each other.

Handoff for Phase 6.2: the downloaded cuDNN DLLs (cuda-runtime asset) need to be on
PATH, not only registered via os.add_dll_directory, before GPU inference actually
works (cuDNN is found through PATH). This module only downloads and stages them in
paths.get_path("cuda"); wiring them into the process at startup is 6.2's job.
"""

import os
import queue
import shutil
import subprocess
import threading
import tkinter as tk
import zipfile
from tkinter import messagebox, ttk

from shared import assets, downloader, paths, registry, version, video_utils, whisper_engine
from shared.ui_helpers import (
    HelpDialog, PALETTE, ScrollableFrame, TutorialIntro, TutorialReplayButton,
    TutorialWalkthrough, display_font, run_download_with_modal, ui_font,
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _fmt_size(n):
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f} GB"
    return f"{n / 1_000_000:.0f} MB"


def _cuda_device_count():
    """0 if ctranslate2 isn't importable or reports no CUDA devices. Never raises -
    an absent or broken backend must never crash the tab."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count()
    except Exception:  # noqa: BLE001
        return 0


def _adapter_names():
    """Best-effort Windows display-adapter name list, or None if detection failed.
    Cheap: a single PowerShell CIM query, timed out and wrapped so it can never hang
    or crash tab construction."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController).Name"],
            capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
        if proc.returncode != 0:
            return None
        names = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        return names or None
    except Exception:  # noqa: BLE001
        return None


def gpu_outcome(cuda_count, adapter_names):
    """Pure 3-way branch (spec 4.5): "nvidia" shows the GPU row + recommendation,
    "other" shows the CPU-only note, "unknown" shows nothing. Never claims VRAM or a
    per-model number - the runtime decision stays try-GPU-then-fall-back-to-CPU."""
    if cuda_count and cuda_count > 0:
        return "nvidia"
    if adapter_names:
        joined = " ".join(adapter_names).lower()
        if "nvidia" in joined:
            return "unknown"   # NVIDIA card present, CUDA runtime not staged yet
        if any(v in joined for v in ("amd", "radeon", "intel")):
            return "other"
    return "unknown"


def compute_repoint(selected_id, installed_ids_ordered, removing_id):
    """Pure: given the currently selected model id, the installed ids in best-
    accuracy-first order, and which id is being removed, return the id Subtitle
    Studio should switch to ("" if none remain). A no-op (returns selected_id
    unchanged) unless the removed model IS the one currently selected."""
    if selected_id != removing_id:
        return selected_id
    remaining = [i for i in installed_ids_ordered if i != removing_id]
    return remaining[0] if remaining else ""


def _cleanup_partial_model(asset):
    shutil.rmtree(assets.model_dir(asset["id"]), ignore_errors=True)


def _load_test_model(model_dir):
    """One-shot proof the model directory is actually usable (spec 4.4): construct a
    WhisperModel and transcribe a slice of silence, forcing the lazy generator to
    run - the same trick that catches a broken/incomplete model. Off the
    UI thread. Returns (ok, error)."""
    try:
        import numpy as np
        WhisperModel = whisper_engine.whisper_model_class()
        model = WhisperModel(model_dir, device="cpu", compute_type="int8")
        silence = np.zeros(16000, dtype=np.float32)
        segments, _info = model.transcribe(silence)
        list(segments)
        return True, ""
    except Exception as exc:  # noqa: BLE001 - any load/inference failure means unusable
        return False, str(exc)


def _download_model_files(asset, cancel_event, on_progress):
    """Off the UI thread. Downloads every file for `asset` into its model dir,
    model.bin last so a cancel mid-model.bin leaves only the small files - the
    directory stays correctly "not installed" until the load test passes. Deletes
    any partial files on cancel/failure. Returns a downloader.DownloadResult."""
    dest_dir = assets.model_dir(asset["id"])
    os.makedirs(dest_dir, exist_ok=True)
    files = sorted(asset["files"], key=lambda f: f["name"] == "model.bin")
    total_bytes = sum(f["size"] for f in files)
    done_bytes = 0
    for f in files:
        if cancel_event.is_set():
            _cleanup_partial_model(asset)
            return downloader.DownloadResult(ok=False, canceled=True)
        base_done = done_bytes

        def _progress(chunk_done, _chunk_total, _base=base_done):
            on_progress(_base + chunk_done, total_bytes)

        dest = os.path.join(dest_dir, f["name"])
        result = downloader.download_file(
            f["url"], dest, expected_sha256=f.get("sha256"), expected_size=f["size"],
            on_progress=_progress, cancel_event=cancel_event)
        if not result.ok:
            _cleanup_partial_model(asset)
            return result
        done_bytes += f["size"]

    ok, err = _load_test_model(dest_dir)
    if not ok:
        _cleanup_partial_model(asset)
        return downloader.DownloadResult(ok=False, error=f"model failed to load: {err}")
    return downloader.DownloadResult(ok=True, path=dest_dir)


def _extract_dlls(wheel_path, dest_dir):
    """Pull every *.dll under a bin/ folder out of an NVIDIA runtime wheel (a wheel
    is a zip) into dest_dir. Mirrors shared.video_utils._extract_ffmpeg_exe's zip-
    member-search approach, widened to "every dll in bin/" - cuDNN alone ships many
    required files, not one. Returns (extracted_names, ok, error)."""
    try:
        with zipfile.ZipFile(wheel_path) as zf:
            members = [n for n in zf.namelist()
                      if n.replace("\\", "/").lower().endswith(".dll")
                      and "/bin/" in n.replace("\\", "/").lower()]
            if not members:
                return [], False, "no DLLs found inside the downloaded package"
            names = []
            for member in members:
                name = os.path.basename(member)
                target = os.path.join(dest_dir, name)
                part = target + ".part"
                with zf.open(member) as src, open(part, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.replace(part, target)
                names.append(name)
        return names, True, ""
    except (zipfile.BadZipFile, OSError) as exc:
        return [], False, f"extraction failed: {exc}"


def _cleanup_gpu_files(cuda_dir, names):
    for name in names:
        try:
            os.remove(os.path.join(cuda_dir, name))
        except OSError:
            pass


def _download_gpu_runtime_files(cancel_event, on_progress):
    """Off the UI thread. Downloads every GPU-runtime wheel and extracts its DLLs.
    A failure partway through removes every DLL extracted so far in this run - a
    partial runtime must never present as installed."""
    asset = assets.gpu_runtime_asset()
    files = asset["files"]
    total_bytes = sum(f["size"] for f in files)
    done_bytes = 0
    cuda_dir = paths.get_path("cuda")
    extracted = []
    for f in files:
        if cancel_event.is_set():
            _cleanup_gpu_files(cuda_dir, extracted)
            return downloader.DownloadResult(ok=False, canceled=True)
        base_done = done_bytes

        def _progress(chunk_done, _chunk_total, _base=base_done):
            on_progress(_base + chunk_done, total_bytes)

        wheel_tmp = os.path.join(cuda_dir, f"_dl_{f['name']}")
        result = downloader.download_file(
            f["url"], wheel_tmp, expected_sha256=f["sha256"], expected_size=f["size"],
            on_progress=_progress, cancel_event=cancel_event)
        if not result.ok:
            _cleanup_gpu_files(cuda_dir, extracted)
            return result
        names, ok, err = _extract_dlls(wheel_tmp, cuda_dir)
        try:
            os.remove(wheel_tmp)
        except OSError:
            pass
        if not ok:
            _cleanup_gpu_files(cuda_dir, extracted)
            return downloader.DownloadResult(ok=False, error=err)
        extracted.extend(names)
        done_bytes += f["size"]

    if not extracted:
        return downloader.DownloadResult(
            ok=False, error="no files found in the downloaded runtime")
    return downloader.DownloadResult(ok=True, path=cuda_dir)


# Button styles shared by every row's action button (5.6.2-style: primary = act now,
# danger = remove, neutral = optional/already-satisfied). Kept as one small table so
# every row reads consistently instead of each hand-rolling its own colors.
_BTN_STYLES = {
    "primary": {"bg": PALETTE["green"], "fg": PALETTE["deep"],
               "activebackground": PALETTE["green"], "activeforeground": PALETTE["deep"]},
    "danger": {"bg": PALETTE["panel"], "fg": PALETTE["red_soft"],
              "activebackground": PALETTE["card_hover"], "activeforeground": PALETTE["red_soft"]},
    "neutral": {"bg": PALETTE["panel"], "fg": PALETTE["text_dim"],
               "activebackground": PALETTE["card_hover"], "activeforeground": PALETTE["text"]},
}


class DownloadsModule(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._gpu_refs = None
        self._model_rows = {}
        self._gpu_detect_job = None
        self._gpu_detect_started = False
        self._build()
        # GPU detection imports ctranslate2 (~60 MB of DLLs) and runs a PowerShell
        # query - only worth doing once this tab is actually opened, not on every
        # app start.
        self.bind("<Map>", self._on_first_map, add="+")

    def _on_first_map(self, _event=None):
        if self._gpu_detect_started:
            return
        self._gpu_detect_started = True
        self._gpu_detect_job = self.after(150, self._start_gpu_detect)

    def on_close(self):
        """Hub shutdown hook: no detection job may fire after the window is gone."""
        if self._gpu_detect_job is not None:
            try:
                self.after_cancel(self._gpu_detect_job)
            except tk.TclError:
                pass
            self._gpu_detect_job = None

    # ── layout ───────────────────────────────────────────────────────────────────
    def _row(self, parent, name, sub, size_text, indent=False, tag=None):
        """One asset row: name (+ optional colored `tag` like RECOMMENDED, + optional
        sub-label), size, state, an "IN USE" badge slot, and a button strip the
        caller fills in. Returns a dict of the widget refs so callers can update
        text/commands later."""
        row = tk.Frame(parent, bg=PALETTE["card"], highlightbackground=PALETTE["border"],
                       highlightthickness=1)
        row.pack(fill="x", pady=4, padx=(20, 0) if indent else 0)
        inner = tk.Frame(row, bg=PALETTE["card"])
        inner.pack(fill="x", padx=10, pady=7)

        namecol = tk.Frame(inner, bg=PALETTE["card"])
        namecol.pack(side="left")
        namerow = tk.Frame(namecol, bg=PALETTE["card"])
        namerow.pack(anchor="w")
        tk.Label(namerow, text=name, bg=PALETTE["card"], fg=PALETTE["text"],
                 font=ui_font(11)).pack(side="left")
        if tag:
            tk.Label(namerow, text=tag, bg=PALETTE["yellow"], fg=PALETTE["deep"],
                     font=ui_font(8, "bold"), padx=5, pady=1).pack(side="left", padx=(8, 0))
        sub_label = None
        if sub is not None:
            sub_label = tk.Label(namecol, text=sub, bg=PALETTE["card"],
                                 fg=PALETTE["text_mute"], font=ui_font(9))
            sub_label.pack(anchor="w")

        size_label = tk.Label(inner, text=size_text, bg=PALETTE["card"],
                              fg=PALETTE["text_dim"], font=ui_font(9))
        size_label.pack(side="left", padx=(16, 0))

        state = tk.Label(inner, text="", bg=PALETTE["card"], fg=PALETTE["text_mute"],
                         font=ui_font(9))
        state.pack(side="left", padx=(16, 0))
        badge = tk.Label(inner, text="", bg=PALETTE["card"], fg=PALETTE["green"],
                         font=ui_font(8, "bold"))
        badge.pack(side="left", padx=(6, 0))

        btns = tk.Frame(inner, bg=PALETTE["card"])
        btns.pack(side="right")

        return {"row": row, "sub": sub_label, "size": size_label, "state": state,
                "badge": badge, "btns": btns}

    def _style_btn(self, btn, text, style, command):
        btn.configure(text=text, command=command, **_BTN_STYLES[style])

    def _build(self):
        header = tk.Frame(self, bg=PALETTE["bg"])
        header.pack(fill="x", padx=26, pady=(22, 0))
        tk.Label(header, text="Downloads", bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=display_font(20)).pack(side="left")
        self._used_label = tk.Label(header, text="", bg=PALETTE["bg"],
                                    fg=PALETTE["text_mute"], font=ui_font(10))
        self._used_label.pack(side="left", padx=(14, 0), pady=(5, 0))
        bar = tk.Frame(self, bg=PALETTE["green"], width=66, height=3)
        bar.pack(anchor="w", padx=26, pady=(7, 0))
        bar.pack_propagate(False)
        tk.Label(self, text="ffmpeg, speech models, and the optional GPU runtime - "
                            "download what you use, remove what you don't.",
                 bg=PALETTE["bg"], fg=PALETTE["text_mute"], font=ui_font(10)).pack(
                     anchor="w", padx=26, pady=(9, 14))

        self.scroll = ScrollableFrame(self)
        self.scroll.pack(fill="both", expand=True, padx=18)
        body = self.scroll.body

        self._ffmpeg_refs = self._row(body, "ffmpeg", "video engine",
                                      _fmt_size(video_utils.FFMPEG_DL_BYTES))
        self._ffmpeg_refs["btn"] = tk.Button(
            self._ffmpeg_refs["btns"], relief="flat", bd=0, padx=12, pady=3,
            cursor="hand2", font=ui_font(9))
        self._ffmpeg_refs["btn"].pack(side="left")
        tk.Button(self._ffmpeg_refs["btns"], text="?", command=self._ffmpeg_help,
                  bg=PALETTE["panel"], fg=PALETTE["blue"],
                  activebackground=PALETTE["card_hover"], activeforeground=PALETTE["blue"],
                  relief="flat", bd=0, padx=8, pady=3, cursor="hand2",
                  font=ui_font(9)).pack(side="left", padx=(8, 0))
        self._refresh_ffmpeg_row()

        self._build_models_group(body)

        self._gpu_row_frame = tk.Frame(body, bg=PALETTE["bg"])
        self._gpu_row_frame.pack(fill="x", pady=(10, 0))

        self.note = tk.Label(self, text="", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                             font=ui_font(9))
        self.note.pack(anchor="w", padx=27, pady=(8, 14))

        self._refresh_used_label()

        # Always-present per-tab tutorial replay button (bottom-right corner, 5.5.4b).
        TutorialReplayButton(self, self._start_walkthrough,
                             "New here? Replay the tour.")

    # ── Tutorial (5.5.4, this module's own intro/walkthrough) ──────────────────────
    def maybe_show_tutorial(self):
        """Hub hook (main.py's <<NotebookTabChanged>> dispatch): show the Downloads
        intro popup on first visit, once tutorials have been asked about and are on."""
        tcfg = self.app.config.get("tutorial", {})
        if not tcfg.get("asked") or not tcfg.get("enabled"):
            return
        if tcfg.get("seen", {}).get("downloads"):
            return
        self._offer_tutorial()

    def _offer_tutorial(self):
        tcfg = self.app.config.setdefault("tutorial", {})
        tcfg.setdefault("seen", {})["downloads"] = True
        self.app.save_config()
        TutorialIntro(
            self, title="Welcome to Downloads",
            body=("Everything HERMES can download lives here: ffmpeg (needed by "
                  "Import, Clip Editor, and Export), speech models for Subtitle "
                  "Studio, and an optional GPU speed boost."),
            on_show_me=self._start_walkthrough)

    def _tour_scroll_to(self, widget):
        if widget is not None:
            try:
                self.scroll.scroll_to(widget)
            except Exception:   # noqa: BLE001 - a bad scroll target must never crash the tour
                pass

    def _start_walkthrough(self):
        """3-step tour: ffmpeg, subtitle models, GPU mode. The GPU step's target is
        None when no NVIDIA card was detected - the tour still shows the step
        centered with no ring, same as any other conditional target."""
        steps = [
            {"target": lambda: self._ffmpeg_refs["row"],
             "on_show": lambda: self._tour_scroll_to(self._ffmpeg_refs["row"]),
             "title": "ffmpeg",
             "body": "The video engine Import, Clip Editor, and Export all need. "
                     "Download HERMES's own copy here, or it uses one already on "
                     "your system if found."},
            {"target": lambda: self._models_group["row"],
             "on_show": lambda: self._tour_scroll_to(self._models_group["row"]),
             "title": "Subtitle models",
             "body": "Subtitle Studio needs one of these speech models to "
                     "transcribe your clips. Large Turbo is recommended - nearly "
                     "as accurate as the best model, much faster. Install more "
                     "than one and switch anytime."},
            {"target": lambda: (self._gpu_refs or {}).get("row"),
             "on_show": lambda: self._tour_scroll_to(
                 (self._gpu_refs or {}).get("row")),
             "title": "GPU mode (CUDA)",
             "body": "If your PC has an NVIDIA graphics card, this makes "
                     "transcription roughly 10x faster and can catch words CPU "
                     "mode misses. Only shown when a compatible card is detected."},
        ]
        TutorialWalkthrough(self.app.root, steps).start()

    def _offer_restart_now(self, message):
        restart = getattr(self.app, "restart", None)
        if not callable(restart):
            self.note.config(text=f"{message} Restart to apply (run via main.py).")
            return
        if messagebox.askyesno(f"Restart {version.APP_NAME}",
                               f"{message} Restart now?", parent=self):
            restart()

    # ── ffmpeg row (moved from Home's settings drawer, 5.6.2 - behavior unchanged) ─
    def _refresh_ffmpeg_row(self):
        """ffmpeg either works (found on the system, HERMES's own copy, or the dev
        fallback - all green) or it doesn't (red - Import/Editor/Export need it and
        won't run). Download stays offered even when it already works (grabbing
        HERMES's own pinned copy is still a valid choice), but styled neutral/gray
        instead of a bright call-to-action, since nothing is actually broken."""
        refs = self._ffmpeg_refs
        assets_dir = self.app.paths.get_path("assets", create=False)
        if video_utils.has_downloaded_ffmpeg():
            text = "● Downloaded"
            self._style_btn(refs["btn"], "Remove", "danger", self._remove_ffmpeg)
        elif shutil.which("ffmpeg"):
            text = "● Found on your system"
            self._style_btn(refs["btn"], "Download", "neutral", self._download_ffmpeg)
        elif os.path.exists(os.path.join(assets_dir, "ffmpeg.exe")):
            text = "● Using dev fallback"
            self._style_btn(refs["btn"], "Download", "neutral", self._download_ffmpeg)
        else:
            text = "● Not installed - Import/Editor/Export need it"
            self._style_btn(refs["btn"], "Download", "primary", self._download_ffmpeg)
        working = "Not installed" not in text
        refs["state"].configure(text=text,
                                fg=PALETTE["green"] if working else PALETTE["red_soft"])
        self._refresh_used_label()

    def _download_ffmpeg(self):
        mb = video_utils.FFMPEG_DL_BYTES / 1_000_000
        if not messagebox.askyesno("Download ffmpeg",
                                   f"This is about {mb:.0f} MB. Download it now?",
                                   parent=self):
            return

        def worker(cancel_event, on_progress):
            return video_utils.download_ffmpeg(on_progress=on_progress,
                                               cancel_event=cancel_event)

        run_download_with_modal(self, "Downloading ffmpeg", "Downloading ffmpeg...",
                                worker, self._ffmpeg_download_done)

    def _ffmpeg_download_done(self, result):
        if result.ok:
            self.note.config(text="ffmpeg downloaded.")
        elif result.canceled:
            self.note.config(text="ffmpeg download canceled.")
        else:
            self.note.config(text=f"ffmpeg download failed ({result.error}).")
        self._refresh_ffmpeg_row()

    def _remove_ffmpeg(self):
        if not video_utils.has_downloaded_ffmpeg():
            return
        fallback = video_utils.find_ffmpeg_excluding_downloaded()
        affected = []
        if fallback is None:
            affected = [k for k in registry.enabled_modules(self.app.config)
                       if registry.needs_ffmpeg(k)]
        if affected:
            labels = ", ".join(registry.get_label(k) for k in affected)
            if not messagebox.askyesno(
                    "Remove ffmpeg",
                    f"Removing ffmpeg will disable: {labels} - they need it to "
                    "process video and nothing else on this system provides it.\n\n"
                    "Remove it and disable these modules?", parent=self):
                return
        else:
            if not messagebox.askyesno(
                    "Remove ffmpeg", "Remove the downloaded ffmpeg (about 110 MB)?",
                    parent=self):
                return
        try:
            os.remove(video_utils.ffmpeg_download_target())
        except OSError as ex:
            self.note.config(text=f"Could not remove ffmpeg: {ex}")
            return
        for key in affected:
            self.app.config.setdefault("enabled_modules", {})[key] = False
        if affected:
            self.app.save_config()
        self._refresh_ffmpeg_row()
        if affected:
            self._offer_restart_now("ffmpeg removed; affected modules disabled.")
        else:
            self.note.config(text="ffmpeg removed.")

    def _ffmpeg_help(self):
        HelpDialog(self, title="ffmpeg", body=(
            "ffmpeg is the video engine every video-handling module needs (Import, "
            "Editor, Export).\n\n"
            "Download fetches HERMES's own pinned copy (about 110 MB), independent "
            "of whatever else is on your system.\n\n"
            "Remove deletes that copy. If ffmpeg is still found on your system's "
            "PATH afterward, removing is harmless; otherwise the modules that need "
            "it get disabled and you're asked first."))

    # ── subtitle models group ───────────────────────────────────────────────────
    def _build_models_group(self, parent):
        group = self._row(parent, "Subtitle models", "", "")
        self._models_group = group
        manage_btn = tk.Button(
            group["btns"], text="Manage", command=self._toggle_models_group,
            relief="flat", bd=0, padx=12, pady=3, cursor="hand2", font=ui_font(9),
            bg=PALETTE["panel"], fg=PALETTE["text_dim"],
            activebackground=PALETTE["card_hover"], activeforeground=PALETTE["text"])
        manage_btn.pack(side="left")
        self._models_manage_btn = manage_btn
        tk.Button(group["btns"], text="?", command=self._models_help,
                  bg=PALETTE["panel"], fg=PALETTE["blue"],
                  activebackground=PALETTE["card_hover"], activeforeground=PALETTE["blue"],
                  relief="flat", bd=0, padx=8, pady=3, cursor="hand2",
                  font=ui_font(9)).pack(side="left", padx=(8, 0))

        self._models_content = tk.Frame(parent, bg=PALETTE["bg"])
        if self.app.config.get("downloads", {}).get("models_expanded", False):
            self._models_content.pack(fill="x", pady=(2, 8), after=group["row"])

        # Short and bold - the full recommendation + accuracy tradeoff text lives in
        # the "?" dialog (_models_help) and the tutorial, not taking up row space here.
        rec_name = assets.get_asset(assets.recommended_model_id())["name"]
        tk.Label(self._models_content,
                 text=f"Not sure which model? Get {rec_name} - see the ? for details.",
                 bg=PALETTE["bg"], fg=PALETTE["yellow"], font=ui_font(10, "bold"),
                 justify="left", wraplength=640).pack(anchor="w", padx=(20, 0), pady=(6, 8))

        for asset in assets.list_assets():
            self._build_model_row(self._models_content, asset)

        self._sync_models_toggle_label()
        self._refresh_models_summary()

    def _toggle_models_group(self):
        dcfg = self.app.config.setdefault("downloads", {})
        want = not dcfg.get("models_expanded", False)
        dcfg["models_expanded"] = want
        self.app.save_config()
        if want:
            self._models_content.pack(fill="x", pady=(2, 8),
                                      after=self._models_group["row"])
        else:
            self._models_content.pack_forget()
        self._sync_models_toggle_label()

    def _sync_models_toggle_label(self):
        expanded = self.app.config.get("downloads", {}).get("models_expanded", False)
        self._models_manage_btn.configure(text="Hide" if expanded else "Manage")

    def _refresh_models_summary(self):
        installed = assets.installed_model_ids()
        total = len(assets.list_assets())
        sub = self._models_group["sub"]
        if sub is not None:
            sub.configure(text=f"Whisper - {len(installed)} of {total} installed")
        if installed:
            self._models_group["state"].configure(text="● Ready", fg=PALETTE["green"])
        else:
            self._models_group["state"].configure(text="Not installed",
                                                   fg=PALETTE["text_mute"])
        self._refresh_used_label()

    def _models_help(self):
        HelpDialog(self, title="Subtitle models",
                  body=assets.RECOMMENDATION_TEXT + "\n\n" + assets.ACCURACY_NOTE_TEXT)

    def _build_model_row(self, parent, asset):
        tag = "RECOMMENDED" if asset["recommended"] else None
        refs = self._row(parent, asset["name"], asset["blurb"],
                         _fmt_size(assets.model_total_bytes(asset)), indent=True, tag=tag)
        refs["btn"] = tk.Button(refs["btns"], relief="flat", bd=0, padx=12, pady=3,
                                cursor="hand2", font=ui_font(9))
        refs["btn"].pack(side="left")
        tk.Button(refs["btns"], text="Details",
                  command=lambda a=asset: self._show_model_details(a),
                  bg=PALETTE["panel"], fg=PALETTE["text_dim"],
                  activebackground=PALETTE["card_hover"], activeforeground=PALETTE["text"],
                  relief="flat", bd=0, padx=10, pady=3, cursor="hand2",
                  font=ui_font(9)).pack(side="left", padx=(8, 0))
        self._model_rows[asset["id"]] = refs
        self._refresh_model_row(asset["id"])

    def _refresh_model_row(self, asset_id):
        asset = assets.get_asset(asset_id)
        refs = self._model_rows[asset_id]
        in_use = self.app.config.get("subtitles", {}).get("model") == asset_id
        if assets.is_model_installed(asset):
            refs["state"].configure(text="● Downloaded", fg=PALETTE["green"])
            refs["badge"].configure(text="IN USE" if in_use else "")
            self._style_btn(refs["btn"], "Remove", "danger",
                            lambda a=asset: self._remove_model(a))
        else:
            large = assets.model_total_bytes(asset) >= 1_000_000_000
            refs["state"].configure(
                text="⚠ Not installed - large download" if large else "Not installed",
                fg=PALETTE["yellow"] if large else PALETTE["text_mute"])
            refs["badge"].configure(text="")
            self._style_btn(refs["btn"], "Download", "primary",
                            lambda a=asset: self._download_model(a))
        self._refresh_models_summary()

    def _download_model(self, asset):
        total = assets.model_total_bytes(asset)
        if total >= 1_000_000_000:
            gb = total / 1_000_000_000
            minutes = max(1, round(total / 6_000_000 / 60))   # rough, ~50 Mbit/s
            msg = (f"This model is {gb:.1f} GB. It will be downloaded once and kept "
                   f"in your models folder until you remove it.\n\nOn a typical "
                   f"connection this takes roughly {minutes} min.")
        else:
            msg = f"This model is {total / 1_000_000:.0f} MB. Download it now?"
        if not messagebox.askyesno(f"Download {asset['name']}", msg, parent=self):
            return

        def worker(cancel_event, on_progress):
            return _download_model_files(asset, cancel_event, on_progress)

        run_download_with_modal(
            self, f"Downloading {asset['name']}", f"Downloading {asset['name']}...",
            worker, lambda result: self._model_download_done(asset, result))

    def _model_download_done(self, asset, result):
        if result.ok:
            first_ever = not self.app.config.get("subtitles", {}).get("model")
            self._on_model_installed(asset)
            if first_ever and not registry.is_enabled("subtitle_studio", self.app.config):
                # The model alone does not open the tab: Subtitle Studio is a
                # module that is off until enabled. Turn it on now.
                self.app.config.setdefault("enabled_modules", {})["subtitle_studio"] = True
                self.app.save_config()
                self._offer_restart_now(f"{asset['name']} is ready and Subtitle Studio "
                                        "is turned on. It appears after a restart.")
            elif first_ever:
                messagebox.showinfo(
                    f"{asset['name']} is ready",
                    "Subtitle Studio can use it right away.", parent=self)
            else:
                messagebox.showinfo(
                    f"{asset['name']} is ready",
                    f"{asset['name']} downloaded and ready to use. No restart needed.",
                    parent=self)
            self.note.config(text=f"{asset['name']} downloaded - ready to use.")
        elif result.canceled:
            self.note.config(text=f"{asset['name']} download canceled.")
        else:
            messagebox.showerror(
                "Download failed",
                f"Could not download {asset['name']}: {result.error}\n\n"
                "Nothing was installed.", parent=self)
            self.note.config(text=f"{asset['name']} download failed.")
        self._refresh_model_row(asset["id"])

    def _on_model_installed(self, asset):
        """Repoints config["subtitles"]["model"] only if nothing is selected yet -
        the recommended model wins if it's among the installed set, else the model
        that was just installed. A later download never silently swaps an
        already-chosen model."""
        scfg = self.app.config.setdefault("subtitles", {})
        if not scfg.get("model"):
            installed = assets.installed_model_ids()
            rec = assets.recommended_model_id()
            scfg["model"] = rec if rec in installed else asset["id"]
            self.app.save_config()

    def _remove_model(self, asset):
        scfg = self.app.config.setdefault("subtitles", {})
        selected = scfg.get("model", "")
        installed_ordered = [a["id"] for a in assets.list_assets()
                             if a["id"] in assets.installed_model_ids()]
        next_id = compute_repoint(selected, installed_ordered, asset["id"])
        is_selected = selected == asset["id"]
        size_text = _fmt_size(assets.model_total_bytes(asset))
        if is_selected and next_id:
            msg = (f"Subtitle Studio is set to use this model. Removing it will "
                   f"switch Subtitle Studio to {assets.get_asset(next_id)['name']}."
                   f"\n\nReclaims {size_text}.")
        elif is_selected:
            msg = (f"Subtitle Studio has no other model installed. Removing this "
                   f"one will leave Subtitle Studio without a model.\n\n"
                   f"Reclaims {size_text}.")
        else:
            msg = f"Reclaims {size_text}."
        if not messagebox.askyesno(f"Remove {asset['name']}", msg, parent=self):
            return
        shutil.rmtree(assets.model_dir(asset["id"]), ignore_errors=True)
        if is_selected:
            scfg["model"] = next_id
            self.app.save_config()
            if next_id:
                self.note.config(text=f"{asset['name']} removed; switched to "
                                      f"{assets.get_asset(next_id)['name']}.")
            else:
                self.note.config(
                    text=f"{asset['name']} removed; Subtitle Studio has no model.")
        else:
            self.note.config(text=f"{asset['name']} removed.")
        self._refresh_model_row(asset["id"])
        if is_selected and next_id:
            self._refresh_model_row(next_id)

    def _show_model_details(self, asset):
        d = asset["details"]
        body = (
            f"Download: {d['download']}\n"
            f"RAM while running: {d['ram']}\n"
            f"Speed (CPU): {d['speed_cpu']}\n"
            f"Speed (GPU): {d['speed_gpu']}\n"
            f"Accuracy: {d['accuracy']}\n\n"
            f"{assets.MODEL_DETAIL_NOTES['speed_cpu']}\n\n"
            f"{assets.MODEL_DETAIL_NOTES['speed_gpu']}\n\n"
            f"{assets.MODEL_DETAIL_NOTES['ram']}"
        )
        if asset["id"] in ("whisper-medium", "whisper-small"):
            body += "\n\n" + assets.MODEL_DETAIL_NOTES["accuracy_small_medium"]
        HelpDialog(self, title=asset["name"], body=body)

    # ── GPU runtime row (conditional, spec 4.5) ─────────────────────────────────
    def _start_gpu_detect(self):
        self._gpu_detect_job = None
        if not self.winfo_exists():
            return
        q = queue.Queue()

        def worker():
            q.put((_cuda_device_count(), _adapter_names()))

        threading.Thread(target=worker, daemon=True).start()
        self._gpu_detect_job = self.after(150, self._poll_gpu_detect, q)

    def _poll_gpu_detect(self, q):
        self._gpu_detect_job = None
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            cuda_count, names = q.get_nowait()
        except queue.Empty:
            self._gpu_detect_job = self.after(150, self._poll_gpu_detect, q)
            return
        self._apply_gpu_outcome(gpu_outcome(cuda_count, names))

    def _apply_gpu_outcome(self, outcome):
        for w in self._gpu_row_frame.winfo_children():
            w.destroy()
        self._gpu_refs = None
        if outcome == "nvidia":
            self._build_gpu_row(self._gpu_row_frame)
        elif outcome == "other":
            tk.Label(self._gpu_row_frame, text=assets.GPU_CPU_ONLY_NOTE_TEXT,
                     bg=PALETTE["bg"], fg=PALETTE["text_mute"], font=ui_font(9),
                     justify="left", wraplength=640).pack(anchor="w")
        # "unknown": the frame stays empty - no row, no note.

    def _build_gpu_row(self, parent):
        asset = assets.gpu_runtime_asset()
        refs = self._row(parent, asset["name"], "NVIDIA only",
                         _fmt_size(assets.gpu_total_bytes()))
        refs["btn"] = tk.Button(refs["btns"], relief="flat", bd=0, padx=12, pady=3,
                                cursor="hand2", font=ui_font(9))
        refs["btn"].pack(side="left")
        tk.Button(refs["btns"], text="?", command=self._gpu_help, bg=PALETTE["panel"],
                  fg=PALETTE["blue"], activebackground=PALETTE["card_hover"],
                  activeforeground=PALETTE["blue"], relief="flat", bd=0, padx=8, pady=3,
                  cursor="hand2", font=ui_font(9)).pack(side="left", padx=(8, 0))
        self._gpu_refs = refs
        self._refresh_gpu_row()

    def _refresh_gpu_row(self):
        if self._gpu_refs is None:
            return
        refs = self._gpu_refs
        if assets.gpu_installed():
            refs["state"].configure(text="● Downloaded", fg=PALETTE["green"])
            self._style_btn(refs["btn"], "Remove", "danger", self._remove_gpu_runtime)
        else:
            refs["state"].configure(text="Not installed", fg=PALETTE["text_mute"])
            self._style_btn(refs["btn"], "Download", "primary", self._download_gpu_runtime)
        self._refresh_used_label()

    def _download_gpu_runtime(self):
        total = assets.gpu_total_bytes()
        gb = total / 1_000_000_000
        minutes = max(1, round(total / 6_000_000 / 60))
        msg = (f"This is {gb:.1f} GB. It will be downloaded once and kept until you "
               f"remove it.\n\nOn a typical connection this takes roughly "
               f"{minutes} min.")
        if not messagebox.askyesno("Download GPU mode", msg, parent=self):
            return
        run_download_with_modal(
            self, "Downloading GPU mode", "Downloading GPU mode...",
            _download_gpu_runtime_files, self._gpu_download_done)

    def _gpu_download_done(self, result):
        if result.ok:
            self.note.config(text="GPU mode downloaded.")
        elif result.canceled:
            self.note.config(text="GPU mode download canceled.")
        else:
            messagebox.showerror(
                "Download failed",
                f"Could not download GPU mode: {result.error}\n\n"
                "Nothing was installed.", parent=self)
            self.note.config(text="GPU mode download failed.")
        self._refresh_gpu_row()

    def _remove_gpu_runtime(self):
        if not messagebox.askyesno(
                "Remove GPU mode",
                "GPU acceleration will be unavailable until you download this "
                "again. Remove it?", parent=self):
            return
        cuda_dir = paths.get_path("cuda")
        try:
            names = os.listdir(cuda_dir)
        except OSError:
            names = []
        locked = 0
        for name in names:
            if name.lower().endswith(".dll"):
                try:
                    os.remove(os.path.join(cuda_dir, name))
                except OSError:
                    locked += 1     # still loaded by this session's GPU transcription
        self._refresh_gpu_row()
        if locked:
            self.note.config(text="GPU mode is still in use. Restart HERMES, then "
                                  "click Remove again to finish.")
            messagebox.showinfo(
                "Restart to finish",
                f"{locked} GPU file(s) are still in use by this session, so they "
                "could not be removed yet. Restart HERMES, then click Remove again.",
                parent=self)
        else:
            self.note.config(text="GPU mode removed.")

    def _gpu_help(self):
        HelpDialog(self, title="GPU mode (CUDA)", body=assets.GPU_BLURB_TEXT)

    # ── total-used counter ──────────────────────────────────────────────────────
    def _refresh_used_label(self):
        total = 0
        if video_utils.has_downloaded_ffmpeg():
            total += video_utils.FFMPEG_DL_BYTES
        for a in assets.list_assets():
            if assets.is_model_installed(a):
                total += assets.model_total_bytes(a)
        if assets.gpu_installed():
            total += assets.gpu_total_bytes()
        self._used_label.configure(text=f"{_fmt_size(total)} used" if total else "")
