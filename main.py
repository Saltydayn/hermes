"""Streamer Companion - hub launcher.

Thin. Owns the root window, the tab notebook, the registry/bus/config wiring, and clean
shutdown. NO module-specific logic lives here - if feature code lands in this file, it's
misplaced. Modules are lazy-imported (disabled modules cost nothing at startup) and each
gets an AppContext for everything it needs.

Run: python main.py
"""

import importlib
import os
import shutil
import subprocess
import sys
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

from shared import applog
from shared import config as config_mod
from shared import paths, registry, relaunch, version
from shared.bus import MessageBus
from shared.ui_helpers import WhatsNewDialog, apply_dark_theme, make_kofi_button


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

        # Tk callback errors: to the log (shared/applog.py) as well as stderr.
        root.report_callback_exception = self._report_callback_exception
        self.app = AppContext(root)
        self.load_errors = []   # (module label, short error) for modules that failed to load
        self.app.bus.set_handoff_handler(self._handle_handoff)
        self.app.restart = self._restart   # Home's Restart button calls this
        self.app.apply_update = self._apply_update   # Home's self-update flow (9.3c)
        self._cleanup_stale_update_staging()
        self._folders_moved = self._move_legacy_clip_folders()
        self._whats_new_due = config_mod.whats_new_due(
            self.app.config, version.VERSION, config_mod.LAST_LOAD_FRESH)
        if self.app.config.get("last_run_version") != version.VERSION:
            self.app.config["last_run_version"] = version.VERSION
            self.app.save_config()

        # Restore the user's last window size + position (else a sane default). Set before
        # the window is mapped so it opens in place - incl. after a Restart, not top-left.
        # Maximized is restored as state, never as pixels: geometry() while zoomed reports
        # the zoomed size, and restoring THAT as a normal window loses the real size.
        saved_win = self.app.config.get("window") or {}
        saved_geo = saved_win.get("geometry")
        self._last_normal_geo = saved_geo or "1600x900"
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
        self._notice_job = root.after(700, self._startup_notices)

    def _move_legacy_clip_folders(self):
        """Installed builds up to 1.0.x kept the clip folders in AppData; they now live in
        Videos\\HERMES (shared/paths.py). Point settings that still name the old
        defaults at the new folders, once. Files already there are not moved."""
        if not paths.INSTALLED:
            return []
        moved = config_mod.move_legacy_clip_folders(self.app.config, {
            paths.LEGACY_IMPORT_DIR: paths.IMPORT_DIR,
            paths.LEGACY_EXPORT_DIR: paths.EXPORT_DIR,
            paths.get_path("Shorts", create=False): paths.EXPORT_DIR,
        })
        if moved:
            for _old, new in moved:
                try:
                    os.makedirs(new, exist_ok=True)
                except OSError as ex:
                    print(f"[hub] could not create {new}: {ex}")
            self.app.save_config()
        return moved

    def _show_whats_new(self):
        notes = list(version.WHATS_NEW)
        if self._folders_moved:
            olds = sorted({old for old, _new in self._folders_moved})
            notes.append("Clips you saved before are still in: " + ", ".join(olds))
        downloads = self.tabs.get("downloads")
        WhatsNewDialog(
            self.root, title=f"HERMES is now v{version.display_version()}",
            items=notes,
            footer=("Found a bug or have an idea? The About tab has Report a bug and "
                    "Send feedback."),
            action_label="Open Downloads" if downloads is not None else None,
            on_action=(lambda: self.notebook.select(downloads)) if downloads is not None
            else None)

    def _report_callback_exception(self, exc_type, exc, tb):
        applog.log_exception(exc_type, exc, tb, "a Tk callback")
        traceback.print_exception(exc_type, exc, tb)

    def _startup_notices(self):
        self._notice_job = None
        self._show_startup_notices()

    def _show_startup_notices(self):
        """One-time messages about problems found while starting: a damaged settings
        file (shared/config.py recovers from its backup) and modules that failed to
        load. Generic hub work - no module knowledge."""
        log = applog.log_path()
        where = f"\n\nDetails are in {log}." if log else ""
        status = config_mod.LAST_LOAD_STATUS
        if status in ("recovered", "reset"):
            kept = (f"\n\nThe damaged file was kept as "
                    f"{os.path.basename(config_mod.LAST_CORRUPT_PATH)}."
                    if config_mod.LAST_CORRUPT_PATH else "")
            body = ("Your settings file was damaged (for example by a crash while "
                    "saving). HERMES restored the copy from your last start."
                    if status == "recovered" else
                    "Your settings file was damaged and no backup was available, so "
                    "HERMES started with default settings.")
            messagebox.showwarning("Settings restored", body + kept)
        if self._whats_new_due:
            self._show_whats_new()
        if self.load_errors:
            lines = "\n".join(f"- {label}: {err}" for label, err in self.load_errors)
            messagebox.showerror(
                "Some tabs could not load",
                f"These parts of HERMES failed to start and are hidden:\n\n{lines}"
                f"{where}\n\nPlease report this in the About tab or on Discord.")

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
                applog.log_exception(type(ex), ex, ex.__traceback__, f"loading '{key}'")
                self.load_errors.append((meta["label"], f"{type(ex).__name__}: {ex}"))

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
        if self._notice_job is not None:
            try:
                self.root.after_cancel(self._notice_job)
            except tk.TclError:
                pass
            self._notice_job = None
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
        except OSError as ex:  # noqa: BLE001
            # _cleanup() already closed every module (captures released, pumps
            # stopped), so carrying on would leave a half-dead window. Say so and
            # close; the user's work is already saved.
            print(f"[hub] restart failed to spawn a new process: {ex}")
            messagebox.showerror("Restart failed",
                                 f"HERMES could not restart itself ({ex}). It will close "
                                 "now - please start it again. Your work is saved.")
        self.root.destroy()

    def _apply_update(self, staged_dir, remove_file):
        """Quit and hand off to a generated batch script that swaps a downloaded,
        verified update into place and relaunches HERMES (9.3c). Installed on
        AppContext next to restart so Home can call it once a self-update has
        finished downloading."""
        if not self._confirm_close():   # same in-flight-render guard as close/restart
            return
        self._cleanup()
        ok, error = relaunch.write_and_spawn(
            staged_dir=staged_dir, install_dir=paths.install_dir(),
            exe_path=sys.executable, remove_file=remove_file,
            staging_root=paths.update_staging_dir(create=False))
        if not ok:
            # Modules are already closed by _cleanup(); close rather than keep a
            # half-dead window. Nothing was changed on disk.
            print(f"[hub] update apply failed to start: {error}")
            messagebox.showerror("Update failed",
                                 f"Could not start the update process: {error}\n\n"
                                 "HERMES will close now. Start it again to keep using "
                                 "the current version.")
        self.root.destroy()

    def _cleanup_stale_update_staging(self):
        """Startup safety net (9.3c): remove any leftover update-staging folder from
        a batch whose own cleanup step failed to run, and surface a one-time notice
        if that batch left a "did not complete" marker (shared/relaunch.py)."""
        staging = paths.update_staging_dir(create=False)
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        marker = relaunch.marker_path(staging)
        if os.path.exists(marker):
            try:
                os.remove(marker)
            except OSError as ex:
                print(f"[hub] could not remove the update marker: {ex}")
            self.root.after(1500, lambda: messagebox.showwarning(
                "Update did not complete",
                "The last update did not finish applying, so HERMES kept running "
                "on the previous version. Try Check now again from Home."))


def main():
    applog.install(paths.USER_DATA_DIR)
    applog.info(f"{version.APP_NAME} {version.display_version()} starting "
                f"({'installed' if getattr(sys, 'frozen', False) else 'from source'})")
    root = tk.Tk()
    StreamerCompanion(root)
    root.mainloop()


if __name__ == "__main__":
    main()
