"""Single source of truth for app branding + version (see NAMING.md).

Read by the PyInstaller build (exe metadata), the Inno installer (dist.3), and the
in-app About string (roadmap 2.9.6). Bump VERSION per release; it drives the exe metadata
and the installer's filename + AppVersion. VERSION stays plain numeric semver (the exe
version resource needs ints); release-stage labeling goes in VERSION_SUFFIX."""

APP_NAME  = "HERMES"                         # internal / installer filename / exe ProductName
DISPLAY_NAME = "H.E.R.M.E.S."                 # user-facing dotted brand (NAMING.md)
EXPANSION = "Highlight Editor & Rapid Media Export Suite"
VERSION   = "1.1.0"          # v1.1 "Athena": Subtitle Studio
VERSION_SUFFIX = ""          # release-stage label shown after the version; "" for stable
NAME_TAG  = ""                # shown next to the brand while the name is not final; "" to drop
PUBLISHER = "Saltydayn"      # used in exe metadata + installer
BYLINE    = f"by {PUBLISHER}"  # after the version in the window title, next to the Home brand

# Shown once after an update to this version (main.py). Short, user-facing, plain words.
WHATS_NEW = (
    "Subtitle Studio: HERMES listens to your clip and writes karaoke-style subtitles. "
    "Fix words like in a text file, pick a style, burn them in.",
    "Get a speech model in the Downloads tab first (Large Turbo is a good pick). That "
    "also turns the Subtitle Studio tab on. Got an NVIDIA card? Grab the GPU add-on "
    "there too, it makes transcribing much faster.",
    "Your import and export folders now live in Videos\\HERMES, so they are easy to find.",
    "The name plate shows umlauts and accents, settings can't get wiped by a crash, "
    "and the download is a lot smaller.",
)


def display_version():
    """Version string for user-facing surfaces, e.g. '0.9.0 BETA'."""
    return f"{VERSION} {VERSION_SUFFIX}".strip()


def window_title():
    """Full user-facing title, e.g. 'H.E.R.M.E.S. - Highlight Editor & Rapid Media Export Suite ( v1.1.0 ) by Saltydayn'.

    Hyphen separator (no em dash); the ampersand is part of the locked brand expansion."""
    return f"{DISPLAY_NAME} - {EXPANSION} ( v{display_version()} ) {BYLINE}"
