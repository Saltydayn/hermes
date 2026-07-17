"""Home tab - the control center.

Always loaded, first tab, never disabled. Presents every registry module as a sleek,
clickable card with a function icon and a green/red status lamp: click a card to
enable/disable it (writes config). The card grid lives in a scrollable, reflowing
container so it scales cleanly from 3 modules today to 8-20+ later.

Visuals are all drawn with Canvas vector primitives (no image files / no extra deps) to
stay lightweight. Imports ONLY from shared/ and stdlib (modules never import each other).
"""

import json
import math
import os
import queue
import shutil
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from shared import autostart, downloader, registry, update_check, version, video_utils
from shared.ui_helpers import (
    PALETTE, HelpDialog, ScrollableFrame, TutorialIntro, TutorialReplayButton,
    TutorialWalkthrough, display_font, draw_lamp, load_brand_icon, make_link_bar,
    open_url, round_rect, themed_button, ui_font,
)

CARD_W, CARD_H = 248, 98


# ── Module icons (monoline, drawn around center (cx, cy), half-size h) ────────────────
def _icon_home(c, cx, cy, h, color, w):
    eave = cy - h * 0.15
    c.create_line(cx - h, eave, cx, cy - h, cx + h, eave,
                  fill=color, width=w, capstyle="round", joinstyle="round")
    bx0, bx1, by1 = cx - h * 0.72, cx + h * 0.72, cy + h
    c.create_line(bx0, eave, bx0, by1, bx1, by1, bx1, eave,
                  fill=color, width=w, capstyle="round", joinstyle="round")
    c.create_rectangle(cx - h * 0.22, cy + h * 0.3, cx + h * 0.22, by1,
                       outline=color, width=max(1, w - 1))


def _icon_import(c, cx, cy, h, color, w):
    ty = cy + h * 0.35
    c.create_line(cx - h, ty, cx - h, cy + h, cx + h, cy + h, cx + h, ty,
                  fill=color, width=w, capstyle="round", joinstyle="round")
    c.create_line(cx, cy - h, cx, cy + h * 0.12, fill=color, width=w, capstyle="round")
    c.create_line(cx - h * 0.5, cy - h * 0.32, cx, cy + h * 0.16, cx + h * 0.5, cy - h * 0.32,
                  fill=color, width=w, capstyle="round", joinstyle="round")


def _icon_export(c, cx, cy, h, color, w):
    ty = cy + h * 0.35
    c.create_line(cx - h, ty, cx - h, cy + h, cx + h, cy + h, cx + h, ty,
                  fill=color, width=w, capstyle="round", joinstyle="round")
    c.create_line(cx, cy + h * 0.16, cx, cy - h, fill=color, width=w, capstyle="round")
    c.create_line(cx - h * 0.5, cy - h * 0.4, cx, cy - h, cx + h * 0.5, cy - h * 0.4,
                  fill=color, width=w, capstyle="round", joinstyle="round")


def _icon_editor(c, cx, cy, h, color, w):
    """Crop-frame glyph: two interlocking corner brackets (the crop tool)."""
    o = h * 0.4
    c.create_line(cx - o, cy - h, cx - o, cy + o, cx + h, cy + o,
                  fill=color, width=w, capstyle="round", joinstyle="round")
    c.create_line(cx - h, cy - o, cx + o, cy - o, cx + o, cy + h,
                  fill=color, width=w, capstyle="round", joinstyle="round")


def _icon_about(c, cx, cy, h, color, w):
    """Info glyph: a lowercase 'i' inside a circle (credits / about)."""
    c.create_oval(cx - h, cy - h, cx + h, cy + h, outline=color, width=w)
    r = max(1.2, w * 0.7)
    dy = cy - h * 0.42
    c.create_oval(cx - r, dy - r, cx + r, dy + r, fill=color, outline="")
    c.create_line(cx, cy - h * 0.1, cx, cy + h * 0.5, fill=color, width=w, capstyle="round")


def _icon_module(c, cx, cy, h, color, w):
    """Fallback icon for modules without a bespoke glyph: a futuristic hex node."""
    pts = []
    for i in range(6):
        a = math.radians(60 * i - 30)
        pts += [cx + h * math.cos(a), cy + h * math.sin(a)]
    c.create_polygon(pts, outline=color, fill="", width=w, joinstyle="round")
    c.create_oval(cx - h * 0.22, cy - h * 0.22, cx + h * 0.22, cy + h * 0.22,
                  outline=color, width=w)


# Per-module visuals. Unknown modules fall back to the hex node + green accent, so new
# registry entries render without touching this file.
_ICONS = {"home": _icon_home, "importer": _icon_import, "exporter": _icon_export,
          "editor": _icon_editor, "about": _icon_about}
_ACCENTS = {"home": PALETTE["green"], "importer": PALETTE["blue"],
            "exporter": PALETTE["orange"], "editor": PALETTE["yellow"],
            "about": PALETTE["yellow"]}
# One-line description per module (mirrors the _ICONS/_ACCENTS pattern; unknown → "").
_DESCS = {
    "home": "Enable modules & global settings",
    "importer": "Bring clips in from a file or URL",
    "exporter": "Send the finished clip out to a folder",
    "editor": "Crop & composite clips into vertical Shorts",
    "about": "Credits, supporters & links",
}


def workflow_gaps(config):
    """Human-readable warnings for enabled modules whose consumed data types have no
    enabled producer (5.5.1e). Registry metadata only; a data type nothing produces
    yet (subtitled_clip until Phase 6) never warns. One combined line per consumer;
    multiple candidate producers join with " or ". Empty list = coherent selection."""
    gaps = []
    for key in registry.enabled_modules(config):
        missing = []
        for dtype in registry.MODULE_REGISTRY[key].get("consumes", []):
            producers = registry.who_produces(dtype)
            if not producers:
                continue
            if any(registry.is_enabled(p, config) for p in producers):
                continue
            for p in producers:
                label = registry.get_label(p)
                if label not in missing:
                    missing.append(label)
        if missing:
            gaps.append(f"{registry.get_label(key)} needs {' or '.join(missing)} "
                        f"enabled to receive clips.")
    return gaps


class _DownloadProgressModal(tk.Toplevel):
    """Blocking (grab_set) progress modal for a background download - shared by the
    one-time ffmpeg fetch (9.3b) and the self-update download (9.3c). No title-bar
    close - Cancel is the only way out, so a worker thread never gets orphaned
    mid-download."""

    def __init__(self, parent, title, heading):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=PALETTE["bg"])
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", lambda: None)

        wrap = tk.Frame(self, bg=PALETTE["bg"])
        wrap.pack(fill="both", expand=True, padx=18, pady=16)
        tk.Label(wrap, text=heading, bg=PALETTE["bg"],
                 fg=PALETTE["text"], font=display_font(13)).pack(anchor="w")
        self.status = tk.Label(wrap, text="Starting...", bg=PALETTE["bg"],
                               fg=PALETTE["text_dim"], font=ui_font(9))
        self.status.pack(anchor="w", pady=(8, 6))
        self.bar = ttk.Progressbar(wrap, orient="horizontal", mode="indeterminate",
                                   length=320, maximum=100)
        self.bar.pack(fill="x")
        self.bar.start(12)
        self._pulsing = True
        self.cancel_btn = themed_button(wrap, "Cancel", None, kind="danger")
        self.cancel_btn.pack(anchor="e", pady=(14, 0))

        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")
        except tk.TclError:
            pass
        self.grab_set()

    def set_progress(self, pct, detail=""):
        """pct is 0..100, or None for an unknown-total pulse. detail overrides the
        default percent label (e.g. a "x / y MB" readout for the self-updater)."""
        if pct is None:
            if not self._pulsing:
                self.bar.configure(mode="indeterminate")
                self.bar.start(12)
                self._pulsing = True
            self.status.configure(text=detail or "Downloading...")
            return
        if self._pulsing:
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self._pulsing = False
        self.bar.configure(value=pct)
        self.status.configure(text=detail or f"{pct:.0f}%")

    def close(self):
        try:
            self.bar.stop()
        except tk.TclError:
            pass
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


class ModuleCard:
    """One module rendered as a clickable card on its own small Canvas."""

    def __init__(self, parent, app, key, meta, on_toggle):
        self.app = app
        self.key = key
        self.meta = meta
        self.on_toggle = on_toggle
        self.always_on = bool(meta.get("always_on"))
        self.accent = _ACCENTS.get(key, PALETTE["green"])
        self.icon = _ICONS.get(key, _icon_module)
        self.desc = _DESCS.get(key, "")
        self.hover = False

        self.canvas = tk.Canvas(parent, width=CARD_W, height=CARD_H,
                                bg=PALETTE["bg"], highlightthickness=0, bd=0)
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        if not self.always_on:
            self.canvas.configure(cursor="hand2")
            self.canvas.bind("<Button-1>", self._on_click)
        self.draw()

    def _enabled(self):
        return registry.is_enabled(self.key, self.app.config)

    def _on_enter(self, _e):
        self.hover = True
        self.draw()

    def _on_leave(self, _e):
        self.hover = False
        self.draw()

    def _on_click(self, _e):
        self.on_toggle(self.key)
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        on = self._enabled()
        interactive = not self.always_on

        fill = PALETTE["card_hover"] if (self.hover and interactive) else PALETTE["card"]
        outline = self.accent if (self.hover and interactive) else PALETTE["border"]
        round_rect(c, 2, 2, CARD_W - 2, CARD_H - 2, 16, fill=fill, outline=outline, width=1.5)

        # Left accent rail signals "enabled" at a glance.
        if on:
            round_rect(c, 6, 16, 10, CARD_H - 16, 2, fill=self.accent, outline="")

        icon_color = self.accent if on else PALETTE["text_faint"]
        self.icon(c, 42, CARD_H / 2, 15, icon_color, 2)

        name_color = PALETTE["text"] if on else PALETTE["text_dim"]
        c.create_text(72, 24, text=self.meta["label"], anchor="w",
                      fill=name_color, font=display_font(14))

        if self.desc:
            # Wrap well before the status lamp (its left edge ≈ CARD_W-31) so the text
            # never runs underneath it - break to the next line instead.
            c.create_text(72, 38, text=self.desc, anchor="nw", width=CARD_W - 116,
                          fill=PALETTE["text_mute"] if on else PALETTE["text_faint"],
                          font=ui_font(8))

        if self.always_on:
            status, scolor = "ALWAYS ON", PALETTE["text_mute"]
        elif on:
            status, scolor = "ENABLED", PALETTE["green"]
        else:
            status, scolor = "DISABLED", PALETTE["text_mute"]
        c.create_text(72, 76, text=status, anchor="w", fill=scolor, font=ui_font(8))

        draw_lamp(c, CARD_W - 24, CARD_H / 2, on)


class _SettingsDrawer(tk.Frame):
    """Collapsible settings panel, place()-pinned to the bottom-left corner of its
    parent (mirrors TutorialReplayButton's bottom-right pin in shared/ui_helpers.py).
    A place()-pinned widget is positioned relative to its parent's own bounds, never
    the pack cavity, so it can never be squeezed off-screen by the module-card grid
    or any other content, at any window size (5.6.1) - unlike the settings rows this
    replaces, which used to pack() below the expanding card grid and could be pushed
    past the bottom of a short window with no way to scroll to them.

    Content widgets stay alive and attribute-referenceable at all times regardless of
    collapsed state (toggling only pack()/pack_forget()s the content frame, never
    destroys it) - the tutorial walkthrough resolves settings widgets by reference
    even while the drawer is visually collapsed."""

    def __init__(self, parent, expanded, on_toggle):
        super().__init__(parent, bg=PALETTE["bg"])
        self._on_toggle = on_toggle
        self.expanded = expanded

        self.header = tk.Button(
            self, text="", command=self._toggle,
            bg=PALETTE["bg"], fg=PALETTE["text_mute"],
            activebackground=PALETTE["bg"], activeforeground=PALETTE["text"],
            relief="flat", bd=0, cursor="hand2", anchor="w",
            font=ui_font(9), padx=0, pady=4,
        )
        self.header.pack(anchor="w")

        self.content = tk.Frame(self, bg=PALETTE["bg"])
        if expanded:
            self.content.pack(anchor="w", pady=(4, 0))
        self._refresh_header()

        self.place(relx=0.0, rely=1.0, anchor="sw", x=16, y=-16)
        self.lift()

    def _refresh_header(self):
        arrow = "▾" if self.expanded else "▸"
        self.header.configure(text=f"⚙ Settings {arrow}")

    def _toggle(self):
        self.expand(not self.expanded)

    def expand(self, want=True):
        if want == self.expanded:
            return
        self.expanded = want
        if want:
            self.content.pack(anchor="w", pady=(4, 0))
        else:
            self.content.pack_forget()
        self._refresh_header()
        self._on_toggle(self.expanded)


class HomeModule(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.cards = []
        self._update_running = False
        self._update_notice = None
        self._build()

    def _build(self):
        # Header: title + accent underline + tagline, with a Restart control top-right.
        header = tk.Frame(self, bg=PALETTE["bg"])
        header.pack(fill="x", padx=26, pady=(22, 0))
        assets_dir = self.app.paths.get_path("assets", create=False)
        themed_button(header, "↻ Restart", self._restart, kind="neutral").pack(
            side="right", anchor="ne")
        # Logo + dotted brand name on one row.
        titlerow = tk.Frame(header, bg=PALETTE["bg"])
        titlerow.pack(anchor="w")
        logo = load_brand_icon(assets_dir, "hermes", size=34)
        if logo is not None:
            ll = tk.Label(titlerow, image=logo, bg=PALETTE["bg"])
            ll.image = logo   # keep a ref so it isn't GC'd
            ll.pack(side="left", padx=(0, 12))
        tk.Label(titlerow, text=version.DISPLAY_NAME, bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=display_font(20)).pack(side="left")
        if version.NAME_TAG:
            tk.Label(titlerow, text=version.NAME_TAG, bg=PALETTE["bg"],
                     fg=PALETTE["text_mute"], font=ui_font(9)).pack(
                         side="left", padx=(8, 0), pady=(6, 0))
        bar = tk.Frame(header, bg=PALETTE["green"], width=66, height=3)
        bar.pack(anchor="w", pady=(7, 0))
        bar.pack_propagate(False)
        # Tagline doubles as the About line: the brand expansion + version (NAMING.md).
        tk.Label(header, text=f"{version.EXPANSION}  ·  v{version.display_version()}",
                 bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                 font=ui_font(10)).pack(anchor="w", pady=(9, 0))

        # Creator links + support (shared widget; the About tab reuses the same one).
        self._link_bar = make_link_bar(header, self.app.config.get("links", {}), assets_dir)
        self._link_bar.pack(anchor="w", pady=(12, 2))

        # Update notice slot (5.3): the "newer version available" banner packs in here,
        # between the header and the module grid. Effectively empty until a check hits.
        self._notice_slot = tk.Frame(self, bg=PALETTE["bg"])
        self._notice_slot.pack(fill="x", padx=26)

        # First-visit hint (5.5.1e): what the module cards are, dismissed by the x
        # only (never auto-hidden - a user mid-confusion may toggle before reading).
        self._hint = None
        if not (self.app.config.get("home") or {}).get("hint_dismissed"):
            self._hint = tk.Frame(self, bg=PALETTE["card"],
                                  highlightbackground=PALETTE["border"],
                                  highlightthickness=1)
            self._hint.pack(fill="x", padx=26, pady=(14, 0))
            inner = tk.Frame(self._hint, bg=PALETTE["card"])
            inner.pack(fill="x", padx=12, pady=8)
            tk.Button(inner, text="×", command=self._dismiss_hint, bg=PALETTE["card"],
                      fg=PALETTE["text_mute"], activebackground=PALETTE["card_hover"],
                      activeforeground=PALETTE["text"], relief="flat", bd=0, padx=6,
                      cursor="hand2", font=ui_font(10)).pack(side="right", anchor="n")
            tk.Label(inner,
                     text="Modules are the tabs of this app. Click a card to turn one "
                          "on or off.\nKeeping unused modules off just makes startup a "
                          "little faster. Enabling everything is completely fine, and "
                          "some modules feed each other.",
                     bg=PALETTE["card"], fg=PALETTE["text_dim"], font=ui_font(9),
                     justify="left", anchor="w", wraplength=680).pack(
                         side="left", fill="x", expand=True)

        tk.Label(self, text="MODULES", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(anchor="w", padx=27, pady=(18, 6))

        # Scrollable, reflowing card grid.
        self.scroll = ScrollableFrame(self)
        self.scroll.pack(fill="both", expand=True, padx=18)
        for key, meta in registry.MODULE_REGISTRY.items():
            self.cards.append(ModuleCard(self.scroll.body, self.app, key, meta, self._toggle))

        # Workflow-gap warning (5.5.1e): red, advisory only, packed under the card
        # grid whenever the enabled set leaves a module without its inputs.
        self._gap_warn = tk.Label(self, text="", bg=PALETTE["bg"],
                                  fg=PALETTE["red_soft"], font=ui_font(9),
                                  anchor="w", justify="left", wraplength=680)
        self._refresh_gap_warning()

        self.note = tk.Label(self, text="", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                             font=ui_font(9))
        self.note.pack(anchor="w", padx=27, pady=(10, 14))

        # ── Settings drawer (5.6.1): place()-pinned bottom-left, collapsible, so the
        # settings below can never be squeezed off-screen by the card grid regardless
        # of window size. Expanded by default - the point of this task is
        # discoverability. All rows below are parented to `content`, not `self`.
        home_cfg = self.app.config.setdefault("home", {})
        self._settings_drawer = _SettingsDrawer(
            self, home_cfg.get("settings_expanded", True), self._on_settings_toggle)
        content = self._settings_drawer.content

        # The global Eco/Performance mode (2.8.4).
        srow = tk.Frame(content, bg=PALETTE["bg"])
        srow.pack(fill="x", padx=27)
        self._perf_row = srow
        tk.Label(srow, text="Render & background work", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(10)).pack(side="left")
        self._mode_btns = {}
        for mode in ("eco", "performance"):
            btn = tk.Button(srow, text=mode.capitalize(),
                            command=lambda m=mode: self._set_mode(m),
                            relief="flat", bd=0, padx=12, pady=3, cursor="hand2",
                            font=ui_font(9))
            btn.pack(side="left", padx=(8, 0))
            self._mode_btns[mode] = btn
        tk.Button(srow, text="?", command=self._mode_help, bg=PALETTE["panel"],
                  fg=PALETTE["blue"], activebackground=PALETTE["card_hover"],
                  activeforeground=PALETTE["blue"], relief="flat", bd=0, padx=8,
                  pady=3, cursor="hand2", font=ui_font(9)).pack(side="left",
                                                                padx=(8, 0))
        self._refresh_mode_btns()

        # Hardware encode setting (2.8.5): auto / on / off.
        self._hw_row = hrow = tk.Frame(content, bg=PALETTE["bg"])
        hrow.pack(fill="x", padx=27, pady=(6, 0))
        tk.Label(hrow, text="Hardware encode", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(10)).pack(side="left")
        self._hw_var = tk.StringVar(value=self.app.config.get("hw_encode", "auto"))
        hw_box = ttk.Combobox(hrow, textvariable=self._hw_var, state="readonly",
                              values=("auto", "on", "off"), width=6)
        hw_box.pack(side="left", padx=(8, 0))
        hw_box.bind("<<ComboboxSelected>>", self._on_hw_change)
        tk.Button(hrow, text="?", command=self._hw_help, bg=PALETTE["panel"],
                  fg=PALETTE["blue"], activebackground=PALETTE["card_hover"],
                  activeforeground=PALETTE["blue"], relief="flat", bd=0, padx=8,
                  pady=3, cursor="hand2", font=ui_font(9)).pack(side="left",
                                                                padx=(8, 0))

        # ffmpeg / video engine status (5.6.2): download HERMES's own pinned copy or
        # remove it. Sits next to hardware encode - both are about the video pipeline.
        self._ffmpeg_row = frow = tk.Frame(content, bg=PALETTE["bg"])
        frow.pack(fill="x", padx=27, pady=(6, 0))
        tk.Label(frow, text="ffmpeg (video engine)", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(10)).pack(side="left")
        self._ffmpeg_status = tk.Label(frow, text="", bg=PALETTE["bg"],
                                       fg=PALETTE["text_mute"], font=ui_font(9))
        self._ffmpeg_status.pack(side="left", padx=(8, 0))
        self._ffmpeg_btn = tk.Button(frow, relief="flat", bd=0, padx=12, pady=3,
                                     cursor="hand2", font=ui_font(9),
                                     bg=PALETTE["card"], fg=PALETTE["text_dim"],
                                     activebackground=PALETTE["card_hover"],
                                     activeforeground=PALETTE["text"])
        self._ffmpeg_btn.pack(side="left", padx=(8, 0))
        tk.Button(frow, text="?", command=self._ffmpeg_help, bg=PALETTE["panel"],
                  fg=PALETTE["blue"], activebackground=PALETTE["card_hover"],
                  activeforeground=PALETTE["blue"], relief="flat", bd=0, padx=8,
                  pady=3, cursor="hand2", font=ui_font(9)).pack(side="left",
                                                                padx=(8, 0))
        self._refresh_ffmpeg_row()

        # Launch-on-boot toggle (dist.4): mirrors the installer's checkbox via HKCU Run.
        # Reflects the actual REGISTRY state, not just config - shows On if the installer
        # set it. Source of truth for display is the registry; config records the intent.
        brow = tk.Frame(content, bg=PALETTE["bg"])
        brow.pack(fill="x", padx=27, pady=(6, 0))
        tk.Label(brow, text="Launch on startup", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(10)).pack(side="left")
        self._boot_on = autostart.is_launch_on_boot()
        self._boot_btn = tk.Button(brow, command=self._toggle_boot, relief="flat", bd=0,
                                   padx=12, pady=3, cursor="hand2", font=ui_font(9))
        self._boot_btn.pack(side="left", padx=(8, 0))
        tk.Button(brow, text="?", command=self._boot_help, bg=PALETTE["panel"],
                  fg=PALETTE["blue"], activebackground=PALETTE["card_hover"],
                  activeforeground=PALETTE["blue"], relief="flat", bd=0, padx=8,
                  pady=3, cursor="hand2", font=ui_font(9)).pack(side="left",
                                                                padx=(8, 0))
        # Keep config honest with the registry truth (harmless if unchanged).
        if self.app.config.get("launch_on_boot") != self._boot_on:
            self.app.config["launch_on_boot"] = self._boot_on
            self.app.save_config()
        self._refresh_boot_btn()

        # Update check (5.3): manual Check now + the quiet on-launch toggle.
        self._update_row = urow = tk.Frame(content, bg=PALETTE["bg"])
        urow.pack(fill="x", padx=27, pady=(6, 0))
        tk.Label(urow, text="Updates", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(10)).pack(side="left")
        self._upd_check_btn = tk.Button(
            urow, text="Check now", command=lambda: self._start_update_check("manual"),
            bg=PALETTE["card"], fg=PALETTE["text_dim"],
            activebackground=PALETTE["card_hover"], activeforeground=PALETTE["text"],
            relief="flat", bd=0, padx=12, pady=3, cursor="hand2", font=ui_font(9))
        self._upd_check_btn.pack(side="left", padx=(8, 0))
        self._upd_launch_btn = tk.Button(urow, command=self._toggle_update_launch,
                                         relief="flat", bd=0, padx=12, pady=3,
                                         cursor="hand2", font=ui_font(9))
        self._upd_launch_btn.pack(side="left", padx=(8, 0))
        tk.Button(urow, text="?", command=self._update_help, bg=PALETTE["panel"],
                  fg=PALETTE["blue"], activebackground=PALETTE["card_hover"],
                  activeforeground=PALETTE["blue"], relief="flat", bd=0, padx=8,
                  pady=3, cursor="hand2", font=ui_font(9)).pack(side="left",
                                                                padx=(8, 0))
        self._upd_status = tk.Label(urow, text="", bg=PALETTE["bg"],
                                    fg=PALETTE["text_mute"], font=ui_font(9))
        self._upd_status.pack(side="left", padx=(10, 0))
        self._refresh_update_btn()

        # Keyboard layout (5.5.2b correction): the editor's undo key is Y on QWERTZ
        # and Z on QWERTY (same physical key). Combobox here + a one-shot first-start
        # prompt below.
        kbrow = tk.Frame(content, bg=PALETTE["bg"])
        kbrow.pack(fill="x", padx=27, pady=(6, 0))
        tk.Label(kbrow, text="Keyboard layout", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(10)).pack(side="left")
        self._kb_var = tk.StringVar(
            value=self.app.config.get("keyboard_layout", "qwertz"))
        self._kb_box = ttk.Combobox(kbrow, textvariable=self._kb_var, state="readonly",
                                    values=("qwertz", "qwerty"), width=8)
        self._kb_box.pack(side="left", padx=(8, 0))
        self._kb_box.bind("<<ComboboxSelected>>", self._on_kb_change)
        tk.Button(kbrow, text="?", command=self._kb_help, bg=PALETTE["panel"],
                  fg=PALETTE["blue"], activebackground=PALETTE["card_hover"],
                  activeforeground=PALETTE["blue"], relief="flat", bd=0, padx=8,
                  pady=3, cursor="hand2", font=ui_font(9)).pack(side="left",
                                                                padx=(8, 0))

        # Tutorials (5.5.4a): re-enable toggle. Resets per-module "seen" so every tab's
        # intro replays; the per-tab green "?" (TutorialReplayButton) is separate and
        # always present regardless of this setting.
        trow = tk.Frame(content, bg=PALETTE["bg"])
        trow.pack(fill="x", padx=27, pady=(6, 0))
        tk.Label(trow, text="Tutorials", bg=PALETTE["bg"],
                 fg=PALETTE["text_dim"], font=ui_font(10)).pack(side="left")
        self._tut_btn = tk.Button(trow, command=self._toggle_tutorials, relief="flat",
                                  bd=0, padx=12, pady=3, cursor="hand2", font=ui_font(9))
        self._tut_btn.pack(side="left", padx=(8, 0))
        tk.Button(trow, text="?", command=self._tutorial_help, bg=PALETTE["panel"],
                  fg=PALETTE["blue"], activebackground=PALETTE["card_hover"],
                  activeforeground=PALETTE["blue"], relief="flat", bd=0, padx=8,
                  pady=3, cursor="hand2", font=ui_font(9)).pack(side="left",
                                                                padx=(8, 0))
        self._refresh_tutorial_btn()

        # Always-present per-tab tutorial replay button (bottom-right corner).
        TutorialReplayButton(self, self._start_walkthrough,
                             "Need a refresher? Replay the tour.")

        # Reflow columns on resize. add="+" so we don't clobber ScrollableFrame's binding.
        self.scroll.canvas.bind("<Configure>", self._reflow, add="+")
        self.after(60, self._reflow)

        # Quiet startup check (5.3): delayed so launch stays snappy and off the network
        # during boot. Startup checks surface nothing except the "newer" notice.
        if (self.app.config.get("update_check_on_launch", True)
                and update_check.effective_url(self.app.config)):
            self.after(2500, self._start_update_check, "startup")

        # One-shot first-launch prompts (keyboard layout, tutorials) followed by the
        # ffmpeg startup check (9.3b) - all chained through ONE after() into ONE callback
        # so their blocking modals never stack; each askyesno blocks until answered
        # before the next dialog can open.
        self.after(1200, self._run_startup_prompts_then_ffmpeg_check)

    def _reflow(self, event=None):
        width = event.width if event is not None else self.scroll.canvas.winfo_width()
        if width <= 1:
            return
        gap = 14
        cols = max(1, (width - gap) // (CARD_W + gap))
        for i, card in enumerate(self.cards):
            row, col = divmod(i, cols)
            card.canvas.grid(row=row, column=col, padx=gap // 2, pady=gap // 2, sticky="nw")

    def _toggle(self, key):
        """Flip a module's enabled state, persist it, and give feedback. Module load/unload
        happens on restart (the hub lazy-imports at startup). Enabling a module that needs
        ffmpeg when none is found (9.3b) routes through the download flow instead of
        flipping immediately - the card stays in its current state until that resolves."""
        turning_on = not registry.is_enabled(key, self.app.config)
        if turning_on and registry.needs_ffmpeg(key) and video_utils.find_ffmpeg() is None:
            self._ensure_ffmpeg(lambda: self._enable_after_ffmpeg(key))
            return
        self._set_module_enabled(key, turning_on)

    def _enable_after_ffmpeg(self, key):
        """on_ready callback once an enable-triggered ffmpeg download succeeds: flip the
        module on, then offer the restart it always needed anyway."""
        self._set_module_enabled(key, True)
        self._offer_restart_now(f"{registry.get_label(key)} is ready.")

    def _set_module_enabled(self, key, new_state):
        self.app.config.setdefault("enabled_modules", {})[key] = new_state
        self.app.save_config()
        verb = "enabled" if new_state else "disabled"
        self.note.config(text=f"{registry.get_label(key)} {verb} - Restart to apply.")
        self._refresh_gap_warning()
        self._redraw_card(key)

    def _redraw_card(self, key):
        for card in self.cards:
            if card.key == key:
                card.draw()
                return

    def _offer_restart_now(self, message):
        restart = getattr(self.app, "restart", None)
        if not callable(restart):
            self.note.config(text=f"{message} Restart to apply (run via main.py).")
            return
        if messagebox.askyesno(f"Restart {version.APP_NAME}",
                               f"{message} Restart now?", parent=self):
            restart()

    # ── On-demand ffmpeg download (9.3b) ────────────────────────────────────────────
    def _ensure_ffmpeg(self, on_ready):
        """If ffmpeg is already available, run on_ready() immediately. Else prompt with
        the download size; on accept, run a blocking modal download; on success call
        on_ready(). Declining, cancelling, or a failed download never calls on_ready -
        the caller's pending action (enable a module, offer a restart) simply doesn't
        happen and can be retried later."""
        if video_utils.find_ffmpeg() is not None:
            on_ready()
            return
        mb = video_utils.FFMPEG_DL_BYTES / 1_000_000
        if not messagebox.askyesno(
                "Download ffmpeg",
                "This module needs ffmpeg to process video. Download it now? "
                f"(about {mb:.0f} MB)", parent=self):
            self.note.config(text="ffmpeg download declined - module not enabled.")
            return
        self._start_ffmpeg_download(on_ready)

    def _start_ffmpeg_download(self, on_ready):
        """Spawn the download worker plus a blocking progress modal. The worker touches
        no tkinter; progress and the final result are marshalled through a queue and
        drained by a UI-thread after() poll (house pattern, mirrors _poll_update)."""
        modal = _DownloadProgressModal(self, "Downloading ffmpeg", "Downloading ffmpeg...")
        cancel_event = threading.Event()
        modal.cancel_btn.configure(command=cancel_event.set)
        q = queue.Queue()

        def on_progress(done, total):
            q.put(("progress", (done / total * 100) if total else None))

        def worker():
            result = video_utils.download_ffmpeg(on_progress=on_progress,
                                                 cancel_event=cancel_event)
            q.put(("done", result))

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, self._poll_ffmpeg_download, q, modal, on_ready)

    def _poll_ffmpeg_download(self, q, modal, on_ready):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "progress":
                    modal.set_progress(payload)
                else:
                    modal.close()
                    self._ffmpeg_download_done(payload, on_ready)
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll_ffmpeg_download, q, modal, on_ready)

    def _ffmpeg_download_done(self, result, on_ready):
        if result.ok:
            self.note.config(text="ffmpeg downloaded.")
            on_ready()
        elif result.canceled:
            self.note.config(text="ffmpeg download cancelled - module not enabled.")
        else:
            self.note.config(
                text=f"ffmpeg download failed ({result.error}) - try again from Home later.")

    def _refresh_ffmpeg_row(self):
        """Recompute and redraw the ffmpeg status text + action button (5.6.2). Called
        after _build, after any ffmpeg download completes, and after a removal."""
        assets_dir = self.app.paths.get_path("assets", create=False)
        if video_utils.has_downloaded_ffmpeg():
            status, btn_text = "Downloaded (~110 MB)", "Remove"
            command = self._remove_ffmpeg
        elif shutil.which("ffmpeg"):
            status, btn_text = "Found on your system", "Download"
            command = lambda: self._ensure_ffmpeg(self._refresh_ffmpeg_row)  # noqa: E731
        elif os.path.exists(os.path.join(assets_dir, "ffmpeg.exe")):
            status, btn_text = "Using dev fallback", "Download"
            command = lambda: self._ensure_ffmpeg(self._refresh_ffmpeg_row)  # noqa: E731
        else:
            status, btn_text = "Not installed", "Download"
            command = lambda: self._ensure_ffmpeg(self._refresh_ffmpeg_row)  # noqa: E731
        self._ffmpeg_status.configure(text=status)
        self._ffmpeg_btn.configure(text=btn_text, command=command)

    def _remove_ffmpeg(self):
        """Delete HERMES's own downloaded ffmpeg copy. If nothing else on this system
        would still resolve (PATH, dev fallback), warn and auto-disable whichever
        enabled modules need it rather than blocking the removal outright."""
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
                    "Remove ffmpeg",
                    "Remove the downloaded ffmpeg (about 110 MB)?", parent=self):
                return
        try:
            os.remove(video_utils.ffmpeg_download_target())
        except OSError as ex:
            self.note.config(text=f"Could not remove ffmpeg: {ex}")
            return
        for key in affected:
            self._set_module_enabled(key, False)
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

    def _check_ffmpeg_startup(self):
        """Startup-only ffmpeg presence check: catches an update from an older build
        that bundled ffmpeg, where an already-enabled module now has none. Chained
        after the first-launch prompts (not its own after() delay) so blocking modals
        never stack."""
        if not self.winfo_exists():
            return
        if video_utils.find_ffmpeg() is not None:
            return
        needing = [k for k in registry.enabled_modules(self.app.config)
                  if registry.needs_ffmpeg(k)]
        if not needing:
            return
        self._ensure_ffmpeg(lambda: self._offer_restart_now("ffmpeg is ready."))

    def _on_kb_change(self, _e=None):
        """Persist the keyboard layout; the editor reads it fresh per keypress, so
        the undo key applies right away (button hints update on restart)."""
        self.app.config["keyboard_layout"] = self._kb_var.get()
        self.app.save_config()
        undo = "Z" if self._kb_var.get() == "qwerty" else "Y"
        self.note.config(text=f"Undo is now {undo} (redo stays X) - applies right "
                              f"away; button hints update on restart.")

    def _kb_help(self):
        HelpDialog(self, title="Keyboard layout", body=(
            "The editor's undo key sits next to X (redo): Y on a QWERTZ keyboard "
            "(German layout), Z on QWERTY.\n\nPick your physical keyboard here and "
            "the undo key follows. Every other shortcut is the same on both "
            "layouts."))

    def _ask_keyboard_layout(self):
        """One-shot first-start prompt (5.5.2b correction). Sets the layout AND the
        asked flag together so the question never repeats."""
        if self.app.config.get("keyboard_layout_asked") or not self.winfo_exists():
            return
        qwertz = messagebox.askyesno(
            "Keyboard layout",
            "HERMES uses the Y key for undo and X for redo, designed for QWERTZ "
            "keyboards (German layout).\n\nAre you using a QWERTZ keyboard?\n\n"
            "Choose No if you use QWERTY - undo then moves to Z, the same key "
            "position.",
            parent=self)
        self.app.config["keyboard_layout"] = "qwertz" if qwertz else "qwerty"
        self.app.config["keyboard_layout_asked"] = True
        self.app.save_config()
        self._kb_var.set(self.app.config["keyboard_layout"])

    def _run_startup_prompts_then_ffmpeg_check(self):
        """The single scheduled entry point for every one-shot/startup dialog: the
        first-launch prompts (each askyesno blocks until answered), then the ffmpeg
        startup check (9.3b) - which only itself prompts if ffmpeg actually turns out
        to be missing. Keeping this as one callback is what stops any of them from
        showing as stacked dialogs."""
        self._run_first_launch_prompts()
        self._check_ffmpeg_startup()

    def _run_first_launch_prompts(self):
        """Fires both one-shot first-launch prompts in sequence (see the scheduling
        comment above) so they never show as two stacked dialogs."""
        if not self.app.config.get("keyboard_layout_asked"):
            self._ask_keyboard_layout()
        self._ask_tutorial_prompt()

    def _ask_tutorial_prompt(self):
        """One-shot: does the user want the guided tutorials? Sets "enabled" from the
        answer and "asked" together so the question never repeats."""
        tcfg = self.app.config.setdefault("tutorial", {})
        if tcfg.get("asked") or not self.winfo_exists():
            return
        want = messagebox.askyesno(
            "Tutorials",
            "Want a short guided tour the first time you visit each tab?\n\n"
            "You can turn this on or off anytime from Home, and replay any tab's "
            "tour later with its '?' button.",
            parent=self)
        tcfg["enabled"] = want
        tcfg["asked"] = True
        self.app.save_config()
        self._refresh_tutorial_btn()
        # Home is the tab already showing at launch, so no <<NotebookTabChanged>>
        # event ever fires for it - the hub's generic hook only runs on an actual
        # switch. Without this, Home's own intro would sit dormant until the user
        # happened to leave and come back, which read as "it fires again when I
        # click back to Home" (5.5.4b beta fix; it was really firing for the first
        # time, just badly timed).
        self.maybe_show_tutorial()

    def maybe_show_tutorial(self):
        """Hub hook (main.py's generic <<NotebookTabChanged>> dispatch): show Home's
        intro popup on first visit, once tutorials have been asked about and are on."""
        tcfg = self.app.config.get("tutorial", {})
        if not tcfg.get("asked") or not tcfg.get("enabled"):
            return
        if tcfg.get("seen", {}).get("home"):
            return
        self._offer_tutorial()

    def _offer_tutorial(self):
        """Show Home's intro popup. Marks "seen" the moment it's shown - skipping and
        touring both count, so the intro never repeats either way."""
        tcfg = self.app.config.setdefault("tutorial", {})
        tcfg.setdefault("seen", {})["home"] = True
        self.app.save_config()
        TutorialIntro(
            self, title="Welcome to Home",
            body=("Home is the control center. Click a module card to enable or "
                  "disable it - enabled modules appear as their own tab. Everything "
                  "below the cards is a global setting."),
            on_show_me=self._start_walkthrough)

    def _start_walkthrough(self):
        """Home's 8-step tour: module cards, keyboard layout, performance mode,
        launch on startup, then a closing stretch on the settings the earlier steps
        skip over - links, hardware encode, ffmpeg (5.6.2), updates (5.5.4b beta
        addition). Steps targeting a widget inside the settings drawer expand it
        first via on_show, even starting from a collapsed state. Also the entry
        point for the replay button."""
        steps = [
            {"target": lambda: self.scroll, "title": "Module cards",
             "body": "Click a card to enable or disable that module. Enabled "
                     "modules show up as their own tab after a restart."},
            {"target": lambda: getattr(self, "_kb_box", None),
             "title": "Keyboard layout",
             "body": "Match this to your physical keyboard so the editor's "
                     "undo/redo keys land in the right place.",
             "on_show": self._expand_settings_drawer},
            {"target": lambda: getattr(self, "_perf_row", None),
             "title": "Performance mode",
             "body": "Eco keeps rendering light so gaming or streaming alongside "
                     "stays smooth. Performance renders faster when the PC is "
                     "all yours.",
             "on_show": self._expand_settings_drawer},
            {"target": lambda: getattr(self, "_boot_btn", None),
             "title": "Launch on startup",
             "body": "Turn this on to have HERMES open automatically when you "
                     "log in.",
             "on_show": self._expand_settings_drawer},
            {"target": lambda: getattr(self, "_link_bar", None),
             "title": "Saltydayn's socials",
             "body": "YouTube, Twitch, Discord, and X - come hang out, get in "
                     "touch, or support HERMES. Same links on the About tab."},
            {"target": lambda: getattr(self, "_hw_row", None),
             "title": "Hardware encode",
             "body": "Auto uses your GPU's hardware encoder in Performance mode "
                     "for faster renders, falling back to software if none is "
                     "found. Force it On or Off if you'd rather choose yourself.",
             "on_show": self._expand_settings_drawer},
            {"target": lambda: getattr(self, "_ffmpeg_row", None),
             "title": "ffmpeg",
             "body": "The video engine every module needs. Download or remove "
                     "it here, and see whether HERMES downloaded its own copy or "
                     "found one already on your system.",
             "on_show": self._expand_settings_drawer},
            {"target": lambda: getattr(self, "_update_row", None),
             "title": "Updates",
             "body": "Check now looks for a newer version right away. The other "
                     "button controls whether HERMES quietly checks on every "
                     "launch.",
             "on_show": self._expand_settings_drawer},
        ]
        TutorialWalkthrough(self.app.root, steps).start()

    def _toggle_tutorials(self):
        """Master on/off. Turning it back ON resets every module's "seen" state so
        the intros replay from a clean slate on the next visit."""
        tcfg = self.app.config.setdefault("tutorial", {})
        want = not tcfg.get("enabled", True)
        tcfg["enabled"] = want
        if want:
            tcfg["seen"] = {k: False for k in tcfg.get("seen", {})}
        self.app.save_config()
        self._refresh_tutorial_btn()
        self.note.config(text=("Tutorials will replay on your next visit to each tab."
                               if want else "Tutorials turned off."))

    def _refresh_tutorial_btn(self):
        on = self.app.config.get("tutorial", {}).get("enabled", True)
        self._tut_btn.configure(
            text="On" if on else "Off",
            bg=PALETTE["green"] if on else PALETTE["card"],
            fg=PALETTE["deep"] if on else PALETTE["text_dim"],
            activebackground=PALETTE["green"] if on else PALETTE["card_hover"],
            activeforeground=PALETTE["deep"] if on else PALETTE["text"])

    def _tutorial_help(self):
        HelpDialog(self, title="Tutorials", body=(
            "Turning this on replays the guided intro for every tab the next time "
            "you visit it.\n\nEach tab also has its own small green '?' in the "
            "bottom-right corner - click it anytime to replay that tab's tour "
            "directly, no restart needed."))

    def _dismiss_hint(self):
        """The hint's x: persist the dismissal and drop the panel (5.5.1e)."""
        self.app.config.setdefault("home", {})["hint_dismissed"] = True
        self.app.save_config()
        if self._hint is not None:
            self._hint.destroy()
            self._hint = None

    def _on_settings_toggle(self, expanded):
        """The settings drawer's header click callback: persist collapsed/expanded
        state (5.6.1)."""
        self.app.config.setdefault("home", {})["settings_expanded"] = expanded
        self.app.save_config()

    def _expand_settings_drawer(self):
        """Tutorial on_show hook: expand the settings drawer before a step targeting
        a setting positions its ring, mirroring ScrollableFrame.scroll_to's role for
        the card grid."""
        self._settings_drawer.expand(True)

    def _refresh_gap_warning(self):
        """Show/hide the red workflow warning from workflow_gaps(). Advisory only,
        never blocks a toggle. Packed after the card grid so it sits right under it."""
        gaps = workflow_gaps(self.app.config)
        if gaps:
            self._gap_warn.configure(text="Heads up: " + " · ".join(gaps))
            if not self._gap_warn.winfo_manager():
                self._gap_warn.pack(fill="x", padx=27, pady=(4, 0), after=self.scroll)
        else:
            self._gap_warn.pack_forget()

    def _set_mode(self, mode):
        """Persist the global Eco/Performance mode (read fresh by consumers - the
        editor picks it up on the NEXT render, no restart needed)."""
        self.app.config["performance_mode"] = mode
        self.app.save_config()
        self._refresh_mode_btns()
        self.note.config(text=(
            "Performance mode - parallel rendering + above-normal priority."
            if mode == "performance"
            else "Eco mode - minimal footprint while gaming or streaming."))

    def _refresh_mode_btns(self):
        current = self.app.config.get("performance_mode", "eco")
        for mode, btn in self._mode_btns.items():
            active = mode == current
            btn.configure(
                bg=PALETTE["green"] if active else PALETTE["card"],
                fg=PALETTE["deep"] if active else PALETTE["text_dim"],
                activebackground=PALETTE["green"] if active else PALETTE["card_hover"],
                activeforeground=PALETTE["deep"] if active else PALETTE["text"])

    def _mode_help(self):
        HelpDialog(self, title="Eco vs Performance", body=(
            "Eco: minimal footprint - renders run one frame at a time at normal "
            "process priority, so gaming or streaming alongside stays smooth.\n\n"
            "Performance: parallel rendering (multiple compositor threads) plus "
            "above-normal process priority - the fastest render when the PC is "
            "yours.\n\nTakes effect on the next render - no restart needed."))

    def _on_hw_change(self, _e=None):
        self.app.config["hw_encode"] = self._hw_var.get()
        self.app.save_config()
        self.note.config(
            text=f"Hardware encode: {self._hw_var.get()} - applies to the next render.")

    def _hw_help(self):
        HelpDialog(self, title="Hardware encode", body=(
            "Uses your GPU's dedicated H.264 encoder (NVIDIA NVENC, AMD AMF or "
            "Intel QSV) instead of the CPU.\n\n"
            "auto: hardware encoding only in Performance mode.\n"
            "on: both modes - NVENC runs on a separate chip, so this is the least "
            "CPU load while gaming or streaming.\n"
            "off: always libx264 on the CPU (best quality per file size).\n\n"
            "If the hardware encoder ever fails, the render automatically retries "
            "on the CPU - you never lose a render."))

    def _toggle_boot(self):
        """Flip the Windows startup entry (registry) and persist the intent in config.
        Registry write is fast + synchronous - no worker thread (Arch §Conventions 2)."""
        want = not self._boot_on
        if autostart.set_launch_on_boot(want):
            self._boot_on = want
            self.app.config["launch_on_boot"] = want
            self.app.save_config()
            self._refresh_boot_btn()
            self.note.config(text=("Will launch on startup." if want
                                   else "Won't launch on startup."))
        else:
            self.note.config(text="Couldn't change the startup setting.")

    def _refresh_boot_btn(self):
        """House style: green-accented when on, neutral when off (like the mode buttons)."""
        on = self._boot_on
        self._boot_btn.configure(
            text="On" if on else "Off",
            bg=PALETTE["green"] if on else PALETTE["card"],
            fg=PALETTE["deep"] if on else PALETTE["text_dim"],
            activebackground=PALETTE["green"] if on else PALETTE["card_hover"],
            activeforeground=PALETTE["deep"] if on else PALETTE["text"])

    def _boot_help(self):
        HelpDialog(self, title="Launch on startup", body=(
            "Adds HERMES to your Windows startup so it opens when you log in.\n\n"
            "This is the same setting as the installer's 'Launch on startup' "
            "checkbox - toggling it here updates it.\n\n"
            "Per-user only; no admin needed."))

    def _start_update_check(self, origin):
        """Run a version check on a worker thread. origin is "manual" (Check now; every
        outcome reaches the status label) or "startup" (quiet; only "newer" surfaces).
        UI thread only; the URL is snapshotted here so the worker never reads config."""
        if self._update_running:
            return
        url = update_check.effective_url(self.app.config)
        if not url:
            if origin == "manual":
                self._upd_status.config(text="No update source configured.")
            return
        self._update_running = True
        if origin == "manual":
            self._upd_check_btn.config(state="disabled")
            self._upd_status.config(text="Checking...")
        q = queue.Queue()
        threading.Thread(target=lambda: q.put(update_check.check(url)),
                         daemon=True).start()
        self.after(150, self._poll_update, q, origin)

    def _poll_update(self, q, origin):
        """UI-thread after-loop draining the worker's result queue. The worker touches
        no widgets; check() never raises, so exactly one result always arrives."""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            result = q.get_nowait()
        except queue.Empty:
            self.after(150, self._poll_update, q, origin)
            return
        self._update_running = False
        self._upd_check_btn.config(state="normal")
        if result["status"] == "newer":
            if origin == "manual":
                self._upd_status.config(text="")
            self._show_update_notice(result)
        elif origin == "manual":
            self._upd_status.config(text={
                "current": "You're on the latest version.",
                "ahead": "This build is newer than the latest release. "
                         "It may misbehave.",
            }.get(result["status"], "Couldn't check for updates."))

    def _show_update_notice(self, result):
        """One dismissable banner under the header: the headline + notes, plus
        whichever action fits (9.3c): "Update now" when can_self_update(result) says
        this build can download and apply the update itself, else the plain "Open
        download" browser link (also always offered as a fallback). A fresh "newer"
        result replaces any notice already showing. Dismissal is session-only; the
        banner returns next launch if still newer."""
        self._dismiss_update_notice()
        outer = tk.Frame(self._notice_slot, bg=PALETTE["border"])
        outer.pack(fill="x", pady=(12, 0))
        row = tk.Frame(outer, bg=PALETTE["panel"])
        row.pack(fill="x", padx=1, pady=1)
        self._update_notice = outer
        headline = result.get("title") or f"Update available (v{result['latest']})"
        tk.Label(row, text=headline, bg=PALETTE["panel"],
                 fg=PALETTE["yellow"], font=ui_font(10, "bold")).pack(
                     side="left", padx=(12, 0), pady=6)
        notes = result["notes"].strip()
        if notes:
            if len(notes) > 90:   # keep the banner one line
                notes = notes[:90].rstrip() + "..."
            tk.Label(row, text=f"- {notes}", bg=PALETTE["panel"],
                     fg=PALETTE["text_dim"], font=ui_font(9)).pack(side="left",
                                                                   padx=(8, 0))
        tk.Button(row, text="✕", command=self._dismiss_update_notice,
                  bg=PALETTE["panel"], fg=PALETTE["text_mute"],
                  activebackground=PALETTE["card_hover"],
                  activeforeground=PALETTE["text"], relief="flat", bd=0, padx=8,
                  pady=2, cursor="hand2", font=ui_font(9)).pack(side="right",
                                                                padx=(4, 8))
        if result["download_url"]:
            url = result["download_url"]
            tk.Button(row, text="Open download", command=lambda: open_url(url),
                      bg=PALETTE["card"], fg=PALETTE["text_dim"],
                      activebackground=PALETTE["card_hover"],
                      activeforeground=PALETTE["text"], relief="flat", bd=0,
                      padx=12, pady=3, cursor="hand2",
                      font=ui_font(9)).pack(side="right", padx=(8, 0))
        if update_check.can_self_update(result):
            tk.Button(row, text="Update now",
                      command=lambda: self._start_self_update(result),
                      bg=PALETTE["green"], fg=PALETTE["deep"],
                      activebackground=PALETTE["green"],
                      activeforeground=PALETTE["deep"], relief="flat", bd=0,
                      padx=12, pady=3, cursor="hand2",
                      font=ui_font(9)).pack(side="right", padx=(8, 0))

    def _dismiss_update_notice(self):
        if self._update_notice is not None:
            self._update_notice.destroy()
            self._update_notice = None

    # ── Self-update download + apply (9.3c) ─────────────────────────────────────
    def _start_self_update(self, result):
        """Kick off the manifest-diff download for a self-applicable update. The
        worker thread touches no tkinter; progress and the final outcome marshal
        through a queue drained by a UI-thread after() poll (house pattern)."""
        self._dismiss_update_notice()
        modal = _DownloadProgressModal(self, "Downloading update", "Downloading update...")
        cancel_event = threading.Event()
        modal.cancel_btn.configure(command=cancel_event.set)
        q = queue.Queue()
        threading.Thread(target=self._self_update_worker,
                         args=(result["manifest_url"], cancel_event, q),
                         daemon=True).start()
        self.after(100, self._poll_self_update, q, modal, result)

    def _self_update_worker(self, manifest_url, cancel_event, q):
        """Off the UI thread: fetch the remote manifest, diff it against the locally
        installed one, download each unique changed blob into a staging tree, then
        write the staged manifest.json + a remove.txt of dropped files. Never
        touches tkinter; every outcome goes through q."""
        staging_root = self.app.paths.update_staging_dir()
        staged_dir = os.path.join(staging_root, "staged")
        try:
            remote = update_check.fetch_manifest(manifest_url)
        except Exception as exc:  # noqa: BLE001 - network/JSON errors, report don't crash
            q.put(("error", f"could not fetch the update manifest: {exc}"))
            return
        local = update_check.load_local_manifest()
        plan = update_check.diff_manifests(local, remote)
        total_bytes = sum(size for _, size, _ in plan["download"])
        done_bytes = 0
        try:
            os.makedirs(staged_dir, exist_ok=True)
            for sha, size, relpaths in plan["download"]:
                if cancel_event.is_set():
                    q.put(("canceled", None))
                    return
                base_done = done_bytes

                def on_progress(chunk_done, _chunk_total, _base=base_done):
                    q.put(("progress", (_base + chunk_done, total_bytes)))

                blob_tmp = os.path.join(staging_root, f"_blob_{sha}")
                dl_result = downloader.download_file(
                    update_check.blob_url(manifest_url, sha), blob_tmp,
                    expected_sha256=sha, expected_size=size or None,
                    on_progress=on_progress, cancel_event=cancel_event)
                if dl_result.canceled:
                    q.put(("canceled", None))
                    return
                if not dl_result.ok:
                    q.put(("error", f"download failed: {dl_result.error}"))
                    return
                first_target = os.path.join(staged_dir, relpaths[0])
                os.makedirs(os.path.dirname(first_target), exist_ok=True)
                os.replace(blob_tmp, first_target)
                for relpath in relpaths[1:]:
                    target = os.path.join(staged_dir, relpath)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(first_target, target)
                done_bytes += size
            with open(os.path.join(staged_dir, "manifest.json"), "w",
                     encoding="utf-8") as f:
                json.dump(remote, f)
            remove_file = os.path.join(staging_root, "remove.txt")
            with open(remove_file, "w", encoding="utf-8") as f:
                f.write("\n".join(plan["remove"]))
        except OSError as exc:
            q.put(("error", f"could not stage the update: {exc}"))
            return
        q.put(("ready", (staged_dir, remove_file)))

    def _poll_self_update(self, q, modal, result):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "progress":
                    done, total = payload
                    pct = (done / total * 100) if total else None
                    detail = (f"{done / 1_000_000:.1f} / {total / 1_000_000:.1f} MB"
                             if total else "")
                    modal.set_progress(pct, detail=detail)
                elif kind == "ready":
                    modal.close()
                    self._self_update_ready(payload)
                    return
                elif kind == "canceled":
                    modal.close()
                    self._self_update_cleanup()
                    self.note.config(text="Update canceled.")
                    self._show_update_notice(result)
                    return
                else:  # error
                    modal.close()
                    self._self_update_cleanup()
                    self.note.config(text=f"Update failed: {payload}")
                    self._show_update_notice(result)
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll_self_update, q, modal, result)

    def _self_update_cleanup(self):
        shutil.rmtree(self.app.paths.update_staging_dir(create=False),
                      ignore_errors=True)

    def _self_update_ready(self, payload):
        staged_dir, remove_file = payload
        if messagebox.askyesno(
                "Update ready",
                "The update downloaded and verified. HERMES will close and reopen "
                "to finish - continue?", parent=self):
            self.app.apply_update(staged_dir, remove_file)
        else:
            self._self_update_cleanup()
            self.note.config(
                text="Update ready - click Update now again when you're ready.")

    def _toggle_update_launch(self):
        want = not self.app.config.get("update_check_on_launch", True)
        self.app.config["update_check_on_launch"] = want
        self.app.save_config()
        self._refresh_update_btn()
        self.note.config(text=("Will check for updates on launch." if want
                               else "Won't check for updates on launch."))

    def _refresh_update_btn(self):
        """Same on/off style as the launch-on-boot button."""
        on = self.app.config.get("update_check_on_launch", True)
        self._upd_launch_btn.configure(
            text="On launch: On" if on else "On launch: Off",
            bg=PALETTE["green"] if on else PALETTE["card"],
            fg=PALETTE["deep"] if on else PALETTE["text_dim"],
            activebackground=PALETTE["green"] if on else PALETTE["card_hover"],
            activeforeground=PALETTE["deep"] if on else PALETTE["text"])

    def _update_help(self):
        HelpDialog(self, title="Updates", body=(
            "Check now fetches a small version file online and compares it with "
            "this build. Nothing downloads or installs automatically; if a newer "
            "version exists, a notice appears with a button that opens the "
            "download page in your browser.\n\n"
            "On launch runs the same check quietly at startup and only speaks up "
            "when a newer version is available.\n\n"
            "No data about you or your clips is sent."))

    def _restart(self):
        """Relaunch the app via the hub so enable/disable changes take effect. Confirms
        first so an accidental click can't relaunch mid-work."""
        restart = getattr(self.app, "restart", None)
        if not callable(restart):
            self.note.config(text="Restart unavailable (run via main.py).")
            return
        if messagebox.askyesno(
                f"Restart {version.APP_NAME}",
                "Restart now? Any unsaved work in other tabs may be lost."):
            restart()
