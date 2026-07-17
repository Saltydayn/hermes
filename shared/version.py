"""Single source of truth for app branding + version (see NAMING.md).

Read by the PyInstaller build (exe metadata), the Inno installer (dist.3), and the
in-app About string (roadmap 2.9.6). Bump VERSION per release; it drives the exe metadata
and the installer's filename + AppVersion. VERSION stays plain numeric semver (the exe
version resource needs ints); release-stage labeling goes in VERSION_SUFFIX."""

APP_NAME  = "HERMES"                         # internal / installer filename / exe ProductName
DISPLAY_NAME = "H.E.R.M.E.S."                 # user-facing dotted brand (NAMING.md)
EXPANSION = "Highlight Editor & Rapid Media Export Suite"
VERSION   = "1.0.2"          # small fix release: installer/asset bundling
VERSION_SUFFIX = ""          # release-stage label shown after the version; "" for stable
NAME_TAG  = ""                # shown next to the brand while the name is not final; "" to drop
PUBLISHER = "Saltydayn"      # used in exe metadata + installer


def display_version():
    """Version string for user-facing surfaces, e.g. '0.9.0 BETA'."""
    return f"{VERSION} {VERSION_SUFFIX}".strip()


def window_title():
    """Full user-facing title, e.g. 'H.E.R.M.E.S. - Highlight Editor & Rapid Media Export Suite ( v0.9.0 BETA )'.

    Hyphen separator (no em dash); the ampersand is part of the locked brand expansion."""
    return f"{DISPLAY_NAME} - {EXPANSION} ( v{display_version()} )"
