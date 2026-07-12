"""Reusable UI: dark theme, HelpDialog, themed widget factories.

No module-specific code lives here - only widgets/styling shared by more than one tab.
Palette and look are defined once in PALETTE + apply_dark_theme so the whole app stays
consistent. Keep it flat, dark, and cheap (Prime Directive: optics never beat speed).
"""

import os
import urllib.parse
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# Palette (from claude-guidance/2_ARCHITECTURE.md). Single source of truth for colors.
PALETTE = {
    "bg": "#1e1e1e",
    "panel": "#2d2d2d",
    "deep": "#151515",
    "green": "#52b788",     # primary / success
    "yellow": "#ffd166",    # secondary / warn
    "blue": "#4ea8de",      # info
    "orange": "#f4a261",    # highlight
    "red": "#ff4d4d",       # error / remove
    "red_soft": "#ff6b6b",
    "text": "#ffffff",
    "text_dim": "#cccccc",
    "text_mute": "#8a8a8a",
    "text_faint": "#555555",
    # Card / surface tones for the futuristic flat look.
    "card": "#242424",          # resting card fill
    "card_hover": "#2f2f2f",    # card fill on hover
    "border": "#3a3a3a",        # subtle card outline
    "lamp_on_glow": "#2f6b50",  # dim ring behind a green lamp
    "lamp_off_glow": "#7a2a2a", # dim ring behind a red lamp
}

# Fonts are chosen at runtime from what's installed (graceful fallback keeps the app
# portable). Prefer Bahnschrift - a condensed, slightly industrial/"futuristic" sans that
# ships with Windows 10+ - then fall back to Segoe UI. Populated by init_fonts(root).
_FONTS = {"display": "Segoe UI", "body": "Segoe UI", "mono": "Consolas"}


def init_fonts(root):
    """Pick the sleekest installed font for each role. Safe to call once at startup."""
    try:
        fams = set(tkfont.families(root))
    except tk.TclError:
        return
    def pick(prefs, default):
        return next((p for p in prefs if p in fams), default)
    _FONTS["display"] = pick(["Bahnschrift SemiBold", "Segoe UI Semibold", "Segoe UI"], "Segoe UI")
    _FONTS["body"] = pick(["Bahnschrift", "Segoe UI"], "Segoe UI")
    _FONTS["mono"] = pick(["Cascadia Mono", "Consolas"], "Consolas")


def display_font(size, weight=None):
    """Header/label font tuple (the condensed display family)."""
    return (_FONTS["display"], size) if weight is None else (_FONTS["display"], size, weight)


def ui_font(size, weight=None):
    """Body font tuple."""
    return (_FONTS["body"], size) if weight is None else (_FONTS["body"], size, weight)


def mono_font(size, weight=None):
    """Monospace font tuple (for code/numbers)."""
    return (_FONTS["mono"], size) if weight is None else (_FONTS["mono"], size, weight)


def round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    """Draw a rounded rectangle on a Canvas via a smoothed polygon. Returns the item id.
    No image deps - cheap vector drawing, redrawable on hover/resize."""
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kwargs)


def draw_lamp(c, cx, cy, on):
    """Draw a small glowing status lamp on Canvas `c` at (cx, cy): green = on, red = off.

    Shared app-wide so any module (Home cards today, the editor's render status later) can
    show the same green/red state indicator. Cheap vector drawing - no image deps."""
    glow = PALETTE["lamp_on_glow"] if on else PALETTE["lamp_off_glow"]
    core = PALETTE["green"] if on else PALETTE["red"]
    c.create_oval(cx - 7, cy - 7, cx + 7, cy + 7, outline=glow, width=2)
    c.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=core, outline="")
    c.create_oval(cx - 3.5, cy - 3.5, cx - 1, cy - 1, fill="#ffffff", outline="")  # gloss

# Button kinds → fill color. "neutral" uses the panel color for low-emphasis actions.
_BUTTON_KINDS = {
    "primary": PALETTE["green"],
    "secondary": PALETTE["yellow"],
    "info": PALETTE["blue"],
    "highlight": PALETTE["orange"],
    "danger": PALETTE["red"],
    "neutral": PALETTE["panel"],
}


def apply_dark_theme(root):
    """Apply the dark palette to a Tk root and ttk widgets.

    Uses the 'clam' ttk theme as the base because the native Windows themes ignore most
    color options. Returns the configured ttk.Style."""
    root.configure(bg=PALETTE["bg"])
    init_fonts(root)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass  # fall back to whatever's available; colors below still apply where honored

    bg, panel, deep = PALETTE["bg"], PALETTE["panel"], PALETTE["deep"]
    text, dim = PALETTE["text"], PALETTE["text_dim"]
    accent = PALETTE["green"]

    style.configure(".", background=bg, foreground=text, fieldbackground=panel,
                    bordercolor=deep, lightcolor=panel, darkcolor=deep, font=ui_font(10))
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=text, font=ui_font(10))
    style.configure("Dim.TLabel", background=bg, foreground=dim, font=ui_font(10))
    style.configure("Header.TLabel", background=bg, foreground=text, font=display_font(15))
    style.configure("Panel.TFrame", background=panel)

    style.configure("TButton", background=panel, foreground=text, borderwidth=0,
                    focusthickness=0, padding=(10, 5), font=ui_font(10))
    style.map("TButton",
              background=[("active", deep), ("pressed", deep)],
              foreground=[("disabled", PALETTE["text_faint"])])

    # Notebook (tab bar)
    style.configure("TNotebook", background=deep, borderwidth=0)
    style.configure("TNotebook.Tab", background=panel, foreground=dim,
                    padding=(16, 8), borderwidth=0, font=display_font(10))
    style.map("TNotebook.Tab",
              background=[("selected", bg)],
              foreground=[("selected", accent)])

    style.configure("TCheckbutton", background=bg, foreground=text, font=ui_font(10))
    style.map("TCheckbutton", background=[("active", bg)])
    # Radiobutton - same on-palette treatment as Checkbutton. Without this it falls back to
    # clam's light-grey default (the Plate Left/Center/Right artifact on the dark theme).
    style.configure("TRadiobutton", background=bg, foreground=text, font=ui_font(10))
    style.map("TRadiobutton", background=[("active", bg)],
              indicatorcolor=[("selected", accent)])
    style.configure("TSeparator", background=panel)

    # Scrollbar - slim and dark to match the flat look.
    style.configure("Vertical.TScrollbar", background=panel, troughcolor=bg,
                    bordercolor=bg, arrowcolor=dim, width=10)
    style.map("Vertical.TScrollbar", background=[("active", PALETTE["border"])])

    # Entry - dark field, accent focus ring, light caret.
    style.configure("TEntry", fieldbackground=panel, foreground=text, insertcolor=text,
                    bordercolor=deep, lightcolor=deep, darkcolor=deep, padding=(6, 4))
    style.map("TEntry", bordercolor=[("focus", accent)], lightcolor=[("focus", accent)])

    # Combobox - the collapsed/readonly field was rendering light under clam; force dark.
    style.configure("TCombobox", fieldbackground=panel, background=panel, foreground=text,
                    arrowcolor=dim, bordercolor=deep, lightcolor=deep, darkcolor=deep,
                    selectbackground=panel, selectforeground=text, padding=(6, 4))
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", panel), ("disabled", bg)],
        foreground=[("readonly", text), ("disabled", PALETTE["text_faint"])],
        background=[("active", deep)],
        arrowcolor=[("active", accent)],
        selectbackground=[("readonly", panel)], selectforeground=[("readonly", text)],
        bordercolor=[("focus", accent)], lightcolor=[("focus", accent)],
    )
    # The drop-down list is a classic Tk Listbox, themed via the option DB (not ttk style).
    root.option_add("*TCombobox*Listbox.background", panel)
    root.option_add("*TCombobox*Listbox.foreground", text)
    root.option_add("*TCombobox*Listbox.selectBackground", accent)
    root.option_add("*TCombobox*Listbox.selectForeground", deep)
    root.option_add("*TCombobox*Listbox.font", ui_font(10))

    # Progressbar - on-palette (green bar on a deep trough) instead of the clam default.
    style.configure("Horizontal.TProgressbar", background=accent, troughcolor=deep,
                    bordercolor=deep, lightcolor=accent, darkcolor=accent, thickness=14)

    return style


def themed_button(parent, text, command=None, kind="primary", **kwargs):
    """A flat colored tk.Button. `kind` ∈ primary|secondary|info|highlight|danger|neutral.

    Uses tk.Button (not ttk) so we can set exact fill colors and a hover effect simply."""
    fill = _BUTTON_KINDS.get(kind, _BUTTON_KINDS["primary"])
    fg = PALETTE["deep"] if kind != "neutral" else PALETTE["text"]

    btn = tk.Button(
        parent, text=text, command=command,
        bg=fill, fg=fg, activebackground=fill, activeforeground=fg,
        relief="flat", bd=0, padx=12, pady=6, cursor="hand2",
        highlightthickness=0, font=("Segoe UI", 10), **kwargs,
    )

    def _on_enter(_):
        if str(btn["state"]) != "disabled":
            btn.configure(bg=PALETTE["deep"], fg=fill if kind != "neutral" else PALETTE["text"])

    def _on_leave(_):
        btn.configure(bg=fill, fg=fg)

    btn.bind("<Enter>", _on_enter)
    btn.bind("<Leave>", _on_leave)
    return btn


def bind_scale_jump(scale):
    """Fix tk.Scale's hostile stock mouse bindings (the value label eats clicks, a
    trough click steps by exactly 1, the thumb is a tiny target): press or drag
    anywhere in the widget jumps the value to the cursor and follows it. Promoted
    from the editor so every module's slider behaves the same."""
    scale.bind("<ButtonPress-1>", _scale_jump)
    scale.bind("<B1-Motion>", _scale_jump)


def _scale_jump(event):
    s = event.widget
    s.focus_set()   # "break" below also suppresses the stock focus-on-click
    slider = int(str(s.cget("sliderlength")))
    span = s.winfo_width() - slider
    if span <= 0:
        return "break"
    lo, hi = float(str(s.cget("from"))), float(str(s.cget("to")))
    frac = min(1.0, max(0.0, (event.x - slider / 2) / span))
    s.set(round(lo + frac * (hi - lo)))   # widget .set() DOES fire the command
    return "break"   # suppress the stock bindings so they don't fight the jump


# ── Creator links + persistent support button (Phase 4.2) ─────────────────────────────
# Brand accent colors and the fixed display order (twitch first = primary). Used by the
# Home header and the About tab - one widget, no copy-paste (convention 7).
BRAND = {"twitch": "#9146FF", "youtube": "#FF0000", "discord": "#5865F2",
         "x": "#ffffff", "kofi": "#FF5E5B"}
LINK_ORDER = ("twitch", "youtube", "discord", "x", "kofi")
_LINK_LABELS = {"twitch": "Twitch", "youtube": "YouTube", "discord": "Discord",
                "x": "X", "kofi": "Ko-fi"}

# Decoded icons, reused across builds. Keyed by (assets_dir, name, size) so two callers
# pointing at different asset folders never collide (matters in tests; in the app the dir
# is constant). A PhotoImage is bound to the interpreter that made it, so each cached image
# is liveness-probed and rebuilt if its root was destroyed.
_brand_icon_cache = {}


def open_url(url):
    """Open `url` in the system browser (new tab). No-op on blank/None url; never raises."""
    if not url or not str(url).strip():
        return
    try:
        webbrowser.open(url, new=2)
    except Exception:   # noqa: BLE001 - a missing browser must never crash the app
        pass


def load_brand_icon(assets_dir, name, size=18):
    """Return a tk.PhotoImage for assets/social_<name>.png (or <name>.png), downscaled so its
    HEIGHT is near `size` px (aspect kept, so a non-square logo still matches the label
    height), cached by (assets_dir, name, size). Returns None if the file is missing or
    unreadable so the caller can fall back to a colored text label. Stdlib only (PhotoImage
    reads PNG on Tk 8.6+); no Pillow needed."""
    key = (assets_dir, name, size)
    if key in _brand_icon_cache:
        cached = _brand_icon_cache[key]
        if cached is None:
            return None
        try:
            cached.width()              # liveness probe: TclError if its interpreter is gone
            return cached
        except tk.TclError:
            del _brand_icon_cache[key]  # stale (a prior root was destroyed) - rebuild below
    img = None
    for fname in (f"social_{name}.png", f"{name}.png"):
        path = os.path.join(assets_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            raw = tk.PhotoImage(file=path)
            h = raw.height()
            factor = max(1, int(round(h / size))) if h > size else 1
            img = raw.subsample(factor, factor) if factor > 1 else raw
        except tk.TclError:
            img = None
            continue
        break
    _brand_icon_cache[key] = img
    return img


def _attach_hover(btn, rest, hot):
    """Subtle bg swap on hover for the flat link/support buttons. No after() - safe."""
    btn.bind("<Enter>", lambda _e: btn.configure(bg=hot), add="+")
    btn.bind("<Leave>", lambda _e: btn.configure(bg=rest), add="+")


def _line_height(font_tuple):
    """Pixel line height of a font tuple, so an icon can be sized to match its label height."""
    try:
        weight = font_tuple[2] if len(font_tuple) > 2 else "normal"
        f = tkfont.Font(family=font_tuple[0], size=font_tuple[1], weight=weight)
        return max(14, int(f.metrics("linespace")))
    except (tk.TclError, IndexError):
        return 18


def _mix(c1, c2, t):
    """Blend two #rrggbb colors: t=0 -> c1, t=1 -> c2. Used for subtle brand-tinted fills."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make_link_bar(parent, links, assets_dir, primary="twitch", font_size=13):
    """A horizontal Frame of icon+label buttons, one per LINK_ORDER key present in `links`.

    Each button shows the brand icon (sized to the label's height) plus its name, and opens
    links[key] in the system browser. `primary` gets a 2px accent border in its brand color;
    the rest a 1px outline. A missing icon falls back to colored text only (BRAND[key]) so the
    bar works before final art lands. An empty/blank url renders the button but the click is a
    no-op (keeps layout stable pre-config). One tooltip per button. Returns the Frame.

    `font_size` scales the whole bar (label + icon together) for callers that want it larger."""
    bar = tk.Frame(parent, bg=PALETTE["bg"])
    bar._link_icons = []   # keep PhotoImage refs alive, else GC blanks the buttons
    font = ui_font(font_size, "bold")
    icon_px = _line_height(font)
    for key in LINK_ORDER:
        if key not in links:
            continue
        url = links.get(key) or ""
        icon = load_brand_icon(assets_dir, key, size=icon_px)
        brand = BRAND.get(key, PALETTE["green"])
        # Branded look: every button carries a faint brand-tinted fill (the primary a touch
        # stronger), a 2px outline in its brand color, and a matching colored hover glow, so
        # the whole bar reads as branded links rather than generic buttons.
        rest = _mix(PALETTE["card"], brand, 0.16 if key == primary else 0.13)
        hot = _mix(PALETTE["card"], brand, 0.32)
        label = _LINK_LABELS.get(key, key.capitalize())
        kw = dict(
            text=(("  " + label) if icon is not None else label), compound="left",
            bg=rest, activebackground=hot,
            fg=(PALETTE["text"] if icon is not None else brand),
            relief="flat", bd=0, cursor="hand2",
            highlightthickness=2, highlightbackground=brand, highlightcolor=brand,
            font=font, padx=11, pady=6,
            command=(lambda u=url: open_url(u)),
        )
        if icon is not None:
            kw["image"] = icon
            bar._link_icons.append(icon)
        btn = tk.Button(bar, **kw)
        btn.pack(side="left", padx=4)
        _attach_hover(btn, rest, hot)
        Tooltip(btn, label)
    return bar


def make_kofi_button(parent, url, assets_dir, compact=False, font_size=11):
    """The Ko-fi support button. compact=True -> icon-only pill; else icon + 'Support'. The
    icon is sized to the label height. Opens `url` in the browser (no-op if blank). Carries a
    2px Ko-fi outline + a faint coral resting tint and hover glow so it stands out from regular
    buttons; text fallback ('Ko-fi' / 'Support') when the icon is missing. Returns it."""
    font = ui_font(font_size, "bold")
    icon = load_brand_icon(assets_dir, "kofi", size=_line_height(font))
    brand = BRAND["kofi"]
    rest = _mix(PALETTE["card"], brand, 0.16)
    hot = _mix(PALETTE["card"], brand, 0.34)
    kw = dict(
        bg=rest, activebackground=hot,
        relief="flat", bd=0, cursor="hand2",
        highlightthickness=2, highlightbackground=brand, highlightcolor=brand,
        font=font, padx=(8 if compact else 11), pady=4,
        command=(lambda u=url: open_url(u)),
    )
    label = "" if compact else "Support"
    if icon is not None:
        kw["image"] = icon
        kw["compound"] = "left"
        kw["fg"] = PALETTE["text"]
        if label:
            kw["text"] = "  " + label
        btn = tk.Button(parent, **kw)
        btn._kofi_icon = icon   # keep ref alive
    else:
        kw["fg"] = brand
        kw["text"] = ("Ko-fi" if compact else "Support")
        btn = tk.Button(parent, **kw)
    _attach_hover(btn, rest, hot)
    Tooltip(btn, "Support HERMES on Ko-fi")
    return btn


# ── Feedback / bug report relay (Phase 5.5.5) ──────────────────────────────────────────
# Low-sensitivity anti-spam token: authorizes posting feedback text to the kofi-sync Apps
# Script relay, nothing more. Not a secret in the credential sense (same trust level as a
# public contact form) - must match the FEEDBACK_TOKEN Script Property on the deployment.
FEEDBACK_TOKEN = "hermes-fb-7f3a1c9e2b6d4f80"


def feedback_payload(kind, message, link, app_version):
    """Build the form-encoded field dict for a feedback/bug POST. Pure - no I/O - so it can
    be unit tested without a network."""
    return {
        "kind": kind,
        "message": message,
        "link": link or "",
        "app_version": app_version,
        "feedback_token": FEEDBACK_TOKEN,
    }


def post_feedback(url, kind, message, link, app_version, timeout=10):
    """Blocking POST of a feedback/bug report to the kofi-sync feedback relay. Returns
    (ok, detail). Never raises - any failure (blank url, network, timeout, bad response) is
    caught and reported as (False, reason). Call this off the UI thread; the caller marshals
    the result back to tkinter."""
    if not url or not str(url).strip():
        return False, "No feedback address configured."
    body = urllib.parse.urlencode(feedback_payload(kind, message, link, app_version)).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = resp.read().decode("utf-8", errors="replace")
    except Exception as ex:   # noqa: BLE001 - network failure must never crash the app
        return False, str(ex)
    if "ok" in reply:
        return True, "Sent."
    return False, f"Server said: {reply[:120]}"


def make_feedback_bar(parent, on_open=None, font_size=10):
    """Two small utility buttons, "Send feedback" and "Report a bug", styled as a quieter
    sibling of the Ko-fi button (thin outline, faint tint, hover glow). `on_open(kind)` is
    called with "feedback" or "bug" on click; the caller owns opening a FeedbackDialog since
    it needs app-specific config. Returns the Frame."""
    bar = tk.Frame(parent, bg=PALETTE["bg"])
    font = ui_font(font_size)
    accent = PALETTE["blue"]
    rest = _mix(PALETTE["card"], accent, 0.10)
    hot = _mix(PALETTE["card"], accent, 0.24)

    def _btn(text, kind):
        b = tk.Button(
            bar, text=text, bg=rest, activebackground=hot, fg=PALETTE["text_dim"],
            relief="flat", bd=0, cursor="hand2",
            highlightthickness=1, highlightbackground=PALETTE["border"],
            highlightcolor=PALETTE["border"], font=font, padx=9, pady=4,
            command=(lambda k=kind: on_open(k)) if on_open else None,
        )
        b.pack(side="left", padx=(0, 6))
        _attach_hover(b, rest, hot)
        return b

    _btn("Send feedback", "feedback")
    _btn("Report a bug", "bug")
    return bar


def show_toast(root, title, body, timeout_ms=12000, action_label=None, action=None):
    """A non-modal themed toast pinned to the bottom-right of `root`. Auto-dismisses after
    `timeout_ms`, has an X to close now, and an OPTIONAL secondary action button (low-emphasis
    neutral). No grab_set (never modal); safe if the root closes first. Returns the Toplevel,
    or None if the root is already gone."""
    try:
        top = tk.Toplevel(root)
    except tk.TclError:
        return None
    top.overrideredirect(True)                 # borderless
    try:
        top.transient(root)
    except tk.TclError:
        pass
    top.configure(bg=PALETTE["border"])        # 1px frame revealed by inner padding

    state = {"after": None}

    def _cancel():
        if state["after"] is not None:
            try:
                top.after_cancel(state["after"])
            except tk.TclError:
                pass
            state["after"] = None

    def _close():
        _cancel()
        try:
            top.destroy()
        except tk.TclError:
            pass

    inner = tk.Frame(top, bg=PALETTE["card"])
    inner.pack(padx=1, pady=1, fill="both", expand=True)

    head = tk.Frame(inner, bg=PALETTE["card"])
    head.pack(fill="x", padx=12, pady=(10, 0))
    tk.Label(head, text=title, bg=PALETTE["card"], fg=PALETTE["text"],
             font=display_font(12)).pack(side="left")
    close = tk.Label(head, text="✕", bg=PALETTE["card"], fg=PALETTE["text_mute"],
                     font=ui_font(11), cursor="hand2")
    close.pack(side="right")
    close.bind("<Button-1>", lambda _e: _close())

    tk.Label(inner, text=body, bg=PALETTE["card"], fg=PALETTE["text_dim"], font=ui_font(10),
             justify="left", wraplength=300).pack(anchor="w", padx=12, pady=(6, 10))

    if action_label and action:
        def _do_action():
            try:
                action()
            finally:
                _close()
        themed_button(inner, action_label, command=_do_action, kind="neutral").pack(
            anchor="e", padx=12, pady=(0, 10))

    # Position bottom-right of root, computed once after layout.
    top.update_idletasks()
    try:
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        rw, rh = root.winfo_width(), root.winfo_height()
        w, h = top.winfo_width(), top.winfo_height()
        top.geometry(f"+{rx + rw - w - 16}+{ry + rh - h - 16}")
    except tk.TclError:
        pass

    # Auto-dismiss; cancel the timer on any destroy so it never fires post-teardown.
    top.bind("<Destroy>", lambda _e: _cancel(), add="+")
    try:
        state["after"] = top.after(timeout_ms, _close)
    except tk.TclError:
        pass
    return top


class HelpDialog(tk.Toplevel):
    """A themed modal popup that shows step-by-step help text.

    Used wherever a setup/API-key field needs a `?` button with instructions. Pass `steps`
    as a list of strings (rendered as a numbered list) or `body` as freeform text.
    """

    def __init__(self, parent, title="Help", steps=None, body=None):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=PALETTE["bg"])
        self.resizable(False, False)
        self.transient(parent)

        wrap = tk.Frame(self, bg=PALETTE["bg"])
        wrap.pack(fill="both", expand=True, padx=16, pady=14)

        tk.Label(wrap, text=title, bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=display_font(14)).pack(anchor="w", pady=(0, 8))

        if steps:
            for i, step in enumerate(steps, 1):
                tk.Label(wrap, text=f"{i}.  {step}", bg=PALETTE["bg"],
                         fg=PALETTE["text_dim"], justify="left", wraplength=420,
                         font=ui_font(10)).pack(anchor="w", pady=2)
        if body:
            tk.Label(wrap, text=body, bg=PALETTE["bg"], fg=PALETTE["text_dim"],
                     justify="left", wraplength=420, font=ui_font(10)).pack(
                         anchor="w", pady=2)

        themed_button(wrap, "Got it", command=self.destroy, kind="primary").pack(
            anchor="e", pady=(12, 0))

        # Center over parent, grab focus (modal).
        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")
        except tk.TclError:
            pass
        self.grab_set()
        self.bind("<Escape>", lambda _e: self.destroy())


class FeedbackDialog(tk.Toplevel):
    """Feedback / bug-report dialog (Phase 5.5.5). Modeled on HelpDialog for theming,
    centering, and Escape-to-close. Collects a message (plus, for feedback only, an
    optional supporters-wall link) and calls `on_send(dialog, kind, message, link)` when
    Send is clicked. The caller owns the network POST - it must run off the UI thread - and
    reports back through `mark_sending()` / `show_result(ok, detail)`. `can_send=False`
    disables Send entirely (e.g. no feedback URL configured) without hiding the dialog, so
    Discord still reads as a fallback contact route.
    """

    def __init__(self, parent, kind, can_send=True, on_send=None):
        super().__init__(parent)
        self.kind = kind
        self._on_send = on_send
        self._can_send = can_send
        title = "Report a bug" if kind == "bug" else "Send feedback"
        self.title(title)
        self.configure(bg=PALETTE["bg"])
        self.resizable(False, False)
        self.transient(parent)

        wrap = tk.Frame(self, bg=PALETTE["bg"])
        wrap.pack(fill="both", expand=True, padx=16, pady=14)

        tk.Label(wrap, text=title, bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=display_font(14)).pack(anchor="w", pady=(0, 8))
        intro = ("Found something broken? Describe what you did and what happened."
                  if kind == "bug" else
                  "Tell me what's working, what's not, or what you'd like to see next.")
        tk.Label(wrap, text=intro, bg=PALETTE["bg"], fg=PALETTE["text_dim"], justify="left",
                 wraplength=380, font=ui_font(10)).pack(anchor="w", pady=(0, 8))

        self.text = tk.Text(wrap, width=46, height=6, bg=PALETTE["card"], fg=PALETTE["text"],
                            insertbackground=PALETTE["text"], relief="flat", bd=0,
                            wrap="word", font=ui_font(10), padx=8, pady=6)
        self.text.pack(fill="x")
        self.text.bind("<KeyRelease>", lambda _e: self._sync_send_state())
        self.text.focus_set()

        self.link_entry = None
        if kind == "feedback":
            linkrow = tk.Frame(wrap, bg=PALETTE["bg"])
            linkrow.pack(fill="x", pady=(10, 0))
            tk.Label(linkrow, text="Optional: a link for your supporters entry", bg=PALETTE["bg"],
                     fg=PALETTE["text_dim"], font=ui_font(9)).pack(side="left")
            qbtn = tk.Label(linkrow, text="?", bg=PALETTE["card"], fg=PALETTE["text_dim"],
                            font=ui_font(9, "bold"), cursor="hand2", padx=5)
            qbtn.pack(side="left", padx=(6, 0))
            qbtn.bind("<Button-1>", lambda _e: HelpDialog(
                self, "Supporters listing",
                body=("Testers and feedback-givers can be listed on the About tab's "
                      "Special thanks wall. Drop a link here (your channel, socials, "
                      "whatever) and it will show up next to your name if you get added.")))
            self.link_entry = tk.Entry(wrap, bg=PALETTE["card"], fg=PALETTE["text"],
                                       insertbackground=PALETTE["text"], relief="flat", bd=0,
                                       font=ui_font(10))
            self.link_entry.pack(fill="x", pady=(4, 0), ipady=4)

        self.status = tk.Label(wrap, text="", bg=PALETTE["bg"], fg=PALETTE["text_mute"],
                               justify="left", wraplength=380, font=ui_font(9))
        self.status.pack(anchor="w", pady=(8, 0))

        tk.Label(wrap, text=("Screenshots or clips aren't uploaded here - you can reach me on "
                             "Discord instead (the Discord button is on the Home tab)."),
                 bg=PALETTE["bg"], fg=PALETTE["text_mute"], justify="left", wraplength=380,
                 font=ui_font(9)).pack(anchor="w", pady=(4, 0))

        btnrow = tk.Frame(wrap, bg=PALETTE["bg"])
        btnrow.pack(fill="x", pady=(12, 0))
        themed_button(btnrow, "Cancel", command=self.destroy, kind="neutral").pack(
            side="right", padx=(6, 0))
        self.send_btn = themed_button(btnrow, "Send", command=self._on_send_click, kind="primary")
        self.send_btn.pack(side="right")

        if not can_send:
            self.status.configure(text="Feedback isn't reachable right now - use Discord instead.",
                                  fg=PALETTE["yellow"])
        self._sync_send_state()

        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")
        except tk.TclError:
            pass
        self.grab_set()
        self.bind("<Escape>", lambda _e: self.destroy())

    def _sync_send_state(self):
        has_text = bool(self.text.get("1.0", "end").strip())
        self.send_btn.configure(state=("normal" if (has_text and self._can_send) else "disabled"))

    def _on_send_click(self):
        message = self.text.get("1.0", "end").strip()
        if not message or not self._can_send or self._on_send is None:
            return
        link = self.link_entry.get().strip() if self.link_entry is not None else ""
        self.mark_sending()
        self._on_send(self, self.kind, message, link)

    def mark_sending(self):
        if not self.winfo_exists():
            return
        self.send_btn.configure(state="disabled", text="Sending...")
        self.status.configure(text="Sending...", fg=PALETTE["text_mute"])

    def show_result(self, ok, detail):
        if not self.winfo_exists():
            return
        if ok:
            self.status.configure(text="Thanks, that was sent.", fg=PALETTE["green"])
            try:
                self.after(900, self.destroy)
            except tk.TclError:
                pass
        else:
            self.send_btn.configure(state="normal", text="Send")
            self.status.configure(
                text=f"Couldn't send ({detail}). Your text is still here - try again or use Discord.",
                fg=PALETTE["red_soft"])


class Tooltip:
    """Hover tooltip: themed, delayed, self-cleaning. Attach to any widget:
        Tooltip(widget, "Short one-line explanation.")
    Shows ~`delay_ms` after the pointer enters, hides on leave/click/destroy. The show
    timer is cancelled on <Leave> AND on <Destroy> so no after() fires post-teardown (the
    editor closes/reopens constantly - a stale after throws a Tcl error). No focus/grab, no
    mouse-wheel capture - it must never steal interaction. Cheap: one Toplevel + one Label,
    created lazily on show and destroyed on leave."""

    def __init__(self, widget, text, delay_ms=500, wraplength=260):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.wraplength = wraplength
        self._after_id = None
        self._tip = None
        self._px = self._py = 0
        # add="+" so we never clobber the widget's own <Enter>/<Leave>/press bindings.
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._on_destroy, add="+")

    def _schedule(self, event):
        self._cancel()
        self._px, self._py = event.x_root, event.y_root
        try:
            self._after_id = self.widget.after(self.delay_ms, self._show)
        except tk.TclError:
            self._after_id = None   # widget already gone

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self):
        self._after_id = None
        if self._tip is not None:
            return
        try:
            tip = tk.Toplevel(self.widget)
            tip.overrideredirect(True)             # borderless
            tip.configure(bg=PALETTE["border"])    # 1px frame revealed by the label padding
            tk.Label(tip, text=self.text, bg=PALETTE["card"], fg=PALETTE["text_dim"],
                     font=ui_font(9), wraplength=self.wraplength, justify="left",
                     padx=6, pady=4).pack(padx=1, pady=1)
            tip.geometry(f"+{self._px + 12}+{self._py + 16}")   # a few px below-right
            self._tip = tip
        except tk.TclError:
            self._tip = None   # widget/display gone mid-show - never raise

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None

    def _on_destroy(self, _event=None):
        self._cancel()
        self._hide()


class TutorialIntro(tk.Toplevel):
    """Modal first-visit popup for a module's tutorial (Phase 5.5.4a). Modeled on
    HelpDialog (centering, grab_set, Escape-to-close) but with two buttons instead of
    one: "Show me around" starts the walkthrough, "Skip" just closes. The caller marks
    the module "seen" before showing this: skipping and touring both count as seen.
    """

    def __init__(self, parent, title, body, on_show_me=None, on_skip=None):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=PALETTE["bg"])
        self.resizable(False, False)
        self.transient(parent)

        wrap = tk.Frame(self, bg=PALETTE["bg"])
        wrap.pack(fill="both", expand=True, padx=18, pady=16)

        tk.Label(wrap, text=title, bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=display_font(15)).pack(anchor="w", pady=(0, 8))
        tk.Label(wrap, text=body, bg=PALETTE["bg"], fg=PALETTE["text_dim"],
                 justify="left", wraplength=360, font=ui_font(10)).pack(anchor="w")

        def _skip():
            self.destroy()
            if on_skip:
                on_skip()

        def _show():
            self.destroy()
            if on_show_me:
                on_show_me()

        btnrow = tk.Frame(wrap, bg=PALETTE["bg"])
        btnrow.pack(fill="x", pady=(16, 0))
        themed_button(btnrow, "Skip", command=_skip, kind="neutral").pack(side="right")
        themed_button(btnrow, "Show me around", command=_show, kind="primary").pack(
            side="right", padx=(0, 8))

        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")
        except tk.TclError:
            pass
        self.grab_set()
        self.bind("<Escape>", lambda _e: _skip())


class TutorialWalkthrough:
    """Multi-step guided tour: a green spotlight ring around a target widget plus a
    floating step card (title/body/Back/Next/Skip). Non-modal by design: nothing is
    drawn ON TOP of the target, so the user can still click and use the highlighted
    control while a step is showing. Manual progression only, no auto-advance.

    `steps` is a list of dicts: {"target": callable-returning-widget-or-None, "title",
    "body", "on_show": optional callable run before positioning (e.g. expand a
    collapsed section)}. `target` is a callable (not a widget) because the real widget
    may not exist yet when the step list is built, or gets recreated on rebuild.
    """

    def __init__(self, root, steps):
        self.root = root
        self.steps = steps
        self.idx = 0
        self._ring = []
        self._card = None
        self._configure_funcid = None
        self._escape_bound = False
        self._tick_job = None

    def start(self):
        if not self.steps:
            return
        self.idx = 0
        self._configure_funcid = self.root.bind(
            "<Configure>", self._on_root_configure, add="+")
        self.root.bind_all("<Escape>", self._on_escape, add="+")
        self._escape_bound = True
        self._show_step()
        self._schedule_tick()

    def _schedule_tick(self):
        """Reposition ring/card on a short timer, not just on the root's own
        <Configure>. A target inside a ScrollableFrame moves when the user scrolls it
        manually mid-step, which fires no <Configure> at all - the timer is what keeps
        the ring honest in that case (5.5.4b beta fix)."""
        self._tick_job = self.root.after(120, self._tick)

    def _tick(self):
        self._tick_job = None
        if not self._ring and self._card is None:
            return
        self._on_root_configure(None)
        self._schedule_tick()

    def _resolve_target(self, step):
        target_fn = step.get("target")
        if target_fn is None:
            return None
        try:
            w = target_fn()
        except Exception:   # noqa: BLE001 - a bad target callable must never crash the tour
            return None
        if w is None:
            return None
        try:
            if not w.winfo_exists() or not w.winfo_ismapped():
                return None
        except tk.TclError:
            return None
        return w

    def _show_step(self):
        for t in self._ring:
            try:
                t.destroy()
            except tk.TclError:
                pass
        self._ring = []
        if self._card is not None:
            try:
                self._card.destroy()
            except tk.TclError:
                pass
            self._card = None

        step = self.steps[self.idx]
        on_show = step.get("on_show")
        if callable(on_show):
            try:
                on_show()
            except Exception:   # noqa: BLE001 - a bad on_show must never crash the tour
                pass
        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass

        target = self._resolve_target(step)
        if target is not None:
            self._draw_ring(target)
        self._draw_card(step, target)

    def _on_root_configure(self, event=None):
        """Keep the ring + card aligned when the window moves/resizes. Filters to the
        root's OWN events (child <Configure> bubbles up to the toplevel's bindings,
        same gotcha main.py's window-geometry tracker guards against)."""
        if event is not None and event.widget is not self.root:
            return
        if not self._ring and self._card is None:
            return
        step = self.steps[self.idx]
        target = self._resolve_target(step)
        if target is not None and self._ring:
            self._position_ring(target)
        if self._card is not None:
            self._position_card(target)

    def _draw_ring(self, target):
        for _ in range(4):
            try:
                t = tk.Toplevel(self.root)
                t.overrideredirect(True)
                t.configure(bg=PALETTE["green"])
                t.attributes("-topmost", True)
            except tk.TclError:
                return
            self._ring.append(t)
        self._position_ring(target)

    def _position_ring(self, target):
        try:
            tx, ty = target.winfo_rootx(), target.winfo_rooty()
            tw, th = target.winfo_width(), target.winfo_height()
        except tk.TclError:
            return
        pad = thick = 3
        x0, y0 = tx - pad, ty - pad
        x1, y1 = tx + tw + pad, ty + th + pad
        geoms = (
            (x0, y0, x1 - x0, thick),          # top
            (x0, y1 - thick, x1 - x0, thick),  # bottom
            (x0, y0, thick, y1 - y0),          # left
            (x1 - thick, y0, thick, y1 - y0),  # right
        )
        for t, (gx, gy, gw, gh) in zip(self._ring, geoms):
            try:
                t.geometry(f"{int(max(1, gw))}x{int(max(1, gh))}+{int(gx)}+{int(gy)}")
            except tk.TclError:
                pass

    def _draw_card(self, step, target):
        try:
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
        except tk.TclError:
            return
        top.configure(bg=PALETTE["border"])
        inner = tk.Frame(top, bg=PALETTE["card"])
        inner.pack(padx=1, pady=1)

        n = len(self.steps)
        tk.Label(inner, text=f"Step {self.idx + 1} of {n}", bg=PALETTE["card"],
                 fg=PALETTE["text_mute"], font=ui_font(9)).pack(
                     anchor="w", padx=12, pady=(10, 0))
        tk.Label(inner, text=step.get("title", ""), bg=PALETTE["card"],
                 fg=PALETTE["text"], font=display_font(13)).pack(
                     anchor="w", padx=12, pady=(2, 6))
        tk.Label(inner, text=step.get("body", ""), bg=PALETTE["card"],
                 fg=PALETTE["text_dim"], justify="left", wraplength=260,
                 font=ui_font(10)).pack(anchor="w", padx=12)

        btnrow = tk.Frame(inner, bg=PALETTE["card"])
        btnrow.pack(fill="x", padx=12, pady=(12, 10))
        back_btn = themed_button(btnrow, "Back", command=self._back, kind="neutral")
        back_btn.pack(side="left")
        if self.idx == 0:
            back_btn.configure(state="disabled")
        themed_button(btnrow, "Skip", command=self._cleanup, kind="neutral").pack(
            side="left", padx=(8, 0))
        next_label = "Done" if self.idx == n - 1 else "Next"
        themed_button(btnrow, next_label, command=self._next, kind="primary").pack(
            side="right")

        self._card = top
        self._position_card(target)

    def _position_card(self, target):
        """Pick whichever side (right / left / below / above the target) actually has
        room for the card, instead of always trying right-then-left and clamping
        blind - the old clamp could pull the card back ONTO the target when the target
        was wide or near an edge (5.5.4b beta fix: the card covered the very control
        it was pointing at)."""
        card = self._card
        if card is None:
            return
        try:
            card.update_idletasks()
            w, h = card.winfo_width(), card.winfo_height()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
        except tk.TclError:
            return
        gap = 14
        if target is None:
            x = rx + max(0, (rw - w) // 2)
            y = ry + max(0, (rh - h) // 2)
        else:
            try:
                tx, ty = target.winfo_rootx(), target.winfo_rooty()
                tw, th = target.winfo_width(), target.winfo_height()
            except tk.TclError:
                tx, ty, tw, th = rx, ry, 0, 0

            room_right = (rx + rw) - (tx + tw) - gap
            room_left = tx - rx - gap
            room_below = (ry + rh) - (ty + th) - gap
            room_above = ty - ry - gap

            if room_right >= w:
                x, y = tx + tw + gap, ty
            elif room_left >= w:
                x, y = tx - w - gap, ty
            elif room_below >= h:
                x, y = tx, ty + th + gap
            elif room_above >= h:
                x, y = tx, ty - h - gap
            else:
                # Nothing fits cleanly (a very wide/tall target, or a small window) -
                # use whichever side has the most room so the card still leans away
                # from the target rather than defaulting onto it.
                best = max((room_right, "right"), (room_left, "left"),
                          (room_below, "below"), (room_above, "above"))
                side = best[1]
                if side == "right":
                    x, y = tx + tw + gap, ty
                elif side == "left":
                    x, y = tx - w - gap, ty
                elif side == "below":
                    x, y = tx, ty + th + gap
                else:
                    x, y = tx, ty - h - gap

            x = max(rx + 8, min(x, rx + rw - w - 8))
            y = max(ry + 8, min(y, ry + rh - h - 8))
        try:
            card.geometry(f"+{int(x)}+{int(y)}")
        except tk.TclError:
            pass

    def _back(self):
        if self.idx > 0:
            self.idx -= 1
            self._show_step()

    def _next(self):
        if self.idx >= len(self.steps) - 1:
            self._cleanup()
            return
        self.idx += 1
        self._show_step()

    def _on_escape(self, _e=None):
        self._cleanup()

    def _cleanup(self):
        if self._tick_job is not None:
            try:
                self.root.after_cancel(self._tick_job)
            except tk.TclError:
                pass
            self._tick_job = None
        for t in self._ring:
            try:
                t.destroy()
            except tk.TclError:
                pass
        self._ring = []
        if self._card is not None:
            try:
                self._card.destroy()
            except tk.TclError:
                pass
            self._card = None
        if self._configure_funcid is not None:
            try:
                self.root.unbind("<Configure>", self._configure_funcid)
            except tk.TclError:
                pass
            self._configure_funcid = None
        if self._escape_bound:
            try:
                self.root.unbind_all("<Escape>")
            except tk.TclError:
                pass
            self._escape_bound = False


def TutorialReplayButton(parent, on_replay, caption, font_size=9):
    """Small green "?" button + caption, pinned to `parent`'s bottom-right corner via
    place() (independent of the caller's own pack/grid layout). Present permanently,
    not gated on tutorial state. Clicking it replays that module's walkthrough
    directly (no intro popup, no re-ask). Returns the outer Frame."""
    frame = tk.Frame(parent, bg=PALETTE["bg"])
    accent = PALETTE["green"]
    rest = _mix(PALETTE["card"], accent, 0.16)
    hot = _mix(PALETTE["card"], accent, 0.32)
    btn = tk.Button(
        frame, text="?", command=on_replay,
        bg=rest, activebackground=hot, fg=accent,
        relief="flat", bd=0, cursor="hand2",
        highlightthickness=2, highlightbackground=accent, highlightcolor=accent,
        font=ui_font(font_size, "bold"), padx=8, pady=2,
    )
    btn.pack(side="left")
    _attach_hover(btn, rest, hot)
    tk.Label(frame, text=caption, bg=PALETTE["bg"], fg=PALETTE["text_mute"],
             font=ui_font(font_size)).pack(side="left", padx=(6, 0))
    frame.place(relx=1.0, rely=1.0, anchor="se", x=-16, y=-16)
    return frame


class ScrollableFrame(ttk.Frame):
    """A vertically scrollable container. Put content in `.body` (a ttk.Frame).

    The scrollbar auto-hides when everything fits (pass `autohide=False` to keep it
    always visible - a full-range bar reads as inert), mouse-wheel scrolls while
    hovered, and `.body` is kept at the viewport width so child layouts can reflow.
    Lightweight - one Canvas + one inner frame; built so the Home grid can grow to
    8-20+ cards without overflowing the window.
    """

    def __init__(self, parent, autohide=True, **kwargs):
        super().__init__(parent, **kwargs)
        self._autohide = autohide
        self.canvas = tk.Canvas(self, bg=PALETTE["bg"], highlightthickness=0, bd=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                  style="Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.canvas.pack(side="left", fill="both", expand=True)
        if not autohide:
            self.vbar.pack(side="right", fill="y")

        self.body.bind("<Configure>", self._on_body_config)
        self.canvas.bind("<Configure>", self._on_canvas_config)
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_body_config(self, _e):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_scrollbar()

    def _on_canvas_config(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)  # body fills viewport width

    def _on_yscroll(self, lo, hi):
        self.vbar.set(lo, hi)
        self._sync_scrollbar()

    def _sync_scrollbar(self):
        if not self._autohide:
            return  # bar stays packed permanently
        lo, hi = self.canvas.yview()
        needed = not (lo <= 0.0 and hi >= 1.0)
        mapped = self.vbar.winfo_ismapped()
        if needed and not mapped:
            self.vbar.pack(side="right", fill="y")
        elif not needed and mapped:
            self.vbar.pack_forget()

    def _on_wheel(self, e):
        if self.canvas.yview() == (0.0, 1.0):
            return  # nothing to scroll
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def scroll_to(self, widget, margin=24):
        """Scroll so `widget` (a descendant of .body) is visible, with a small margin.
        Used as a TutorialWalkthrough step's on_show (5.5.4b) so a target below the
        fold is brought into view before the ring is drawn - otherwise the ring ends
        up pointing at a widget that is technically mapped but scrolled out of the
        canvas viewport. No-op if geometry isn't settled yet or there's nothing to
        scroll."""
        try:
            self.canvas.update_idletasks()
            # winfo_rooty() of both widget and body already reflect the current scroll
            # offset (the canvas moves the body's window item as it scrolls), so the
            # difference is the widget's FIXED position within body's own content
            # coordinates - the same frame the canvas scrollregion uses.
            wy = widget.winfo_rooty() - self.body.winfo_rooty()
            wh = widget.winfo_height()
        except tk.TclError:
            return
        bbox = self.canvas.bbox("all")
        if not bbox:
            return
        total_h = bbox[3] - bbox[1]
        view_h = self.canvas.winfo_height()
        if total_h <= view_h:
            return  # everything already fits, nothing to scroll
        lo, _hi = self.canvas.yview()
        top_visible = lo * total_h
        bottom_visible = top_visible + view_h
        if wy < top_visible + margin:
            frac = max(0.0, (wy - margin) / total_h)
            self.canvas.yview_moveto(frac)
        elif wy + wh > bottom_visible - margin:
            frac = max(0.0, (wy + wh + margin - view_h) / total_h)
            self.canvas.yview_moveto(frac)
