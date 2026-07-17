"""Update check data layer (Phase 5.3, extended Phase 9.3c). Headless and tkinter-free.

Fetches a small dev-hosted version.json, compares its "latest" against the running
VERSION, and reports one of four outcomes: newer, current, ahead, error. check() is the
single entry point and never raises; a failed or offline check simply returns "error".

Phase 9.3c turns the old notify-and-open-browser flow into a real in-app updater when
the hosted feed also carries a manifest_url: fetch_manifest/load_local_manifest/
diff_manifests/blob_url/can_self_update are the headless pieces of that path. A client
without a manifest_url (or with no local manifest.json - a dev run or a pre-9.3c build)
falls back to the original notify + open download_url behavior automatically, since
can_self_update() covers that check. Unknown keys in version.json are ignored, letting
the hosted file grow fields without breaking older clients.

Stdlib only (plus shared.version and shared.paths, both read-only / tkinter-free).
Only fetch() and fetch_manifest() raise; their callers run them on a worker thread and
catch.
"""

import json
import os
import urllib.request

from shared import paths, version

# The built-in update source. Set before release (see for-you/DISTRIBUTION.md);
# "" means checking is unconfigured. Overridable via the "update_url" config key.
DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/Saltydayn/hermes/main/version.json"

_NOTES_MAX = 200
_TITLE_MAX = 120


def effective_url(config):
    """The update source to use: the config "update_url" override if set, else the
    built-in constant. "" means checking is unconfigured."""
    url = config.get("update_url") if isinstance(config, dict) else ""
    if isinstance(url, str) and url.strip():
        return url.strip()
    return DEFAULT_UPDATE_URL


def parse_version(s):
    """"1.2.0" -> (1, 2, 0). Strips whitespace and one leading v/V; accepts 1 to 3
    numeric dot-parts, zero-padded to length 3. Anything else -> None. Never raises."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    parts = s.split(".")
    if not 1 <= len(parts) <= 3:
        return None
    out = []
    for p in parts:
        # isascii guards int() against exotic unicode digits isdigit() accepts.
        if not (p.isascii() and p.isdigit()):
            return None
        out.append(int(p))
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def compare_versions(a, b):
    """Compare two 3-tuples from parse_version(): -1 if a < b, 0 if equal, 1 if a > b."""
    return (a > b) - (a < b)


def _valid_http_url(value):
    return isinstance(value, str) and (value.startswith("http://")
                                       or value.startswith("https://"))


def parse_info(raw):
    """Validate a decoded version.json object. Returns {"latest", "download_url",
    "notes", "title", "manifest_url"} with the optional fields coerced to "" when
    missing or invalid (download_url/manifest_url must be http(s); notes clamped to
    200 chars, title to 120). Returns None if raw is not a dict or "latest" is
    missing/unparsable. Unknown keys are ignored so future fields never break this
    client. Never raises."""
    if not isinstance(raw, dict):
        return None
    latest = raw.get("latest")
    if parse_version(latest) is None:
        return None
    url = raw.get("download_url")
    if not _valid_http_url(url):
        url = ""
    notes = raw.get("notes")
    if not isinstance(notes, str):
        notes = ""
    title = raw.get("title")
    if not isinstance(title, str):
        title = ""
    manifest_url = raw.get("manifest_url")
    if not _valid_http_url(manifest_url):
        manifest_url = ""
    return {"latest": latest.strip(), "download_url": url, "notes": notes[:_NOTES_MAX],
            "title": title[:_TITLE_MAX], "manifest_url": manifest_url}


def fetch(url, timeout=4):
    """Blocking GET + JSON decode of the hosted version file. RAISES on any failure
    (network, HTTP, decode). Do not call with ""; the caller runs this on a worker
    thread and catches."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def check(url, current=version.VERSION, timeout=4):
    """The one entry point for consumers (Home). NEVER raises. Returns {"status":
    "newer"|"current"|"ahead"|"error", "latest": str, "download_url": str,
    "notes": str, "title": str, "manifest_url": str}. VERSION_SUFFIX plays no part
    in the compare."""
    result = {"status": "error", "latest": "", "download_url": "", "notes": "",
              "title": "", "manifest_url": ""}
    cur = parse_version(current)
    if cur is None:
        return result
    try:
        info = parse_info(fetch(url, timeout=timeout))
    except Exception:
        return result
    if info is None:
        return result
    result.update(info)
    cmp = compare_versions(parse_version(info["latest"]), cur)
    result["status"] = "newer" if cmp > 0 else ("current" if cmp == 0 else "ahead")
    return result


def fetch_manifest(url, timeout=8):
    """Blocking GET + JSON decode of a hosted release manifest.json. RAISES on any
    failure (network, HTTP, decode) - the caller runs this on a worker thread and
    catches, mirroring fetch()."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def load_local_manifest():
    """Read the installed manifest.json from paths.install_dir(). Returns the parsed
    dict, or None if it is absent, unreadable, or malformed - a dev run, a build
    that predates 9.3c, or a first-ever release never has one, and callers treat
    that as "self-update unavailable" rather than an error."""
    path = os.path.join(paths.install_dir(), "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    return data


def diff_manifests(local, remote):
    """Compare two manifest.json dicts ({"version":.., "files": {relpath:
    {"sha256":.., "size":..}}}) and return the apply plan:

      {"download": [(sha256, size, [relpaths...]), ...],   # grouped by unique sha
       "remove":   [relpath, ...]}                          # in local, absent from remote

    A relpath present in both with an equal sha is unchanged and appears in
    neither list - that is the delta this whole feature exists for. Two remote
    relpaths sharing one sha appear as a single download entry naming both
    (content-addressed dedup - the blob only needs fetching once). local may be
    None (a fresh install with nothing installed yet - everything downloads,
    nothing is removed)."""
    local_files = (local or {}).get("files") or {}
    remote_files = remote.get("files") or {}

    groups = {}
    sizes = {}
    for relpath, info in remote_files.items():
        sha = info.get("sha256")
        local_info = local_files.get(relpath)
        if local_info is not None and local_info.get("sha256") == sha:
            continue
        groups.setdefault(sha, []).append(relpath)
        sizes[sha] = info.get("size", 0)

    download = [(sha, sizes[sha], relpaths) for sha, relpaths in groups.items()]
    remove = [relpath for relpath in local_files if relpath not in remote_files]
    return {"download": download, "remove": remove}


def blob_url(manifest_url, sha256):
    """Where a content-addressed blob lives: beside the manifest in the same
    release-download folder, named by its sha256."""
    return manifest_url.rsplit("/", 1)[0] + "/" + sha256


def can_self_update(result):
    """True only if result is a "newer" outcome carrying a manifest_url, a local
    installed manifest.json exists to diff against, and the install directory is
    writable. Everything else (dev run, a build predating 9.3c, no manifest_url
    hosted yet, a locked-down install dir) falls back to the plain notify +
    browser-open flow that already exists."""
    if result.get("status") != "newer" or not result.get("manifest_url"):
        return False
    if load_local_manifest() is None:
        return False
    return os.access(paths.install_dir(), os.W_OK)
