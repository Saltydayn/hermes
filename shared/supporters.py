"""Supporter data layer (Phase 4.3a). Headless and tkinter-free.

Owns supporter data end to end: load a bundled seed and a writable cache, refresh from a
dev-hosted JSON in the background (offline-safe), validate/normalize it, keep immortal names
alive across syncs, and provide the ordering the About wall needs (tiers fully shuffled each
call; Special Thanks pinned-first then role groups). The About UI (SPEC_4.3b) consumes this;
this module holds NO rendering and NO tkinter so it is unit-testable directly.

Stdlib only. Every load path degrades gracefully and never raises to the caller; only the
explicit fetch() raises (its caller runs it on a worker thread and catches it).
"""

import json
import os
import random
import urllib.request

DEFAULT_TIER_ORDER = ("terabyte", "gigabyte", "megabyte", "kilobyte", "byte")
IMMORTAL_TIERS = ("terabyte", "gigabyte", "megabyte")   # entries here never drop on resync
THANKS_ROLES = ("streamer", "friend", "tester")

# Display metadata. `accent` is a PALETTE KEY (string) so this file stays tk-free; the UI
# resolves it to a color. terabyte is intentionally omitted until the tier exists; the UI
# skips keys without meta (see tier_render_order).
TIER_META = {
    "gigabyte": {"label": "Gigabyte", "accent": "yellow"},   # gold #ffd166
    "megabyte": {"label": "Megabyte", "accent": "orange"},
    "kilobyte": {"label": "Kilobyte", "accent": "blue"},
    "byte":     {"label": "Byte",     "accent": "green"},
}

_CACHE_NAME = "supporters_cache.json"
_SEED_NAME = "supporters.seed.json"


# ── internal helpers ──────────────────────────────────────────────────────────────────
def _read_json(path):
    """Parse a JSON object file. Return the dict, or None on any failure (missing/corrupt/
    not-an-object). Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _is_count(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _norm_entries(entries):
    """Normalize a tier list: bare strings -> {"name": s}; dicts must have a non-empty
    string name; keep optional url/immortal. Drop malformed entries (never raise)."""
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if isinstance(e, str):
            if e.strip():
                out.append({"name": e.strip()})
        elif isinstance(e, dict):
            name = e.get("name")
            if isinstance(name, str) and name.strip():
                item = {"name": name.strip()}
                url = e.get("url")
                if isinstance(url, str) and url.strip():
                    item["url"] = url.strip()
                if e.get("immortal") is True:
                    item["immortal"] = True
                out.append(item)
    return out


def _norm_thanks(thanks):
    """Normalize the thanks list: non-empty string name; role coerced into THANKS_ROLES
    (unknown -> friend); keep optional pinned/url. Drop malformed entries."""
    if not isinstance(thanks, list):
        return []
    out = []
    for e in thanks:
        if isinstance(e, str):
            if e.strip():
                out.append({"name": e.strip(), "role": "friend"})
        elif isinstance(e, dict):
            name = e.get("name")
            if isinstance(name, str) and name.strip():
                role = e.get("role")
                if role not in THANKS_ROLES:
                    role = "friend"
                item = {"name": name.strip(), "role": role}
                if e.get("pinned") is True:
                    item["pinned"] = True
                url = e.get("url")
                if isinstance(url, str) and url.strip():
                    item["url"] = url.strip()
                out.append(item)
    return out


# ── public API ────────────────────────────────────────────────────────────────────────
def validate(raw):
    """Normalize `raw` into a guaranteed-valid structure: tier_order / tiers / anonymous /
    thanks always present with correct types. Coerces defensively and drops malformed
    entries rather than crashing. Never raises."""
    if not isinstance(raw, dict):
        raw = {}

    out = {}
    out["updated"] = raw["updated"] if isinstance(raw.get("updated"), str) else ""
    out["source"] = raw["source"] if isinstance(raw.get("source"), str) else ""

    order = raw.get("tier_order")
    if isinstance(order, list):
        order = [t for t in order if isinstance(t, str)]
    if not order:
        order = list(DEFAULT_TIER_ORDER)
    out["tier_order"] = order

    raw_tiers = raw.get("tiers") if isinstance(raw.get("tiers"), dict) else {}
    tiers = {}
    for key in raw_tiers:
        if isinstance(key, str):
            tiers[key] = _norm_entries(raw_tiers[key])
    for key in order:                       # guarantee every ordered tier exists
        tiers.setdefault(key, [])
    out["tiers"] = tiers

    raw_anon = raw.get("anonymous") if isinstance(raw.get("anonymous"), dict) else {}
    anon = {key: 0 for key in order}
    for key, val in raw_anon.items():
        if isinstance(key, str) and _is_count(val):
            anon[key] = val
    out["anonymous"] = anon

    out["thanks"] = _norm_thanks(raw.get("thanks", []))
    return out


def load_local(paths):
    """Return a valid dict from the writable cache if present and valid, else the bundled
    seed, else a minimal valid empty structure. Always validated; never raises."""
    data = _read_json(os.path.join(paths.USER_DATA_DIR, _CACHE_NAME))
    if data is None:
        data = _read_json(os.path.join(paths.get_path("assets", create=False), _SEED_NAME))
    return validate(data if data is not None else {})


def load_cache(paths):
    """Return ONLY the writable cache (the last successful sync), validated; or an empty valid
    structure if there is no cache. Unlike load_local this does NOT fall back to the bundled
    seed, so a background merge never folds seed placeholder names into live synced data."""
    return validate(_read_json(os.path.join(paths.USER_DATA_DIR, _CACHE_NAME)) or {})


def fetch(url, timeout=5):
    """Blocking GET + parse + validate of the hosted supporters JSON. RAISES on any failure
    (network, decode, JSON). A blank url means 'no refresh' and must be handled by the
    CALLER (do not call fetch('')). The caller runs this on a worker thread and catches."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read()
    return validate(json.loads(raw.decode("utf-8")))


def save_cache(paths, data):
    """Best-effort write of `data` to the user-data cache. Swallows OSError (a read-only or
    full disk must not break the app)."""
    try:
        os.makedirs(paths.USER_DATA_DIR, exist_ok=True)
        with open(os.path.join(paths.USER_DATA_DIR, _CACHE_NAME), "w", encoding="utf-8") as f:
            json.dump(validate(data), f, indent=2)
    except OSError:
        pass


def merge_immortal(old, new):
    """Keep immortal names from `old` that a fresh `new` omits, so Megabyte/Gigabyte/Terabyte
    entries are never lost even if a future sync drops them (independent of the creator's
    tooling). An entry is immortal if it sets immortal=True OR sits in an IMMORTAL_TIERS tier.
    Returns the merged (validated) `new`."""
    old = validate(old)
    new = validate(new)
    for tier in set(old["tiers"]) | set(new["tiers"]):
        new_list = new["tiers"].setdefault(tier, [])
        new_names = {e["name"] for e in new_list}
        for entry in old["tiers"].get(tier, []):
            if (entry.get("immortal") or tier in IMMORTAL_TIERS) and entry["name"] not in new_names:
                new_list.append(entry)
                new_names.add(entry["name"])
    return new


def shuffled_tier(data, key, rng=None):
    """All entries of tier `key`, in a fresh random order each call (no pinning in tiers)."""
    entries = list(data.get("tiers", {}).get(key, []))
    (rng or random).shuffle(entries)
    return entries


def thanks_pinned(data):
    """Pinned Special-Thanks entries, in stored order (stable across calls)."""
    return [e for e in data.get("thanks", []) if e.get("pinned")]


def thanks_groups(data, rng=None):
    """{role: [shuffled non-pinned entries]} for each role in THANKS_ROLES. Pinned entries are
    excluded (they render first, separately); unknown roles bucket under friend."""
    groups = {role: [] for role in THANKS_ROLES}
    for e in data.get("thanks", []):
        if e.get("pinned"):
            continue
        role = e.get("role")
        groups[role if role in groups else "friend"].append(e)
    for role in groups:
        (rng or random).shuffle(groups[role])
    return groups


def anonymous_count(data, key):
    """The '+N anonymous' count for a tier (0 if missing/invalid)."""
    val = data.get("anonymous", {}).get(key, 0)
    return val if _is_count(val) else 0


def tier_render_order(data):
    """The tiers to render, top to bottom: data['tier_order'] keys that have TIER_META
    (so unknown / not-yet-launched tiers like terabyte are skipped)."""
    return [k for k in data.get("tier_order", DEFAULT_TIER_ORDER) if k in TIER_META]
