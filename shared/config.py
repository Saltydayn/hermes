"""User preferences. The ONLY module that touches config.json.

load_config() reads config.json, deep-merges it over DEFAULTS, and returns a dict.
save_config(cfg) writes it back. Missing keys always fall back to DEFAULTS, so adding a
new default later doesn't break existing configs. Clean start - no migration from the old app.
"""

import json
import os
import shutil
import time

from shared import paths

CONFIG_PATH = os.path.join(paths.USER_DATA_DIR, "config.json")

# How the last load_config() went, for the hub to tell the user: "ok" (or no file
# yet), "recovered" (config.json was damaged, the backup from the last good launch
# was used), or "reset" (damaged and no usable backup - defaults). The damaged file
# is never overwritten; it is kept as config.corrupt-<time>.json.
LAST_LOAD_STATUS = "ok"
LAST_LOAD_FRESH = False   # True when there was no config.json yet (first start)
LAST_CORRUPT_PATH = ""

# Defaults. `enabled_modules` holds per-module on/off state for modules that aren't
# always_on (home is always on - see shared/registry.py). Modules not built yet default
# to disabled so the hub never tries to import a file that doesn't exist.
DEFAULTS = {
    "enabled_modules": {
        "importer": False,
        "exporter": False,
        "editor": False,
        "subtitle_studio": False,
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
    # Subtitle model selection (Phase 6.1). Maintained by the Downloads tab; read by
    # Subtitle Studio (6.2+). "model" = the id of the installed Whisper model to use,
    # "" = none installed. "device" is "auto" | "cpu" | "gpu"; the switch UI is 6.5.
    "subtitles": {
        "model": "",
        "device": "auto",
        "quality": "balanced",       # 6.2b: "fast" | "balanced" | "best"
        "language": "auto",          # 6.2b: "auto" or a Whisper language code
        "game_terms": False,         # 6.2b: prompt-seed toggle
        "game_terms_text": "",       # 6.2b: freeform words/names seed (e.g. character names)
        "second_language": "",       # 6.2b: "" = off, else a Whisper language code
        "second_language_weight": 50,  # 6.2b: 0-100, "a few terms" to "almost 50/50"
        "auto_transcribe": False,    # 6.2b: keep-in-sync toggle
        "flag_confidence": 0.6,      # 6.3a: shaky-word cutoff (0.4-0.95, Config slider)
        "vad": False,                # 6.3b: Silero VAD noise/silence filter
        "reduce_hallucinations": False,  # 6.3b: hallucination_silence_threshold on/off
        "snap_word_starts": False,   # 6.5 C2: bounded word-start snap to real audio onset
        "timing_offset_ms": 0,       # 6.3b: global karaoke offset, -500..+500, non-destructive
        "expert": {                  # 6.3b: Expert drawer; each defaults to faster-whisper's own
            "beam_override": 0,      # 0 = use the Quality->beam mapping, >0 = force this beam
            "no_speech_threshold": 0.6,
            "log_prob_threshold": -1.0,
            "compression_ratio_threshold": 2.4,
            # 6.5: off (faster-whisper's own default is on) - stops one bad segment's
            # text from biasing/cascading into the next one. See build_transcribe_kwargs.
            "condition_on_previous_text": False,
            "temperature": 0.0,      # 0 = the default fallback ladder; a fixed value pins it
        },
        # Active karaoke burn-in style (6.4a). Seeded from SubtitleStyle().to_dict() so a
        # fresh install renders the same default every future field addition tolerates
        # via SubtitleStyle.from_dict's unknown/missing-key leniency.
        "style": {
            "font": "Anton", "size": 72, "fill": "#FFFFFF",
            "outline_on": True, "outline_width": 10, "outline_color": "#141418",
            "highlight_on": True, "highlight_color": "#FFD166", "highlight_pad": 14,
            "highlight_radius": 16, "highlight_alpha": 235, "highlight_text": "",
            "shadow_on": False, "shadow_dx": 4, "shadow_dy": 5,
            "shadow_color": "#000000", "shadow_alpha": 160,
            "align": "center", "anchor_x": 0.5, "anchor_y": 0.72,
            "max_width": 0.90, "max_lines": 1,
            "reveal": "full_line", "motion": "none", "transition": "cut",
            "highlight_mode": "box", "highlight_image": "",
        },
        # Render pickup (6.4b): per-clip subtitles-on/off + a style SNAPSHOT (the
        # active style at the moment the user enabled subtitles for this clip, so a
        # later global style edit doesn't silently change an already-set-up clip).
        # Keyed by paths.clip_key(path); read-only from the editor's render loop,
        # written only by Subtitle Studio. {} = no clip has subtitles enabled yet.
        "per_clip": {},
        # Subtitle Studio's preview/transcript draggable divider (6.4b) - the
        # ttk.PanedWindow sash position in pixels from the pane's left edge. 0 = not
        # set yet (the built-in initial split is used).
        "preview_split": 0,
        # User-saved style slots (6.4c) - list of {"name", "style"}, mirrors the
        # editor's layout-slot pattern. "active_slot" is the name of the last-used
        # preset/slot, restored on open; "" = none picked yet (the plain default).
        "slots": [],
        "active_slot": "",
        # Optional plain-text SRT sidecar (6.4c), written next to the render
        # alongside the burn-in when the clip has subtitles enabled. Off by
        # default - burn-in is the primary feature, SRT is a bonus for platforms
        # that want a separate caption file.
        "srt_export": False,
    },
    # Downloads tab UI state (Phase 6.1).
    "downloads": {
        "models_expanded": False,
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
        "github":  "https://github.com/Saltydayn",
        "linkpage": "https://saltydayn.pages.dev",
        "linkpage_create": "https://saltydayn.pages.dev/create",
        "webtools": "https://saltydayn.github.io/web-tools/",
    },
    # Dev-hosted supporters JSON (Phase 4.3): the creator's Ko-fi sync web app. Fetched in the
    # background on launch; offline-safe (renders the seed/cache if unreachable). Empty would
    # skip the network. A Google Apps Script sits behind this URL.
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
            "exporter": False, "about": False, "downloads": False,
            "subtitle_studio": False,
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
    # How many clips keep their editor edits and their subtitles (oldest dropped first).
    "clip_memory": 200,
}

CLIP_MEMORY_CHOICES = (50, 200, 500, 1000)


def _deep_merge(base, override):
    """Recursively merge `override` onto a copy of `base`. Dicts merge key-by-key;
    every other value (including lists) is replaced wholesale.

    Every dict/list in the result is a FRESH object, never a reference into `base`
    (DEFAULTS) - even nested dicts/lists a caller's `override` never mentions still
    get copied, not aliased. Without this, an existing user's config.json missing a
    newly-added nested key (e.g. a sub-dict introduced in a later release) would
    make the merged config's value for that key literally BE the DEFAULTS object,
    so the first in-place mutation of it would silently corrupt DEFAULTS for the
    rest of the process - including what "reset to defaults" reads back."""
    out = {}
    for key, val in base.items():
        if key in override:
            ov = override[key]
            if isinstance(val, dict) and isinstance(ov, dict):
                out[key] = _deep_merge(val, ov)
            else:
                out[key] = ov
        elif isinstance(val, dict):
            out[key] = _deep_merge(val, {})
        elif isinstance(val, list):
            out[key] = list(val)
        else:
            out[key] = val
    for key, val in override.items():
        if key not in out:
            out[key] = val
    return out


def clip_memory(cfg):
    """The configured clip-memory size, snapped to the nearest offered choice so a
    hand-edited or corrupt value can never mean "keep nothing" or "keep everything"."""
    try:
        want = int((cfg or {}).get("clip_memory", DEFAULTS["clip_memory"]))
    except (TypeError, ValueError):
        want = DEFAULTS["clip_memory"]
    return min(CLIP_MEMORY_CHOICES, key=lambda c: abs(c - want))


def move_legacy_clip_folders(cfg, moves):
    """One-time settings move for installed builds whose clip folders moved
    (shared/paths.py). `moves` maps an old default folder to its new location.
    Only folders that are still the OLD app default are rewritten; a folder the user
    picked themselves is left alone. Returns the list of (old, new) actually moved."""
    def same(a, b):
        return bool(a) and os.path.normcase(os.path.normpath(a)) == \
            os.path.normcase(os.path.normpath(b))

    done = []
    imp = cfg.setdefault("importer", {})
    out = cfg.setdefault("output_dirs", {})
    for old, new in moves.items():
        if same(imp.get("default_folder", ""), old):
            imp["default_folder"] = new
            imp["detect_known_files"] = []
            done.append((old, new))
        if same(out.get("Shorts", ""), old):
            out["Shorts"] = new
            done.append((old, new))
    return done


def whats_new_due(cfg, version, fresh_install):
    """True once per version after an update: the last version that ran differs and
    this is not a brand-new install (those get the first-run tour instead)."""
    return not fresh_install and cfg.get("last_run_version") != version


def _backup_path():
    return CONFIG_PATH + ".bak"


def _read_json_dict(path):
    """The parsed dict at `path`, or None when missing, unreadable, or not a dict."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_config():
    """Return the merged config dict (DEFAULTS overlaid with config.json, if present).

    A good config.json is copied to config.json.bak (once per launch), so a file
    damaged later in the session - a crash or power loss mid-save - is recovered
    from that backup on the next launch instead of silently falling back to
    defaults and being overwritten. The app always boots."""
    global LAST_LOAD_STATUS, LAST_CORRUPT_PATH, LAST_LOAD_FRESH
    LAST_LOAD_STATUS, LAST_CORRUPT_PATH = "ok", ""
    LAST_LOAD_FRESH = not os.path.exists(CONFIG_PATH)
    if not os.path.exists(CONFIG_PATH):
        # A fresh install, or the user deleted the file on purpose to start over -
        # saves are atomic, so a crash never leaves config.json missing.
        return _deep_merge(DEFAULTS, {})
    user = _read_json_dict(CONFIG_PATH)
    if user is not None:
        try:
            shutil.copyfile(CONFIG_PATH, _backup_path())
        except OSError:
            pass
        return _deep_merge(DEFAULTS, user)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    LAST_CORRUPT_PATH = os.path.join(os.path.dirname(CONFIG_PATH),
                                     f"config.corrupt-{stamp}.json")
    try:
        os.replace(CONFIG_PATH, LAST_CORRUPT_PATH)
    except OSError:
        LAST_CORRUPT_PATH = ""
    backup = _read_json_dict(_backup_path())
    if backup is not None:
        LAST_LOAD_STATUS = "recovered"
        return _deep_merge(DEFAULTS, backup)
    LAST_LOAD_STATUS = "reset"
    return _deep_merge(DEFAULTS, {})


def save_config(cfg):
    """Write the config dict to config.json (pretty-printed, UTF-8), atomically.

    Serialized fully first (a value json cannot encode raises here, before the file
    is touched), written to config.json.tmp and flushed to disk, then swapped in with
    os.replace - config.json is always either the old or the new complete file,
    never a half-written one. The swap retries briefly: on Windows an antivirus
    scan or a reader can hold the file for a moment. Ensures the writable root
    exists first - an installed build's first save lands in a freshly-created
    %LOCALAPPDATA%\\HERMES that nothing has touched yet."""
    os.makedirs(paths.USER_DATA_DIR, exist_ok=True)
    text = json.dumps(cfg, indent=2)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(5):
        try:
            os.replace(tmp, CONFIG_PATH)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)
