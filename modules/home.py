"""Home tab - the control center.

Always loaded, first tab, never disabled. Presents every registry module as a sleek,
clickable card with a function icon and a green/red status lamp: click a card to
enable/disable it (writes config). The card grid lives in a scrollable, reflowing
container so it scales cleanly from 3 modules today to 8-20+ later.

Visuals are all drawn with Canvas vector primitives (no image files / no extra deps) to
stay lightweight. Imports ONLY from shared/ and stdlib (modules never import each other).
"""

import math
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from shared import autostart, registry, update_check, version
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

        # ── Settings: the global Eco/Performance mode (2.8.4) ──
        tk.Label(self, text="SETTINGS", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(anchor="w", padx=27, pady=(8, 4))
        srow = tk.Frame(self, bg=PALETTE["bg"])
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
        self._hw_row = hrow = tk.Frame(self, bg=PALETTE["bg"])
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

        # Launch-on-boot toggle (dist.4): mirrors the installer's checkbox via HKCU Run.
        # Reflects the actual REGISTRY state, not just config - shows On if the installer
        # set it. Source of truth for display is the registry; config records the intent.
        brow = tk.Frame(self, bg=PALETTE["bg"])
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
        self._update_row = urow = tk.Frame(self, bg=PALETTE["bg"])
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
        kbrow = tk.Frame(self, bg=PALETTE["bg"])
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
        trow = tk.Frame(self, bg=PALETTE["bg"])
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

        self.note = tk.Label(self, text="", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                             font=ui_font(9))
        self.note.pack(anchor="w", padx=27, pady=(6, 14))

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

        # One-shot first-launch prompts: keyboard layout (5.5.2b correction), then
        # tutorials (5.5.4a). Chained in one callback (not two separate after() calls at
        # the same delay) so the two modal dialogs never stack; each askyesno blocks
        # until answered before the next one can open.
        if (not self.app.config.get("keyboard_layout_asked")
                or not self.app.config.get("tutorial", {}).get("asked")):
            self.after(1200, self._run_first_launch_prompts)

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
        happens on restart (the hub lazy-imports at startup)."""
        new_state = not registry.is_enabled(key, self.app.config)
        self.app.config.setdefault("enabled_modules", {})[key] = new_state
        self.app.save_config()
        verb = "enabled" if new_state else "disabled"
        self.note.config(text=f"{registry.get_label(key)} {verb} - Restart to apply.")
        self._refresh_gap_warning()

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
        """Home's 7-step tour: module cards, keyboard layout, performance mode,
        launch on startup, then a closing stretch on the three settings the earlier
        steps skip over - links, hardware encode, updates (5.5.4b beta addition).
        Also the entry point for the replay button."""
        steps = [
            {"target": lambda: self.scroll, "title": "Module cards",
             "body": "Click a card to enable or disable that module. Enabled "
                     "modules show up as their own tab after a restart."},
            {"target": lambda: getattr(self, "_kb_box", None),
             "title": "Keyboard layout",
             "body": "Match this to your physical keyboard so the editor's "
                     "undo/redo keys land in the right place."},
            {"target": lambda: getattr(self, "_perf_row", None),
             "title": "Performance mode",
             "body": "Eco keeps rendering light so gaming or streaming alongside "
                     "stays smooth. Performance renders faster when the PC is "
                     "all yours."},
            {"target": lambda: getattr(self, "_boot_btn", None),
             "title": "Launch on startup",
             "body": "Turn this on to have HERMES open automatically when you "
                     "log in."},
            {"target": lambda: getattr(self, "_link_bar", None),
             "title": "Saltydayn's socials",
             "body": "YouTube, Twitch, Discord, and X - come hang out, get in "
                     "touch, or support HERMES. Same links on the About tab."},
            {"target": lambda: getattr(self, "_hw_row", None),
             "title": "Hardware encode",
             "body": "Auto uses your GPU's hardware encoder in Performance mode "
                     "for faster renders, falling back to software if none is "
                     "found. Force it On or Off if you'd rather choose yourself."},
            {"target": lambda: getattr(self, "_update_row", None),
             "title": "Updates",
             "body": "Check now looks for a newer version right away. The other "
                     "button controls whether HERMES quietly checks on every "
                     "launch."},
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
        """One dismissable banner under the header: new version + notes + a download
        button. A fresh "newer" result replaces any notice already showing. Dismissal
        is session-only; the banner returns next launch if still newer."""
        self._dismiss_update_notice()
        outer = tk.Frame(self._notice_slot, bg=PALETTE["border"])
        outer.pack(fill="x", pady=(12, 0))
        row = tk.Frame(outer, bg=PALETTE["panel"])
        row.pack(fill="x", padx=1, pady=1)
        self._update_notice = outer
        tk.Label(row, text="Update available:", bg=PALETTE["panel"],
                 fg=PALETTE["text"], font=ui_font(10)).pack(side="left",
                                                            padx=(12, 0), pady=6)
        tk.Label(row, text=f"v{result['latest']}", bg=PALETTE["panel"],
                 fg=PALETTE["yellow"], font=ui_font(10, "bold")).pack(side="left",
                                                                      padx=(6, 0))
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
                      bg=PALETTE["green"], fg=PALETTE["deep"],
                      activebackground=PALETTE["green"],
                      activeforeground=PALETTE["deep"], relief="flat", bd=0,
                      padx=12, pady=3, cursor="hand2",
                      font=ui_font(9)).pack(side="right", padx=(8, 0))

    def _dismiss_update_notice(self):
        if self._update_notice is not None:
            self._update_notice.destroy()
            self._update_notice = None

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
