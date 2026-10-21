"""Module registry: the static map of every module that can exist.

This is cheap metadata (strings only) - always fully loaded regardless of which modules
are enabled. It lets any module ask "is the editor enabled? who produces raw_clip?"
WITHOUT importing another module, keeping modules decoupled but workflow-aware.

Per-module runtime on/off state lives in config (shared/config.py), not here. The helpers
that need it take a `config` dict argument.

`class_path` is "module.path:ClassName" - the hub lazy-imports it only when enabling/loading.
Dict insertion order defines tab order (Home first), so keep it intentional.
"""

MODULE_REGISTRY = {
    "home": {
        "label": "Home",
        "class_path": "modules.home:HomeModule",
        "always_on": True,
        "produces": [],
        "consumes": [],
    },
    "importer": {
        "label": "Import",
        "class_path": "modules.importer:ImporterModule",
        "always_on": False,
        "produces": ["raw_clip"],
        "consumes": [],
        "needs_ffmpeg": True,
    },
    "editor": {
        "label": "Clip Editor",
        "class_path": "modules.clip_editor:EditorModule",
        "always_on": False,
        "produces": ["edited_clip"],
        "consumes": ["raw_clip"],
        "needs_ffmpeg": True,
    },
    "subtitle_studio": {
        "label": "Subtitle Studio",
        "class_path": "modules.subtitle_studio:SubtitleStudioModule",
        "always_on": False,
        "produces": ["subtitled_clip"],   # actually published in 6.4 (burn-in); declared now
        "consumes": ["raw_clip", "edited_clip"],
        "needs_ffmpeg": True,
    },
    "exporter": {
        "label": "Export",
        "class_path": "modules.exporter:ExporterModule",
        "always_on": False,
        "produces": ["final_file"],
        "consumes": ["edited_clip", "subtitled_clip"],
        "needs_ffmpeg": True,
    },
    "downloads": {
        "label": "Downloads",
        "class_path": "modules.downloads:DownloadsModule",
        "always_on": True,
        "produces": [],
        "consumes": [],
    },
    "about": {
        "label": "About",
        "class_path": "modules.about:AboutModule",
        "always_on": True,
        "produces": [],
        "consumes": [],
    },
}


# Import, Clip Editor, and Export are one workflow end to end - Home chain-enables
# the whole trio when any one of them is turned on (2026-07-19 beta request).
CORE_PIPELINE = ("importer", "editor", "exporter")


def is_enabled(key, config):
    """True if the module is always-on or marked enabled in config."""
    meta = MODULE_REGISTRY.get(key)
    if not meta:
        return False
    if meta.get("always_on"):
        return True
    return bool(config.get("enabled_modules", {}).get(key, False))


def enabled_modules(config):
    """Ordered list of enabled module keys (registry order = tab order)."""
    return [key for key in MODULE_REGISTRY if is_enabled(key, config)]


def get_label(key):
    """Human-readable tab label for a module key (falls back to the key)."""
    meta = MODULE_REGISTRY.get(key)
    return meta["label"] if meta else key


def who_produces(data_type):
    """Module keys that produce the given data type (e.g. 'raw_clip')."""
    return [k for k, m in MODULE_REGISTRY.items() if data_type in m.get("produces", [])]


def who_consumes(data_type):
    """Module keys that consume the given data type."""
    return [k for k, m in MODULE_REGISTRY.items() if data_type in m.get("consumes", [])]


def needs_ffmpeg(key):
    """True if the module declares needs_ffmpeg in MODULE_REGISTRY."""
    meta = MODULE_REGISTRY.get(key)
    return bool(meta and meta.get("needs_ffmpeg"))
