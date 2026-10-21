"""A small log file for the shipped app.

The release build has no console, so print() output and every traceback from a Tk
callback or a worker thread used to vanish. install() writes them to hermes.log in
the user-data folder (rotated at 1 MB, two old files kept), so a bug report can
include what actually happened. Run from source, output still reaches the console
as before and is copied into the log as well.

Stdlib only, no tkinter import here: the hub passes its root so Tk callback errors
are routed through log_exception.
"""

import logging
import logging.handlers
import os
import sys
import threading

LOG_NAME = "hermes.log"
_logger = logging.getLogger("hermes")
_installed_path = None


class _StreamToLog:
    """A file-like stand-in for sys.stdout/sys.stderr: each complete line goes to the
    log, and to the original stream too when one exists (a dev console)."""

    def __init__(self, level, original):
        self._level = level
        self._original = original
        self._buf = ""

    def write(self, text):
        if self._original is not None:
            try:
                self._original.write(text)
            except Exception:  # noqa: BLE001 - a broken console must not break logging
                pass
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                _logger.log(self._level, line.rstrip())
        return len(text)

    def flush(self):
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self):
        return False


def log_exception(exc_type, exc, tb, where=""):
    """Write one uncaught exception (with traceback) to the log."""
    _logger.error("uncaught exception%s", f" in {where}" if where else "",
                  exc_info=(exc_type, exc, tb))


def install(log_dir):
    """Start logging to <log_dir>/hermes.log and catch uncaught exceptions from the
    main thread and worker threads. Idempotent; never raises - logging is a
    diagnostic, the app must start without it. Returns the log path or None."""
    global _installed_path
    if _installed_path is not None:
        return _installed_path
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, LOG_NAME)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    sys.stdout = _StreamToLog(logging.INFO, sys.stdout)
    sys.stderr = _StreamToLog(logging.ERROR, sys.stderr)

    def _hook(exc_type, exc, tb):
        log_exception(exc_type, exc, tb, "main thread")

    def _thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        log_exception(args.exc_type, args.exc_value, args.exc_traceback,
                      f"thread {name}")

    sys.excepthook = _hook
    threading.excepthook = _thread_hook
    _installed_path = path
    return path


def info(message):
    _logger.info(message)


def log_path():
    """The active log file path, or None before install()."""
    return _installed_path
