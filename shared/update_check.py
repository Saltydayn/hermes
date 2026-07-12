"""Update check data layer (Phase 5.3). Headless and tkinter-free.

Fetches a small dev-hosted version.json, compares its "latest" against the running
VERSION, and reports one of four outcomes: newer, current, ahead, error. check() is the
single entry point and never raises; a failed or offline check simply returns "error".

The v1.0 consumer (Home) only notifies and opens download_url in a browser. The Phase
9.3 auto-updater will call the same check() and act on download_url itself (download and
apply), so nothing here assumes the browser-open is the only possible action. Unknown
keys in version.json are ignored, letting the hosted file grow fields (sha256, size and
so on) without breaking older clients.

Stdlib only (plus shared.version, read-only). Only fetch() raises; its caller runs it on
a worker thread and catches.
"""

import json
import urllib.request

from shared import version

# The built-in update source. Set before release (see for-you/DISTRIBUTION.md);
# "" means checking is unconfigured. Overridable via the "update_url" config key.
DEFAULT_UPDATE_URL = ""

_NOTES_MAX = 200


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


def parse_info(raw):
    """Validate a decoded version.json object. Returns {"latest", "download_url",
    "notes"} with download_url/notes coerced to "" when missing or invalid
    (download_url must be http(s); notes clamped to 200 chars). Returns None if raw is
    not a dict or "latest" is missing/unparsable. Unknown keys are ignored so future
    fields never break this client. Never raises."""
    if not isinstance(raw, dict):
        return None
    latest = raw.get("latest")
    if parse_version(latest) is None:
        return None
    url = raw.get("download_url")
    if not (isinstance(url, str) and (url.startswith("http://")
                                      or url.startswith("https://"))):
        url = ""
    notes = raw.get("notes")
    if not isinstance(notes, str):
        notes = ""
    return {"latest": latest.strip(), "download_url": url, "notes": notes[:_NOTES_MAX]}


def fetch(url, timeout=4):
    """Blocking GET + JSON decode of the hosted version file. RAISES on any failure
    (network, HTTP, decode). Do not call with ""; the caller runs this on a worker
    thread and catches."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def check(url, current=version.VERSION, timeout=4):
    """The one entry point for consumers (Home now, the 9.3 updater later). NEVER
    raises. Returns {"status": "newer"|"current"|"ahead"|"error", "latest": str,
    "download_url": str, "notes": str}. VERSION_SUFFIX plays no part in the compare."""
    result = {"status": "error", "latest": "", "download_url": "", "notes": ""}
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
