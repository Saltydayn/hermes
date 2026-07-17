"""Apply-update batch generation and spawn (Phase 9.3c self-updater).

HERMES cannot overwrite its own running exe or _internal/ files, so applying a
downloaded update means: quit, let an external script swap the files in once the
process has actually exited, then relaunch. build_batch_text() is a pure string
builder (no filesystem, no subprocess) so it is unit-testable on its own;
write_and_spawn() is the only function here that touches disk or a process.

Headless, stdlib only, no tkinter. Never raises from write_and_spawn() - callers
get back (ok, error) and stay running on failure.
"""

import os
import subprocess

MARKER_NAME = "hermes_update_failed.txt"

# CREATE_NO_WINDOW allocates a real console for the child, just never shows its
# window - console tools the batch itself invokes (tasklist, find, robocopy) need
# that to exist. DETACHED_PROCESS ("no console at all") looked like the more
# thorough "run fully invisibly" flag, but combining it here deadlocks the
# WAITLOOP's `tasklist | find` pipe forever (found and fixed during manual
# verification - the app would quit and just never come back). Do not add it back.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def marker_path(staging_root):
    """Where the generated batch drops its "update did not complete" marker: beside
    staging_root, not inside it. staging_root itself gets deleted on both the
    success and the failure path, and its parent (USER_DATA_DIR, where config.json
    already lives) is always writable, so the marker survives either way for the
    next launch to find and report."""
    return os.path.join(os.path.dirname(os.path.normpath(staging_root)), MARKER_NAME)


def build_batch_text(staged_dir, install_dir, exe_path, remove_file, staging_root):
    """Return the apply-update .bat text. Pure - takes no filesystem action itself.

    1. Wait for HERMES.exe to fully exit (poll by image name; bounded so it can
       never hang forever - proceeds anyway after the wait cap).
    2. robocopy staged_dir -> install_dir, excluding manifest.json, with lock
       retries. Exit code 8+ means a real failure (per robocopy's own convention;
       0-7 are all success variants).
    3. On failure: skip the manifest copy and the removals entirely, so the
       installed manifest.json still matches the files actually on disk and the
       next launch's check simply retries. Drop a marker so the app can tell the
       user the update did not complete.
    4. On success: delete each path listed in remove_file, then copy the staged
       manifest.json over install_dir's LAST - the atomic "now I am the new
       version" step.
    5. Relaunch the exe, best-effort remove staging_root, then self-delete.
    """
    marker = marker_path(staging_root)
    return f"""@echo off
setlocal enabledelayedexpansion

:WAITLOOP
set /a WAITCOUNT+=1
"%SystemRoot%\\System32\\tasklist.exe" /fi "imagename eq HERMES.exe" 2>nul | "%SystemRoot%\\System32\\find.exe" /i "HERMES.exe" >nul
if not errorlevel 1 (
    if !WAITCOUNT! GEQ 40 goto WAITDONE
    ping -n 2 127.0.0.1 >nul
    goto WAITLOOP
)
:WAITDONE

robocopy "{staged_dir}" "{install_dir}" /E /XF "manifest.json" /R:5 /W:1
if !errorlevel! GEQ 8 goto APPLY_FAILED

if exist "{remove_file}" (
    for /f "usebackq delims=" %%F in ("{remove_file}") do (
        if exist "{install_dir}\\%%F" del /f /q "{install_dir}\\%%F"
    )
)
copy /y "{staged_dir}\\manifest.json" "{install_dir}\\manifest.json" >nul
goto RELAUNCH

:APPLY_FAILED
echo update did not complete, will retry next launch > "{marker}"

:RELAUNCH
start "" "{exe_path}"
rd /s /q "{staging_root}" 2>nul
del "%~f0"
"""


def write_and_spawn(staged_dir, install_dir, exe_path, remove_file, staging_root):
    """Write apply_update.bat OUTSIDE staging_root (so its own cleanup step can
    remove staging_root cleanly) and launch it windowless in its own process
    group, so it survives this process exiting. Returns (ok, error); never
    raises."""
    batch_text = build_batch_text(staged_dir, install_dir, exe_path, remove_file,
                                  staging_root)
    batch_path = os.path.join(os.path.dirname(os.path.normpath(staging_root)),
                              "apply_update.bat")
    try:
        with open(batch_path, "w", encoding="utf-8") as f:
            f.write(batch_text)
    except OSError as exc:
        return False, f"could not write the update script: {exc}"
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", batch_path],
            creationflags=_CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
            close_fds=True)
    except OSError as exc:
        return False, f"could not launch the update script: {exc}"
    return True, ""
