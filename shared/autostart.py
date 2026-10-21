"""Per-user Windows startup entry (HKCU Run). The in-app mirror of the installer's
launch-on-boot task (build/hermes.iss) - SAME value name + quoting, so the Home toggle and
the installer checkbox are one switch.

Safe everywhere: on a non-Windows interpreter (no winreg) or any registry error, both calls
degrade to a no-op and return False - never raise (Prime Directive #1)."""
import os
import sys

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE = "HERMES"          # MUST match build/hermes.iss [Registry] ValueName


def _command() -> str:
    """The quoted command Windows runs at login. Frozen: the exe itself (== what the
    installer wrote, so they target one value). Source: interpreter + main.py (dev
    convenience)."""
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}"'
    main = os.path.abspath(sys.argv[0])
    return f'"{os.path.abspath(sys.executable)}" "{main}"'


def is_launch_on_boot() -> bool:
    """True iff the HKCU Run "HERMES" value exists. Checks EXISTENCE only - never rewrites
    the value (an installed app stores the exe path; a dev session python+main.py; both
    count as 'on', and a read must not clobber the installer's path)."""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, _VALUE)
        return True
    except OSError:
        return False


def set_launch_on_boot(enabled: bool) -> bool:
    """Write (enabled) or delete (disabled) the HKCU Run "HERMES" value. Returns True on
    success, False on any failure. Deleting an already-absent value is success (idempotent)."""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            if enabled:
                winreg.SetValueEx(k, _VALUE, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(k, _VALUE)
                except FileNotFoundError:
                    pass        # already absent - fine
        return True
    except OSError:
        return False
