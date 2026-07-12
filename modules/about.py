"""About tab (Phase 4.3b) - brand + version, creator links, Special thanks, supporters wall.

Always present and last. Renders supporter data from shared/supporters.py: the bundled
seed/cache instantly (offline), then if config["supporters_url"] is set, one background worker
fetches the hosted JSON, merges immortal names, caches it, and marshals the result back via the
queue + _ui_pump pattern (workers never touch tkinter). Startup never waits on the network.

The supporters wall stacks tier bands top to bottom (Gigabyte spotlight, Megabyte elevated, then
Kilobyte, Byte). Each band's names are reshuffled every launch; a tier whose names overflow its
width auto-scrolls horizontally, decided dynamically and re-evaluated on resize. Animation pauses
when the tab is not visible. Imports only shared/ + stdlib (never another module).
"""

import math
import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from shared import supporters, version
from shared.ui_helpers import (
    PALETTE, FeedbackDialog, ScrollableFrame, Tooltip, TutorialIntro, TutorialReplayButton,
    TutorialWalkthrough, display_font, load_brand_icon, make_feedback_bar, make_link_bar,
    open_url, post_feedback, round_rect, ui_font,
)

_ROLE_LABELS = {"streamer": "Streamers", "friend": "Friends", "tester": "Testers"}

# Lane animation: a gentle ticker (slow, readable), not fast motion.
_SCROLL_STEP = 1        # px per tick
_SCROLL_MS = 33         # ~30 px/s
_STRIP_GAP = 48         # gap between the two looping copies
_CHIP_GAP = 8           # gap between chips


def _draw_star(c, cx, cy, r, color):
    """A small filled 5-point star (Gigabyte spotlight + pinned-thanks marker)."""
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.42
        pts += [cx + rad * math.cos(ang), cy + rad * math.sin(ang)]
    c.create_polygon(pts, fill=color, outline="")


class AboutModule(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._closing = False
        self._ui_queue = queue.Queue()      # worker->UI results (see _marshal)
        self._ui_pump_job = None
        # per-lane state, keyed by tier key
        self._lanes = {}                    # key -> tk.Canvas
        self._scroll_jobs = {}              # key -> after id (animation tick)
        self._configure_jobs = {}           # key -> after id (resize debounce)
        self._scrolling = {}                # key -> bool (overflow / animating)
        self._lane_period = {}              # key -> loop period px
        self._lane_off = {}                 # key -> accumulated scroll offset
        self._lane_last_w = {}              # key -> last rendered canvas width

        self._data = supporters.load_local(app.paths)   # instant, offline-safe
        self._lane_font = ui_font(10)
        self._lane_font_obj = tkfont.Font(family=self._lane_font[0], size=self._lane_font[1])
        self._chip_h = self._lane_font_obj.metrics("linespace") + 8
        self._lane_h = self._chip_h + 12

        self._build()
        self._ui_pump_job = self.after(50, self._ui_pump)
        self.bind("<Map>", self._on_show)
        self.bind("<Unmap>", self._on_hide)
        self._maybe_refresh()

    # ── layout ───────────────────────────────────────────────────────────────────────
    def _build(self):
        bg = PALETTE["bg"]
        self.scroll = ScrollableFrame(self)
        self.scroll.pack(fill="both", expand=True)
        body = self.scroll.body
        assets = self.app.paths.get_path("assets", create=False)

        header = tk.Frame(body, bg=bg)
        header.pack(fill="x", padx=24, pady=(20, 6))
        titlerow = tk.Frame(header, bg=bg)
        titlerow.pack(anchor="w")
        logo = load_brand_icon(assets, "hermes", size=30)
        if logo is not None:
            ll = tk.Label(titlerow, image=logo, bg=bg)
            ll.image = logo
            ll.pack(side="left", padx=(0, 10))
        tk.Label(titlerow, text=version.DISPLAY_NAME, bg=bg, fg=PALETTE["text"],
                 font=display_font(20)).pack(side="left")
        tk.Label(header, text=version.EXPANSION, bg=bg, fg=PALETTE["text_mute"],
                 font=ui_font(10)).pack(anchor="w", pady=(4, 0))
        tk.Label(header, text="Turn raw stream clips into finished YouTube Shorts, fast.",
                 bg=bg, fg=PALETTE["text_dim"], font=ui_font(10)).pack(anchor="w", pady=(2, 0))
        make_link_bar(header, self.app.config.get("links", {}), assets).pack(
            anchor="w", pady=(12, 2))

        self._feedback_bar = make_feedback_bar(body, on_open=self._open_feedback_dialog)
        self._feedback_bar.pack(anchor="w", padx=24, pady=(4, 0))

        self._area = tk.Frame(body, bg=bg)
        self._area.pack(fill="both", expand=True, padx=24, pady=(8, 8))
        self._build_supporters_area()

        tk.Label(body, text=f"HERMES v{version.display_version()} · made by {version.PUBLISHER}",
                 bg=bg, fg=PALETTE["text_mute"], font=ui_font(9)).pack(
            anchor="w", padx=24, pady=(6, 16))

        # Always-present per-tab tutorial replay button (bottom-right corner, 5.5.4b).
        TutorialReplayButton(self, self._start_walkthrough,
                             "New here? Replay the tour.")

    # ── Tutorial (5.5.4b) ────────────────────────────────────────────────────────────
    def maybe_show_tutorial(self):
        """Hub hook (main.py's <<NotebookTabChanged>> dispatch, see 5.5.4a): show the
        About intro popup on first visit, once tutorials have been asked about and
        are on."""
        tcfg = self.app.config.get("tutorial", {})
        if not tcfg.get("asked") or not tcfg.get("enabled"):
            return
        if tcfg.get("seen", {}).get("about"):
            return
        self._offer_tutorial()

    def _offer_tutorial(self):
        """Show About's intro popup. Marks "seen" the moment it's shown - skipping
        and touring both count, so the intro never repeats either way."""
        tcfg = self.app.config.setdefault("tutorial", {})
        tcfg.setdefault("seen", {})["about"] = True
        self.app.save_config()
        TutorialIntro(
            self, title="Welcome to About",
            body=("Credits, supporters, and where to send feedback or report a "
                  "bug."),
            on_show_me=self._start_walkthrough)

    def _tour_scroll_to(self, attr):
        """on_show helper: bring a named widget into view inside self.scroll before its
        step's ring is drawn (5.5.4b beta fix, same as importer/exporter's)."""
        w = getattr(self, attr, None)
        if w is not None:
            try:
                self.scroll.scroll_to(w)
            except Exception:   # noqa: BLE001 - a bad scroll target must never crash the tour
                pass

    def _start_walkthrough(self):
        """About's 2-step tour: the supporters area, then feedback/bug report.
        Also the entry point for the replay button."""
        steps = [
            {"target": lambda: getattr(self, "_area", None),
             "on_show": lambda: self._tour_scroll_to("_area"),
             "title": "Special thanks / Supporters",
             "body": "Everyone who helped build HERMES, and everyone supporting "
                     "it - reshuffled and updated each launch."},
            {"target": lambda: getattr(self, "_feedback_bar", None),
             "on_show": lambda: self._tour_scroll_to("_feedback_bar"),
             "title": "Send feedback / Report a bug",
             "body": "These open a quick message form. Discord is the direct "
                     "channel for screenshots or clips - nothing gets uploaded "
                     "here."},
        ]
        TutorialWalkthrough(self.app.root, steps).start()

    def _build_supporters_area(self):
        """(Re)build the two-column supporters area from self._data. Safe to call again on a
        background refresh: cancels lane jobs and clears the old widgets first."""
        for key in list(self._scroll_jobs):
            self._stop_lane(key)
        for key, job in list(self._configure_jobs.items()):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._configure_jobs.clear()
        for child in self._area.winfo_children():
            child.destroy()
        self._lanes.clear()
        self._scrolling.clear()
        self._lane_period.clear()
        self._lane_off.clear()
        self._lane_last_w.clear()

        bg = PALETTE["bg"]
        # LEFT: Special thanks - a bordered card, sized to its content (min width 180).
        card_fill = PALETTE["deep"]
        accent = PALETTE["green"]
        card = tk.Frame(self._area, bg=card_fill, highlightthickness=1,
                        highlightbackground=accent, highlightcolor=accent)
        card.pack(side="left", anchor="n")
        tk.Frame(card, bg=card_fill, width=180, height=1).pack()   # min-width spacer
        left = tk.Frame(card, bg=card_fill)
        left.pack(fill="x", padx=12, pady=12)
        tk.Label(left, text="Special thanks", bg=card_fill, fg=PALETTE["text"],
                 font=display_font(13)).pack(anchor="w")
        tk.Label(left, text="Helped build HERMES.", bg=card_fill, fg=PALETTE["text_mute"],
                 font=ui_font(9)).pack(anchor="w", pady=(0, 6))
        for entry in supporters.thanks_pinned(self._data):
            self._thanks_row(left, entry, pinned=True, fill=card_fill)
        groups = supporters.thanks_groups(self._data)
        for role in supporters.THANKS_ROLES:
            entries = groups.get(role, [])
            if not entries:
                continue
            tk.Label(left, text=_ROLE_LABELS[role], bg=card_fill, fg=PALETTE["text_mute"],
                     font=ui_font(9)).pack(anchor="w", pady=(8, 1))
            for entry in entries:
                self._thanks_row(left, entry, pinned=False, fill=card_fill)

        # RIGHT: tier bands (expand)
        right = tk.Frame(self._area, bg=bg)
        right.pack(side="left", fill="both", expand=True, padx=(18, 0))
        tk.Label(right, text="Supporters", bg=bg, fg=PALETTE["text"],
                 font=display_font(13)).pack(anchor="w", pady=(0, 6))
        for key in supporters.tier_render_order(self._data):
            self._build_band(right, key)

    def _thanks_row(self, parent, entry, pinned, fill=None):
        bg = fill or PALETTE["bg"]
        row = tk.Frame(parent, bg=bg)
        row.pack(anchor="w", fill="x", pady=1)
        marker = tk.Canvas(row, width=14, height=15, bg=bg, highlightthickness=0)
        marker.pack(side="left")
        if pinned:
            _draw_star(marker, 6, 8, 5, PALETTE["yellow"])
        else:
            marker.create_oval(4, 6, 8, 10, fill=PALETTE["text_faint"], outline="")
        url = entry.get("url", "")
        lbl = tk.Label(row, text=entry["name"], bg=bg,
                       fg=PALETTE["text"] if pinned else PALETTE["text_dim"],
                       font=ui_font(10, "bold") if pinned else ui_font(10), anchor="w")
        lbl.pack(side="left")
        if url:
            lbl.configure(cursor="hand2")
            lbl.bind("<Button-1>", lambda _e, u=url: open_url(u))
            Tooltip(lbl, "Open link")

    def _build_band(self, parent, key):
        meta = supporters.TIER_META[key]
        accent = PALETTE[meta["accent"]]
        spotlight = key == "gigabyte"
        elevated = key == "megabyte"
        bw = 2 if (spotlight or elevated) else 1
        fill = PALETTE["deep"]
        pad = 12 if spotlight else 9
        band = tk.Frame(parent, bg=fill, highlightthickness=bw,
                        highlightbackground=accent, highlightcolor=accent)
        band.pack(fill="x", pady=(0, 10))

        labelrow = tk.Frame(band, bg=fill)
        labelrow.pack(fill="x", padx=pad, pady=(pad, 2))
        lbl_size = 14 if spotlight else (12 if elevated else 11)
        mk = tk.Canvas(labelrow, width=lbl_size + 6, height=lbl_size + 6, bg=fill,
                       highlightthickness=0)
        mk.pack(side="left", padx=(0, 6))
        cc = (lbl_size + 6) / 2
        if spotlight:
            _draw_star(mk, cc, cc, lbl_size * 0.5, accent)
        else:
            r = lbl_size * 0.28
            mk.create_oval(cc - r, cc - r, cc + r, cc + r, fill=accent, outline="")
        tk.Label(labelrow, text=meta["label"], bg=fill, fg=accent,
                 font=display_font(lbl_size)).pack(side="left")
        count = supporters.anonymous_count(self._data, key)
        if count:
            tk.Label(labelrow, text=f"+{count} anonymous", bg=fill, fg=PALETTE["text_mute"],
                     font=ui_font(9)).pack(side="right")

        lane = tk.Canvas(band, height=self._lane_h, bg=fill, highlightthickness=0)
        lane.pack(fill="x", padx=pad, pady=(0, pad))
        self._lanes[key] = lane
        lane.bind("<Configure>", lambda _e, k=key: self._on_lane_configure(k))

    # ── names lane (dynamic auto-scroll) ───────────────────────────────────────────────
    def _chip_width(self, entry):
        return 9 + 6 + 6 + self._lane_font_obj.measure(entry["name"]) + 9   # pad+dot+gap+text+pad

    def _draw_chip(self, lane, x, cy, entry, accent, tag):
        h = self._chip_h
        w = self._chip_width(entry)
        tags = (tag,) if tag else ()
        round_rect(lane, x, cy - h / 2, x + w, cy + h / 2, r=h / 2,
                   fill=PALETTE["card"], outline=accent, width=1, tags=tags)
        dcx = x + 9 + 3
        if entry.get("immortal"):   # hollow ring marks an immortal supporter
            lane.create_oval(dcx - 3, cy - 3, dcx + 3, cy + 3, outline=accent, width=2, tags=tags)
        else:
            lane.create_oval(dcx - 3, cy - 3, dcx + 3, cy + 3, fill=accent, outline="", tags=tags)
        lane.create_text(x + 9 + 6 + 6, cy, text=entry["name"], anchor="w",
                         fill=PALETTE["text"], font=self._lane_font, tags=tags)
        return w

    def _render_lane(self, key):
        lane = self._lanes.get(key)
        if lane is None or not lane.winfo_exists():
            return
        canvas_w = lane.winfo_width()
        if canvas_w <= 1:
            return   # not laid out yet; renders on first <Configure>/show
        self._stop_lane(key)
        lane.delete("all")
        self._lane_last_w[key] = canvas_w
        h = lane.winfo_height()
        cy = (h if h > 1 else self._lane_h) / 2
        accent = PALETTE[supporters.TIER_META[key]["accent"]]
        entries = supporters.shuffled_tier(self._data, key)
        if not entries:
            lane.create_text(2, cy, text="Be the first.", anchor="w",
                             fill=PALETTE["text_mute"], font=self._lane_font)
            self._scrolling[key] = False
            return
        widths = [self._chip_width(e) for e in entries]
        content_w = sum(widths) + _CHIP_GAP * (len(entries) - 1)
        if content_w <= canvas_w:
            x = 0
            for entry in entries:
                x += self._draw_chip(lane, x, cy, entry, accent, None) + _CHIP_GAP
            self._scrolling[key] = False
            return
        # overflow -> auto-scroll: two copies end-to-end, looped by canvas.move
        period = content_w + _STRIP_GAP
        for copy in (0, 1):
            x = copy * period
            for entry in entries:
                x += self._draw_chip(lane, x, cy, entry, accent, "strip") + _CHIP_GAP
        self._lane_period[key] = period
        self._lane_off[key] = 0
        self._scrolling[key] = True
        if self.winfo_viewable():
            self._start_lane(key)

    def _render_all_lanes(self):
        for key in list(self._lanes):
            self._render_lane(key)

    def _on_lane_configure(self, key):
        job = self._configure_jobs.get(key)
        if job is not None:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._configure_jobs[key] = self.after(120, lambda k=key: self._lane_configured(k))

    def _lane_configured(self, key):
        self._configure_jobs[key] = None
        lane = self._lanes.get(key)
        if lane is None or not lane.winfo_exists():
            return
        if lane.winfo_width() != self._lane_last_w.get(key):
            self._render_lane(key)   # width changed -> re-decide static vs scroll

    def _start_lane(self, key):
        if not self._scrolling.get(key) or self._scroll_jobs.get(key) is not None:
            return
        self._scroll_jobs[key] = self.after(_SCROLL_MS, lambda k=key: self._lane_tick(k))

    def _stop_lane(self, key):
        job = self._scroll_jobs.pop(key, None)
        if job is not None:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass

    def _lane_tick(self, key):
        self._scroll_jobs[key] = None
        if self._closing or not self._scrolling.get(key) or not self.winfo_viewable():
            return   # paused; resumes on <Map> via _render_all_lanes
        lane = self._lanes.get(key)
        if lane is None or not lane.winfo_exists():
            return
        try:
            lane.move("strip", -_SCROLL_STEP, 0)
            off = self._lane_off.get(key, 0) + _SCROLL_STEP
            period = self._lane_period.get(key, 1)
            if off >= period:
                lane.move("strip", period, 0)   # seamless wrap (second copy takes over)
                off -= period
            self._lane_off[key] = off
        except tk.TclError:
            return
        self._scroll_jobs[key] = self.after(_SCROLL_MS, lambda k=key: self._lane_tick(k))

    # ── visibility (pause when the tab is not shown) ───────────────────────────────────
    def _on_show(self, _e=None):
        self._render_all_lanes()

    def _on_hide(self, _e=None):
        for key in list(self._lanes):
            self._stop_lane(key)

    # ── feedback / bug report (Phase 5.5.5) ────────────────────────────────────────────
    def _open_feedback_dialog(self, kind):
        target = (self.app.config.get("feedback_url") or self.app.config.get("supporters_url")
                  or "").strip()
        FeedbackDialog(
            self, kind, can_send=bool(target),
            on_send=lambda dlg, k, msg, link, t=target: self._send_feedback(dlg, t, k, msg, link),
        )

    def _send_feedback(self, dialog, target_url, kind, message, link):
        app_version = version.display_version()
        threading.Thread(
            target=self._feedback_worker,
            args=(dialog, target_url, kind, message, link, app_version),
            daemon=True,
        ).start()

    def _feedback_worker(self, dialog, target_url, kind, message, link, app_version):
        ok, detail = post_feedback(target_url, kind, message, link, app_version)
        self._marshal(self._apply_feedback_result, dialog, ok, detail)

    def _apply_feedback_result(self, dialog, ok, detail):
        dialog.show_result(ok, detail)

    # ── background refresh ─────────────────────────────────────────────────────────────
    def _maybe_refresh(self):
        url = (self.app.config.get("supporters_url") or "").strip()
        if not url:
            return   # empty = render from seed/cache only (no network)
        threading.Thread(target=self._fetch_worker, args=(url,), daemon=True).start()

    def _fetch_worker(self, url):
        """Worker thread: fetch -> merge immortal names -> cache -> marshal to the UI. Any
        failure is swallowed (keep showing current data)."""
        try:
            data = supporters.fetch(url)
            # Preserve immortals from the last real sync only (the cache), never the bundled
            # seed -> seed placeholders can't leak into live synced data.
            merged = supporters.merge_immortal(supporters.load_cache(self.app.paths), data)
            supporters.save_cache(self.app.paths, merged)
        except Exception as ex:   # noqa: BLE001 - offline / bad JSON must never crash
            print(f"[about] supporters refresh skipped: {ex}")
            return
        self._marshal(self._apply_supporters, merged)

    def _apply_supporters(self, data):
        self._data = data
        self._build_supporters_area()
        if self.winfo_viewable():
            self.after(60, self._render_all_lanes)

    # ── worker->UI marshalling (the editor/exporter queue + pump pattern) ──────────────
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

    # ── hub lifecycle ──────────────────────────────────────────────────────────────────
    def on_close(self):
        """Cancel the UI pump, every lane scroll job, and any pending resize debounce (a
        stray after firing post-destroy throws a Tcl error)."""
        self._closing = True
        if self._ui_pump_job is not None:
            try:
                self.after_cancel(self._ui_pump_job)
            except tk.TclError:
                pass
            self._ui_pump_job = None
        for key in list(self._lanes):
            self._stop_lane(key)
        for key, job in list(self._configure_jobs.items()):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._configure_jobs.clear()
