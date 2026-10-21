"""Portable path resolution for Streamer Companion / HERMES.

Two roots, not one:

- BASE_DIR - the READ-ONLY bundle dir, where shipped assets/ live. Run-from-source it is
  the dir of main.py (today's value, unchanged). Frozen (PyInstaller) it is the bundled-data
  root sys._MEIPASS (onedir -> <app>/_internal; onefile -> the temp extract dir). We anchor
  assets to _MEIPASS, NEVER dirname(sys.executable) - under PyInstaller 6 onedir the exe sits
  beside _internal/, but datas land INSIDE _internal/.
- USER_DATA_DIR - the WRITABLE root, holding config.json, data/, models/, the exporter's
  shorts_export, and the importer's shorts_import. Resolution:
    1. not frozen (dev): USER_DATA_DIR == BASE_DIR - byte-identical to today, no
       %LOCALAPPDATA% pollution, your existing config.json + data/ stay beside main.py.
    2. frozen + portable.txt beside the exe: USER_DATA_DIR == the exe dir (zip-and-go build).
    3. frozen + no marker (installed): USER_DATA_DIR == %LOCALAPPDATA%\\HERMES
       (fallback ~/AppData/Local/HERMES if the env var is missing).

assets/ always resolves under BASE_DIR; everything writable under USER_DATA_DIR. The public
get_path()/unique_path()/ensure_data_dirs() API is unchanged - modules keep calling get_path()
and never learn the layout split.
"""

import hashlib
import os
import sys

_PORTABLE_MARKER = "portable.txt"   # ships only in the portable zip; the installer omits it


def _bundle_dir():
    """Read-only data root: where bundled assets live."""
    if getattr(sys, "frozen", False):
        # PyInstaller's bundled-data root. onedir -> <app>/_internal; onefile -> temp extract.
        # NEVER dirname(sys.executable): datas live in _internal/, not beside the exe.
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    # run-from-source: <root> = dir of main.py = two levels up from this file.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _exe_dir():
    """The real on-disk dir holding the exe (NOT _MEIPASS). Used for the portable marker
    check and the portable data location."""
    return os.path.dirname(os.path.abspath(sys.executable))


def _user_data_dir(bundle):
    """Writable root. Dev == bundle (unchanged behavior); frozen portable == beside exe;
    frozen installed == %LOCALAPPDATA%\\HERMES."""
    if not getattr(sys, "frozen", False):
        return bundle                                   # dev: identical to today
    exe_dir = _exe_dir()
    if os.path.exists(os.path.join(exe_dir, _PORTABLE_MARKER)):
        return exe_dir                                  # portable build (marker beside exe)
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local, "HERMES")                # installed build


def _videos_dir():
    """The user's Videos folder (it can be moved, e.g. into OneDrive), falling back to
    ~/Videos. Windows only asks the shell; elsewhere ~/Videos."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _GUID(ctypes.Structure):
                _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                            ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

            # FOLDERID_Videos {18989B1D-99B5-455B-841C-AB7C74E4DDFC}
            guid = _GUID(0x18989B1D, 0x99B5, 0x455B,
                         (ctypes.c_ubyte * 8)(0x84, 0x1C, 0xAB, 0x7C, 0x74, 0xE4, 0xDD, 0xFC))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
                path = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                if path:
                    return path
        except Exception:   # noqa: BLE001 - any shell failure falls back to the plain path
            pass
    return os.path.join(os.path.expanduser("~"), "Videos")


def _is_installed():
    """Frozen and not portable: the normal installer build."""
    return (getattr(sys, "frozen", False)
            and not os.path.exists(os.path.join(_exe_dir(), _PORTABLE_MARKER)))


# BASE_DIR = read-only bundle; USER_DATA_DIR = writable root. Resolved once at import time.
BASE_DIR = _bundle_dir()
USER_DATA_DIR = _user_data_dir(BASE_DIR)
INSTALLED = _is_installed()

DATA_DIR = os.path.join(USER_DATA_DIR, "data")
MODELS_DIR = os.path.join(USER_DATA_DIR, "models")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")           # assets stay in the read-only bundle
# The installed app keeps clips where people look for videos (Videos\HERMES); dev and
# portable runs keep them beside the app as before. LEGACY_* is where installed builds up
# to 1.0.x put them, for the one-time settings move in main.py.
LEGACY_EXPORT_DIR = os.path.join(USER_DATA_DIR, "shorts_export")
LEGACY_IMPORT_DIR = os.path.join(USER_DATA_DIR, "shorts_import")
if INSTALLED:
    _clips_root = os.path.join(_videos_dir(), "HERMES")
    EXPORT_DIR = os.path.join(_clips_root, "Export")
    IMPORT_DIR = os.path.join(_clips_root, "Import")
else:
    EXPORT_DIR, IMPORT_DIR = LEGACY_EXPORT_DIR, LEGACY_IMPORT_DIR
BIN_DIR = os.path.join(USER_DATA_DIR, "bin")            # on-demand downloads (e.g. ffmpeg.exe)
CUDA_DIR = os.path.join(USER_DATA_DIR, "cuda")           # on-demand NVIDIA GPU runtime DLLs
TRANSCRIPTS_DIR = os.path.join(USER_DATA_DIR, "transcripts")  # per-clip subtitle transcripts (6.2a)
FONTS_BUNDLED_DIR = os.path.join(ASSETS_DIR, "fonts")     # shipped OFL faces (6.4a), read-only
FONTS_USER_DIR = os.path.join(USER_DATA_DIR, "fonts")     # user-imported .ttf/.otf faces (6.4a)

# Standard data subdirectories (created on demand).
DATA_SUBDIRS = ("Downloads", "Shorts", "Thumbnails", "Subtitles", "Emotes")

# Named top-level locations that aren't data subdirs.
_SPECIAL = {
    "base": BASE_DIR,
    "assets": ASSETS_DIR,
    "userdata": USER_DATA_DIR,
    "data": DATA_DIR,
    "models": MODELS_DIR,
    "export": EXPORT_DIR,
    "import": IMPORT_DIR,
    "bin": BIN_DIR,
    "cuda": CUDA_DIR,
    "transcripts": TRANSCRIPTS_DIR,
    "fonts_bundled": FONTS_BUNDLED_DIR,
    "fonts_user": FONTS_USER_DIR,
}

# Specials we ensure exist on request. "base"/"assets" are read-only bundle dirs - never
# auto-created (assets ships with the app; base must already exist). "fonts_bundled" is
# likewise read-only (ships under assets/) and never auto-created.
_AUTO_CREATE = ("data", "models", "userdata", "export", "import", "bin", "cuda", "transcripts",
                "fonts_user")


def get_path(name, create=True):
    """Return an absolute path for a known location, creating it if missing.

    `name` may be a special location ("base", "assets", "userdata", "data", "models",
    "export") or one of the data subdir names ("Downloads", "Shorts", ...). Lookup is
    case-insensitive for the data subdirs. Unknown names are treated as a new subdir under
    data/.

    "base" and "assets" are never auto-created here (they live in the read-only bundle).
    Everything else is ensured under the writable root before returning.
    """
    key = name.strip()
    lower = key.lower()

    if lower in _SPECIAL:
        path = _SPECIAL[lower]
        if create and lower in _AUTO_CREATE:
            os.makedirs(path, exist_ok=True)
        return path

    # Match a standard data subdir case-insensitively, else treat as a custom subdir.
    match = next((d for d in DATA_SUBDIRS if d.lower() == lower), key)
    path = os.path.join(DATA_DIR, match)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def default_export_dir():
    """Where an export goes when no folder was chosen: Videos\\HERMES\\Export in the
    installed app (created on demand), data/Shorts when run from source or portable."""
    return get_path("export") if INSTALLED else get_path("Shorts")


def default_download_dir():
    """Where a URL import downloads to when no default import folder is set."""
    return get_path("import") if INSTALLED else get_path("Downloads")


def install_dir():
    """The on-disk folder holding the exe + _internal - what the self-updater
    (9.3c) patches. Frozen -> _exe_dir(); run-from-source -> BASE_DIR (dev has no
    shipped manifest.json, so the updater treats itself as unavailable there)."""
    if getattr(sys, "frozen", False):
        return _exe_dir()
    return BASE_DIR


def update_staging_dir(create=True):
    """USER_DATA_DIR/_update - the writable scratch area for a pending self-update
    (9.3c). Kept separate from install_dir so a half-finished download never
    pollutes the live tree. create=False lets a caller check for a leftover dir
    without recreating it."""
    path = os.path.join(USER_DATA_DIR, "_update")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def unique_path(path):
    """`path` if nothing sits there, else the first free `stem_2.ext`, `stem_3.ext`, ….
    The same-second collision guard for timestamped output names (editor render,
    exporter copy - shared here per the no-duplication rule)."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem}_{n}{ext}"):
        n += 1
    return f"{stem}_{n}{ext}"


def fonts_bundled_dir():
    """The read-only dir of shipped OFL subtitle fonts (assets/fonts). Never created -
    ships with the app."""
    return get_path("fonts_bundled", create=False)


def fonts_user_dir():
    """The writable dir for user-imported subtitle fonts (.ttf/.otf). Created on
    first use - these are the user's own files, never bundled or pushed to the repo."""
    return get_path("fonts_user")


def clip_key(path):
    """Stable per-file key: sha1 of the case-normalized absolute path, first 16 hex.

    The one implementation: the editor's _clip_key delegates here, so the editor and
    Subtitle Studio always derive the same key for the same clip."""
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode()).hexdigest()[:16]


def ensure_data_dirs():
    """Create the writable data tree (data/ + standard subdirs + models/). Called once at
    startup. Roots at USER_DATA_DIR, so an installed build builds it under %LOCALAPPDATA%."""
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    for sub in DATA_SUBDIRS:
        os.makedirs(os.path.join(DATA_DIR, sub), exist_ok=True)
    os.makedirs(MODELS_DIR, exist_ok=True)
