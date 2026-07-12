"""Streamer Companion - hub launcher.

Thin. Owns the root window, the tab notebook, the registry/bus/config wiring, and clean
shutdown. NO module-specific logic lives here - if feature code lands in this file, it's
misplaced. Modules are lazy-imported (disabled modules cost nothing at startup) and each
gets an AppContext for everything it needs.

Run: python main.py
"""

import importlib
import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from shared import config as config_mod
from shared import paths, registry, version
from shared.bus import MessageBus
from shared.ui_helpers import apply_dark_theme, make_kofi_button


class AppContext:
    """Everything a module needs, handed in at construction. Modules use this and never
    reach into other modules or touch config.json / paths directly."""

    def __init__(self, root):
        self.root = root
        self.config = config_mod.load_config()
        self.base_dir = paths.BASE_DIR
        self.paths = paths            # modules call app.paths.get_path("Shorts")
        self.registry = registry
        self.bus = MessageBus()
        self.restart = None           # hub installs the real relaunch callable at startup

    def save_config(self):
        config_mod.save_config(self.config)


class StreamerCompanion:
    def __init__(self, root):
        self.root = root
        self.root.title(version.window_title())
        self._set_window_icon()
        self.root.minsize(720, 480)

        paths.ensure_data_dirs()
        apply_dark_theme(root)

        self.app = AppContext(root)
        self.app.bus.set_handoff_handler(self._handle_handoff)
        self.app.restart = self._restart   # Home's Restart button calls this

        # Restore the user's last window size + position (else a sane default). Set before
        # the window is mapped so it opens in place - incl. after a Restart, not top-left.
        # Maximized is restored as state, never as pixels: geometry() while zoomed reports
        # the zoomed size, and restoring THAT as a normal window loses the real size.
        saved_win = self.app.config.get("window") or {}
        saved_geo = saved_win.get("geometry")
        self._last_normal_geo = saved_geo or "900x600"
        self.root.geometry(self._last_normal_geo)
        if saved_win.get("zoomed"):
            self.root.state("zoomed")
        root.bind("<Configure>", self._on_root_configure)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)

        self.tabs = {}  # module key -> instance
        self._load_modules()
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Persistent Ko-fi support button. A ROOT child (not a notebook page) pinned
        # top-right, so it shows on EVERY tab including ones added later, and switching tabs
        # cannot hide it (2_ARCHITECTURE: the support button is always present, no module may
        # cover it). relx=1.0 keeps it top-right on resize; the tab strip is left-aligned so
        # this corner is free. FALLBACK if a future tab count ever reaches this corner: pack
        # a thin top-chrome tk.Frame above the notebook and put this same widget in it,
        # right-aligned - same widget, same guarantee, costs one row of height.
        links = self.app.config.get("links", {})
        self.kofi_btn = make_kofi_button(
            root, links.get("kofi", ""),
            paths.get_path("assets", create=False), compact=False, font_size=10)
        self.kofi_btn.place(relx=1.0, y=4, x=-10, anchor="ne")
        self.kofi_btn.lift()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_window_icon(self):
        """Set the HERMES window/taskbar icon. iconbitmap(.ico) is the native Windows path;
        fall back to a PNG via iconphoto. Missing art must never crash startup."""
        assets = paths.get_path("assets", create=False)
        try:
            self.root.iconbitmap(default=os.path.join(assets, "hermes.ico"))
            return
        except tk.TclError:
            pass
        try:
            img = tk.PhotoImage(file=os.path.join(assets, "hermes.png"))
            self.root.iconphoto(True, img)
            self.root._icon_img = img   # keep a ref so it isn't GC'd
        except tk.TclError:
            pass

    def _load_modules(self):
        """Lazy-import and add a tab for each enabled module, in registry order.

        A module that fails to import/instantiate is skipped with a warning rather than
        taking down the whole app (Prime Directive: never crash)."""
        for key in registry.enabled_modules(self.app.config):
            meta = registry.MODULE_REGISTRY[key]
            try:
                module_path, class_name = meta["class_path"].split(":")
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name)
                instance = cls(self.notebook, self.app)
                self.notebook.add(instance, text=meta["label"])
                self.tabs[key] = instance
            except Exception as ex:  # noqa: BLE001 - any failure must not crash the hub
                print(f"[hub] skipped module '{key}': {ex}")

    def _on_root_configure(self, event):
        """Track the last NON-maximized geometry (root's own events only - children's
        <Configure> bubbles up to the toplevel's bindings). The width guard skips
        pre-map transients smaller than the minsize."""
        if (event.widget is self.root and self.root.state() == "normal"
                and self.root.winfo_width() >= 720):
            self._last_normal_geo = self.root.geometry()

    def _on_tab_changed(self, _event=None):
        """Generic hub-side hook for the Phase 5.5.4 onboarding engine: if the newly
        selected tab's module implements maybe_show_tutorial(), call it. main.py never
        imports tutorial code or knows what a tutorial is, see TutorialIntro /
        TutorialWalkthrough in shared/ui_helpers.py."""
        try:
            widget = self.notebook.nametowidget(self.notebook.select())
        except (tk.TclError, KeyError):
            return
        hook = getattr(widget, "maybe_show_tutorial", None)
        if callable(hook):
            hook()

    def _handle_handoff(self, data_type, target, path):
        """Route a published artifact to a target module's tab. Installed on the bus.

        If the target tab is loaded, focus it and let it receive the path (if it exposes
        receive_handoff). If the target isn't enabled, prompt the user to enable it."""
        instance = self.tabs.get(target)
        if instance is None:
            messagebox.showinfo(
                "Module not enabled",
                f"'{registry.get_label(target)}' isn't enabled.\n"
                f"Enable it on the Home tab (then restart) to send this here.")
            return
        self.notebook.select(instance)
        receiver = getattr(instance, "receive_handoff", None)
        if callable(receiver):
            receiver(data_type, path)

    def _confirm_close(self):
        """Ask once before close/restart if any module reports in-flight work (module
        protocol: optional `has_unsaved() -> short reason string | None`). The hub only
        polls generically - each module owns its own answer. A hook that raises counts
        as "nothing unsaved": a broken module must never block shutdown."""
        busy = []
        for key, instance in self.tabs.items():
            hook = getattr(instance, "has_unsaved", None)
            if not callable(hook):
                continue
            try:
                reason = hook()
            except Exception as ex:  # noqa: BLE001
                print(f"[hub] has_unsaved() failed for '{key}': {ex}")
                continue
            if reason:
                busy.append(f"{registry.get_label(key)}: {reason}")
        if not busy:
            return True
        return messagebox.askyesno("Work in progress",
                                   "\n".join(busy) + "\n\nQuit anyway?")

    def _cleanup(self):
        """Persist config (incl. current window size/position) and release module
        resources. Shared by close and restart."""
        try:
            state = self.root.state()
            win = self.app.config.setdefault("window", {})
            win["zoomed"] = state == "zoomed"
            # Maximized or minimized at close: keep the last normal geometry so
            # un-maximizing (now or next session) returns to a real size.
            win["geometry"] = (self.root.geometry() if state == "normal"
                               else self._last_normal_geo)
        except tk.TclError as ex:
            print(f"[hub] could not read window geometry: {ex}")
        try:
            self.app.save_config()
        except Exception as ex:  # noqa: BLE001
            print(f"[hub] config save failed: {ex}")
        for key, instance in self.tabs.items():
            closer = getattr(instance, "on_close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as ex:  # noqa: BLE001
                    print(f"[hub] module '{key}' cleanup failed: {ex}")

    def _on_close(self):
        """Clean shutdown: persist config, release module resources, destroy the window."""
        if not self._confirm_close():
            return
        self._cleanup()
        self.root.destroy()

    def _restart(self):
        """Relaunch the app so module changes (enable/disable) take effect without the user
        manually restarting. Installed on the AppContext.

        We spawn a fresh process with subprocess.Popen (NOT os.execl): on Windows os.exec*
        is emulated and mangles argv entries containing spaces - e.g. this app's own path,
        '...\\Stream App\\main.py' - so the relaunch would silently fail and the app would
        just close. Popen's list form quotes arguments correctly.

        _cleanup() runs FIRST so the window geometry + config are persisted before the new
        process starts and reads them - that's what makes the relaunched window reappear at
        the same size/position instead of at the top-left."""
        if not self._confirm_close():   # a render dying on restart = same loss as on close
            return
        self._cleanup()
        # Frozen (.exe): argv[0] is already the exe. Source: prefix with the interpreter.
        if getattr(sys, "frozen", False):
            args = [sys.executable, *sys.argv[1:]]
        else:
            args = [sys.executable, *sys.argv]
        try:
            subprocess.Popen(args)
        except OSError as ex:  # noqa: BLE001 - if we can't relaunch, keep running
            print(f"[hub] restart failed to spawn a new process: {ex}")
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    StreamerCompanion(root)
    root.mainloop()


if __name__ == "__main__":
    main()
