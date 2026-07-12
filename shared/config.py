"""User preferences. The ONLY module that touches config.json.

load_config() reads config.json, deep-merges it over DEFAULTS, and returns a dict.
save_config(cfg) writes it back. Missing keys always fall back to DEFAULTS, so adding a
new default later doesn't break existing configs. Clean start - no migration from the old app.
"""

import json
import os

from shared import paths

CONFIG_PATH = os.path.join(paths.USER_DATA_DIR, "config.json")

# Defaults. `enabled_modules` holds per-module on/off state for modules that aren't
# always_on (home is always on - see shared/registry.py). Modules not built yet default
# to disabled so the hub never tries to import a file that doesn't exist.
DEFAULTS = {
    "enabled_modules": {
        "importer": False,
        "exporter": False,
        "editor": False,
    },
    "output_dirs": {},        # optional per-type overrides, e.g. {"Shorts": "D:/Clips"}; empty = use data/ defaults
    "editor": {
        "last_layout": {},    # layout dict (boxes normalized 0..1, split, melt, watermark…); {} = built-in defaults
        "presets": [None, None, None, None, None],   # up to 5 saved layout dicts (each carries a "name")
        "clip_edits": {},     # per-clip edit blocks keyed by sha1(path)[:16]; LRU-capped (see clip_editor)
        "play_in_out_only": False,   # 2.9.3: restrict playback to the In/Out trim window
    },
    "importer": {
        "default_folder": "",        # explicit start dir; "" = assume shorts_import if it exists
        "recent_files": [],          # up to 5 absolute paths, most-recent first
        "detect_new_clips": False,   # master toggle for the new-clip scan
        "detect_known_files": [],    # basenames already accounted for by the scan (name-set, not mtime)
        "recent_collapsed": False,      # RECENT list chevron state
        "new_clips_collapsed": False,   # NEW CLIPS list chevron state
    },
    "exporter": {
        # Editable quick-share buttons (3.2). Order = display order. Clicking one opens its
        # url in the browser AND reveals the exported file in the file manager to drag in.
        "share_buttons": [
            {"label": "YouTube",   "url": "https://www.youtube.com/upload"},
            {"label": "TikTok",    "url": "https://www.tiktok.com/upload"},
            {"label": "Instagram", "url": "https://www.instagram.com/"},
        ],
        # Copy-to-clipboard text snippets (tags, a sub reminder, …). "" = empty slot.
        # Starts at five (5.5.2e); extendable in the Export tab, no maximum. Existing
        # configs keep their saved length (defaults-merge only fills a missing key).
        "copy_slots": ["", "", "", "", ""],
    },
    # Creator / support links (Phase 4.2). These ship with the app (the creator's own
    # channels); empty would render the button but no-op on click.
    "links": {
        "twitch":  "https://www.twitch.tv/saltydayn",
        "youtube": "https://www.youtube.com/@Saltydayn",
        "discord": "https://discord.gg/XhraUHSjSf",
        "x":       "https://x.com/saltydayn",
        "kofi":    "https://ko-fi.com/saltydayn",
    },
    # Dev-hosted supporters JSON (Phase 4.3): the creator's Ko-fi sync web app. Fetched in the
    # background on launch; offline-safe (renders the seed/cache if unreachable). Empty would
    # skip the network. See workspace/kofi-sync/ for the Apps Script behind this URL.
    "supporters_url": "https://script.google.com/macros/s/AKfycbz3UYmpV1HaSeScuHwKu73YMR6zDX2SRclocDGRHiqp6WpOKizDBkA260ZILTlKJi9o3Q/exec",
    # Feedback/bug-report POST target (Phase 5.5.5). Same kofi-sync Apps Script deployment as
    # supporters_url (its doPost now also accepts feedback). "" = fall back to supporters_url.
    "feedback_url": "",
    # Lifetime usage stats + one-shot nudges (Phase 4.4). full_exports counts COMPLETED
    # exports only (exporter final_file), never editor renders.
    "stats": {"full_exports": 0},
    "nudges": {"export_thankyou_shown": False, "editor_autosave_toast_shown": False},
    # Onboarding tutorials (Phase 5.5.4). "asked" gates the one-shot first-launch Y/N
    # prompt; "enabled" is meaningless until asked=True. "seen" tracks per-module intro
    # popups so each tab's tutorial fires once. Keys match shared/registry.py exactly.
    "tutorial": {
        "asked": False,
        "enabled": True,
        "seen": {
            "home": False, "importer": False, "editor": False,
            "exporter": False, "about": False,
        },
    },
    # Update check (Phase 5.3). update_url overrides the built-in source constant in
    # shared/update_check.py; "" = use the constant. Config-file only, no settings UI.
    "update_url": "",
    "update_check_on_launch": True,   # quiet startup check; toggled from Home
    "theme": "dark",
    "performance_mode": "eco",   # "eco" (serial render, normal priority - safe while gaming/streaming) | "performance"
    "hw_encode": "auto",         # "auto" (hw codec in Performance mode only) | "on" (both modes) | "off" (always libx264)
    "launch_on_boot": False,
    "window": {"geometry": ""},   # last user window size+position "WxH+X+Y"; "" = default
}


def _deep_merge(base, override):
    """Recursively merge `override` onto a copy of `base`. Dicts merge key-by-key;
    every other value (including lists) is replaced wholesale."""
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config():
    """Return the merged config dict (DEFAULTS overlaid with config.json, if present).

    A missing or corrupt file falls back to defaults rather than crashing - correctness
    first; the app must always boot."""
    if not os.path.exists(CONFIG_PATH):
        return _deep_merge(DEFAULTS, {})
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            return _deep_merge(DEFAULTS, {})
        return _deep_merge(DEFAULTS, user)
    except (json.JSONDecodeError, OSError):
        return _deep_merge(DEFAULTS, {})


def save_config(cfg):
    """Write the config dict to config.json (pretty-printed, UTF-8).

    Ensures the writable root exists first - an installed build's first save lands in a
    freshly-created %LOCALAPPDATA%\\HERMES that nothing has touched yet."""
    os.makedirs(paths.USER_DATA_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
