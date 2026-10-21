"""Subtitle Studio data model - Phase 6.2a.

Word -> Line -> Transcript: plain dataclasses (serializer-friendly, no widgets, no
global state) so 6.3's transcript editor and 6.4's karaoke renderer can share one
shape. `speaker` (6.6 collab) and `effects` (6.4 per-word styling) are reserved
fields baked in now so nothing later has to migrate the file format.

Persistence is file-based (one JSON per clip under paths.get_path("transcripts")),
not in config.json - a word-level transcript is too heavy for a file rewritten
wholesale on every unrelated setting change (Prime Directive #2, lean).

Leaf module: only shared.paths. Never imports modules/.
"""

import hashlib
import json
import math
import os
import shutil
import wave
from collections import namedtuple
from dataclasses import dataclass, field, fields
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from shared import assets, paths

# Reference output geometry the style's px fields are authored against (matches
# shared.compositor.OUT_W/OUT_H). Kept as local literals rather than an import - this
# module never imports shared.compositor, which imports THIS module to call
# render_subtitles, and a two-way import would cycle.
_REF_W, _REF_H = 1080, 1920

# Smallest allowed word/event span (seconds) - keeps a split/nudge from ever
# producing a zero or negative-length word.
MIN_SPAN = 0.001

# LRU cap on saved transcript files (mirrors the editor's clip_edits LRU intent).
TRANSCRIPTS_CAP = 200   # default; Subtitle Studio passes the Home setting (config clip_memory)


@dataclass
class Word:
    text: str
    start: float            # seconds, on the FINISHED-clip timeline
    end: float               # seconds, finished-clip timeline
    conf: float = 1.0        # Whisper word probability (0..1); 1.0 for hand-inserted words
    speaker: Optional[str] = None    # RESERVED for 6.6 collab; unset in 6.2/6.3
    effects: list = field(default_factory=list)   # per-word effect tags (6.4)
    # 6.5c: written by hand rather than heard by the model. Called out in the
    # transcript and on the timeline, because a word the user just added has not
    # been placed against the audio yet and is the one most likely to need
    # dragging - and, in one-word mode especially, it may not sit well once the
    # reveal style regroups.
    added: bool = False

    def to_dict(self):
        return {
            "text": self.text, "start": self.start, "end": self.end,
            "conf": self.conf, "speaker": self.speaker, "effects": list(self.effects),
            "added": bool(self.added),
        }

    @staticmethod
    def from_dict(d):
        return Word(
            text=str(d.get("text", "")),
            start=float(d.get("start", 0.0)),
            end=float(d.get("end", 0.0)),
            conf=float(d.get("conf", 1.0)),
            speaker=d.get("speaker"),
            effects=list(d.get("effects") or []),
            added=bool(d.get("added", False)),
        )


@dataclass
class Event:
    """A non-speech transcript entry (e.g. "[*laughter*]") - not a word, its own
    subtitle during its span (6.4 draws it separately from `lines`). `kind="manual"`
    is user-authored; `kind="gap_candidate"` is a 6.3b auto-suggestion that renders
    nothing until confirmed into a manual event or dismissed.

    `effects` works exactly as it does on a Word (6.5c). It defaults to a light
    shake rather than nothing: a non-speech entry describes something happening
    ("[*laughter*]"), and a little movement says so. Old saved events without the
    field get that default on load."""
    text: str
    start: float
    end: float
    kind: str = "manual"
    effects: list = field(default_factory=lambda: ["shake"])

    def to_dict(self):
        return {"text": self.text, "start": self.start, "end": self.end,
                "kind": self.kind, "effects": list(self.effects)}

    @staticmethod
    def from_dict(d):
        raw = d.get("effects")
        return Event(
            text=str(d.get("text", "")),
            start=float(d.get("start", 0.0)),
            end=float(d.get("end", 0.0)),
            kind=str(d.get("kind", "manual")),
            effects=[str(e) for e in raw] if isinstance(raw, list) else ["shake"],
        )


@dataclass
class Line:
    words: list   # list[Word], in order (a sentence - see rebuild_lines)

    @property
    def start(self):
        return self.words[0].start if self.words else 0.0

    @property
    def end(self):
        return self.words[-1].end if self.words else 0.0

    def to_dict(self):
        return {"words": [w.to_dict() for w in self.words]}

    @staticmethod
    def from_dict(d):
        return Line(words=[Word.from_dict(w) for w in (d.get("words") or [])])


# Hard safety cap for a run with no sentence-final punctuation, so one giant
# unpunctuated ramble still gets split somewhere sane (always at a word boundary).
_LINE_MAX_WORDS = 14
_LINE_MAX_CHARS = 90
_SENTENCE_END_CHARS = (".", "?", "!")


def rebuild_lines(words, events=None):
    """Deterministically group a flat, time-ordered Word stream into Line objects -
    the single source of truth for line/sentence structure (6.5 A3). Independent of
    Whisper's own segment boundaries, which reshuffle unpredictably run to run and
    can cut mid-sentence: a line seals ONLY on sentence-final punctuation
    (. ? !) or the hard word/char safety cap above, always at a word boundary -
    never mid-word, never on Whisper's own (re)segmentation. Every word is kept,
    INCLUDING low-confidence ones - none are ever dropped from the stream (only
    flagged, elsewhere). `events` (optional) additionally forces a split at any
    point where a manual/gap-candidate Event's span falls strictly between two
    consecutive words, so a line never silently spans across it. Pure; empty
    input -> empty output."""
    words = list(words)
    if not words:
        return []
    cut_after = set()
    if events:
        for i in range(len(words) - 1):
            gap_lo, gap_hi = words[i].end, words[i + 1].start
            if gap_hi <= gap_lo:
                continue
            for ev in events:
                if gap_lo <= ev.start and ev.end <= gap_hi:
                    cut_after.add(i)
                    break
    lines = []
    current = []
    chars = 0
    for i, w in enumerate(words):
        current.append(w)
        chars += len(w.text) + 1
        text = w.text.strip()
        sentence_end = bool(text) and text[-1] in _SENTENCE_END_CHARS
        capped = len(current) >= _LINE_MAX_WORDS or chars >= _LINE_MAX_CHARS
        if sentence_end or capped or i in cut_after:
            lines.append(Line(words=current))
            current = []
            chars = 0
    if current:
        lines.append(Line(words=current))
    return lines


@dataclass
class Transcript:
    lines: list           # list[Line], in order
    language: str         # detected or forced language code actually used
    model_id: str         # the model id that produced it
    source_fps: float     # fps of the source clip the times were computed against
    cut_fingerprint: str  # fingerprint of the cut set the times were built on
    created: int          # unix ts
    events: list = field(default_factory=list)   # list[Event] - 6.3a non-speech entries
    # content_hash() of the transcript exactly as the model produced it,
    # so "has the user fixed anything since?" is a comparison, not a guess. "" for
    # transcripts saved before v1.1.0 (treated as edited: never replaced silently).
    source_hash: str = ""

    def words(self):
        """Iterator over every Word in order, across all lines."""
        for line in self.lines:
            for w in line.words:
                yield w

    @property
    def duration(self):
        return self.lines[-1].end if self.lines else 0.0

    def to_dict(self):
        return {
            "lines": [ln.to_dict() for ln in self.lines],
            "language": self.language,
            "model_id": self.model_id,
            "source_fps": self.source_fps,
            "cut_fingerprint": self.cut_fingerprint,
            "created": self.created,
            "events": [ev.to_dict() for ev in self.events],
            "source_hash": self.source_hash,
        }

    @staticmethod
    def from_dict(d):
        # 6.5 A3: flatten the saved lines back to a flat word stream and rebuild
        # them deterministically - upgrades an OLD transcript (one Line per
        # Whisper segment, reshuffles run to run, can cut mid-sentence) silently
        # on load. A no-op for an already-rebuilt transcript: rebuild_lines is
        # deterministic, so re-running it over its own output reproduces the same
        # lines.
        events = [Event.from_dict(ev) for ev in (d.get("events") or [])]
        raw_lines = [Line.from_dict(ln) for ln in (d.get("lines") or [])]
        words = [w for ln in raw_lines for w in ln.words]
        return Transcript(
            lines=rebuild_lines(words, events=events),
            language=str(d.get("language", "")),
            model_id=str(d.get("model_id", "")),
            source_fps=float(d.get("source_fps", 0.0)),
            cut_fingerprint=str(d.get("cut_fingerprint", "")),
            created=int(d.get("created", 0)),
            events=events,
            source_hash=str(d.get("source_hash", "") or ""),
        )


def content_hash(transcript):
    """Short fingerprint of everything a user can change in a transcript: word text,
    timing and effects, plus the non-speech events. Line grouping is left out on
    purpose - it is rebuilt from the words on every load."""
    words = [[w.text, round(w.start, 4), round(w.end, 4), sorted(w.effects)]
             for w in transcript.words()]
    events = [[ev.text, round(ev.start, 4), round(ev.end, 4), ev.kind,
               sorted(ev.effects)] for ev in transcript.events]
    payload = json.dumps([words, events], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def is_hand_edited(transcript):
    """True if the user changed anything since the model wrote this transcript (or
    it predates the source_hash field, when nobody can tell). Keep-in-sync only
    re-transcribes transcripts that are NOT hand-edited."""
    if transcript is None:
        return False
    if not transcript.source_hash:
        return True
    return content_hash(transcript) != transcript.source_hash


# ── Persistence (file-based, NOT in config.json) ────────────────────────────────────
def transcript_path(clip_key):
    return os.path.join(paths.get_path("transcripts"), clip_key + ".json")


def save_transcript(clip_key, transcript, cap=TRANSCRIPTS_CAP):
    """Write `transcript` for `clip_key` as pretty JSON. Atomic-ish: write to a .tmp
    file then os.replace, so a crash mid-write never leaves a half file. Evicts the
    oldest-saved transcripts past `cap`."""
    path = transcript_path(clip_key)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(transcript.to_dict(), f, indent=2)
    os.replace(tmp, path)
    _evict_old_transcripts(cap)


def load_transcript(clip_key):
    """Return the persisted Transcript for `clip_key`, or None. Never raises - a
    missing, corrupt, or malformed file is treated as "no transcript yet"."""
    path = transcript_path(clip_key)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Transcript.from_dict(data)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def delete_transcript(clip_key):
    """Remove the persisted transcript for `clip_key` and its playback audio
    cache, if present."""
    for p in (transcript_path(clip_key), _audio_cache_path(transcript_path(clip_key))):
        try:
            os.remove(p)
        except OSError:
            pass


def _audio_cache_path(json_path):
    """The cut-audio playback cache Subtitle Studio keeps beside a transcript
    (same key, .wav)."""
    return os.path.splitext(json_path)[0] + ".wav"


def _evict_old_transcripts(cap=TRANSCRIPTS_CAP):
    d = paths.get_path("transcripts")
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return
    if len(names) <= cap:
        return
    files = [os.path.join(d, n) for n in names]
    files.sort(key=_mtime_safe)
    for p in files[: len(files) - cap]:
        for victim in (p, _audio_cache_path(p)):
            try:
                os.remove(victim)
            except OSError:
                pass
    # Audio caches whose transcript is gone (evicted before this rule existed,
    # or a transcribe that never saved) would otherwise sit on disk forever.
    keep = {os.path.splitext(n)[0] for n in os.listdir(d) if n.endswith(".json")}
    for n in os.listdir(d):
        if n.endswith(".wav") and os.path.splitext(n)[0] not in keep:
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass


def _mtime_safe(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# ── The cut fingerprint (stale-detection contract for 6.2b) ─────────────────────────
def cut_fingerprint(segments_frames, fps):
    """Short hex fingerprint of a cut set. Ignores any gain element (a 3rd tuple
    entry) - gain never moves word times, so a gain-only change must not register
    as a cut change."""
    segs = segments_frames or [[0, 0]]
    norm = [[int(s[0]), int(s[1])] for s in segs]
    payload = json.dumps(norm, separators=(",", ":")) + f"|{fps:.3f}"
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


# ── Reading the editor's cut model (read-only; never writes it) ─────────────────────
def kept_segments_for(config, source_path, total_frames):
    """Return the editor's kept frame ranges for `source_path`, or the whole clip if
    the editor never saved a block for it. Read-only - never mutates `config`."""
    block = ((config.get("editor") or {}).get("clip_edits") or {}).get(
        paths.clip_key(source_path))
    segs = (block or {}).get("segments")
    if not segs:
        return [[0, max(0, total_frames - 1)]]
    return [[int(s[0]), int(s[1])] for s in segs]   # drop gain; frames only


def layout_for(config, source_path):
    """Return the editor's saved LAYOUT block for `source_path` - `{"layout": {...},
    "keyframes": [...]}`, boxes still NORMALIZED (0..1) - or None if the editor never
    saved one for this clip. Read-only - never mutates `config`. The caller
    denormalizes against whatever frame size it actually decodes (a fraction of the
    displayed image at save time == the same fraction of the source frame's real
    size, since the display never distorted the source's aspect ratio)."""
    block = ((config.get("editor") or {}).get("clip_edits") or {}).get(
        paths.clip_key(source_path))
    if not block or not block.get("layout"):
        return None
    return {"layout": block["layout"], "keyframes": block.get("keyframes") or []}


def is_stale(stored_fingerprint, segments_frames, fps):
    """True if the editor's LIVE cut set no longer matches the fingerprint a
    transcript was made against (6.2a fingerprint, 6.2b stale detection)."""
    return cut_fingerprint(segments_frames, fps) != stored_fingerprint


# ── Device/compute resolution (6.2b) ─────────────────────────────────────────────────
# Quality -> compute type. int8/float16/float32 are all valid on GPU; CPU has no
# float16 kernel and must NEVER get int8_float16 (it truncates output there).
_QUALITY_TARGET = {"fast": "int8", "balanced": "float16", "best": "float32"}


def gpu_available():
    """True iff CTranslate2 sees a CUDA device AND the GPU runtime was actually
    extracted (assets.gpu_installed()). Never constructs a CUDA model to test
    (CUDA loads lazily, a naive construct succeeds even with
    the runtime missing). Any import/exception - False, never raises."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0 and assets.gpu_installed()
    except Exception:  # noqa: BLE001 - GPU detection must never crash the tab
        return False


def resolve_device_compute(device_pref, quality):
    """(device, compute_type) for faster-whisper, given the user's device
    preference ("auto"/"cpu"/"gpu") and quality ("fast"/"balanced"/"best")."""
    target = _QUALITY_TARGET.get(quality, "float16")
    want_gpu = device_pref in ("auto", "gpu") and gpu_available()
    if want_gpu:
        return "cuda", target
    return "cpu", ("float32" if target == "float32" else "int8")


_cuda_wired = False


def ensure_cuda_on_path():
    """Idempotent: put the downloaded cuDNN DLL dir on PATH before GPU inference.
    cuDNN searches PATH, not only add_dll_directory. Lazy -
    only called when GPU is actually chosen, so CPU-only users' PATH stays clean."""
    global _cuda_wired
    if _cuda_wired:
        return
    d = paths.get_path("cuda")
    if os.path.isdir(d):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(d)
        except (AttributeError, OSError):
            pass
    _cuda_wired = True


# ── Languages (6.2b) ──────────────────────────────────────────────────────────────────
# Whisper's supported language codes/names (openai-whisper tokenizer.LANGUAGES set).
_WHISPER_LANGUAGES = {
    "en": "English", "zh": "Chinese", "de": "German", "es": "Spanish", "ru": "Russian",
    "ko": "Korean", "fr": "French", "ja": "Japanese", "pt": "Portuguese", "tr": "Turkish",
    "pl": "Polish", "ca": "Catalan", "nl": "Dutch", "ar": "Arabic", "sv": "Swedish",
    "it": "Italian", "id": "Indonesian", "hi": "Hindi", "fi": "Finnish", "vi": "Vietnamese",
    "he": "Hebrew", "uk": "Ukrainian", "el": "Greek", "ms": "Malay", "cs": "Czech",
    "ro": "Romanian", "da": "Danish", "hu": "Hungarian", "ta": "Tamil", "no": "Norwegian",
    "th": "Thai", "ur": "Urdu", "hr": "Croatian", "bg": "Bulgarian", "lt": "Lithuanian",
    "la": "Latin", "mi": "Maori", "ml": "Malayalam", "cy": "Welsh", "sk": "Slovak",
    "te": "Telugu", "fa": "Persian", "lv": "Latvian", "bn": "Bengali", "sr": "Serbian",
    "az": "Azerbaijani", "sl": "Slovenian", "kn": "Kannada", "et": "Estonian",
    "mk": "Macedonian", "br": "Breton", "eu": "Basque", "is": "Icelandic",
    "hy": "Armenian", "ne": "Nepali", "mn": "Mongolian", "bs": "Bosnian", "kk": "Kazakh",
    "sq": "Albanian", "sw": "Swahili", "gl": "Galician", "mr": "Marathi", "pa": "Punjabi",
    "si": "Sinhala", "km": "Khmer", "sn": "Shona", "yo": "Yoruba", "so": "Somali",
    "af": "Afrikaans", "oc": "Occitan", "ka": "Georgian", "be": "Belarusian",
    "tg": "Tajik", "sd": "Sindhi", "gu": "Gujarati", "am": "Amharic", "yi": "Yiddish",
    "lo": "Lao", "uz": "Uzbek", "fo": "Faroese", "ht": "Haitian Creole", "ps": "Pashto",
    "tk": "Turkmen", "nn": "Nynorsk", "mt": "Maltese", "sa": "Sanskrit",
    "lb": "Luxembourgish", "my": "Myanmar", "bo": "Tibetan", "tl": "Tagalog",
    "mg": "Malagasy", "as": "Assamese", "tt": "Tatar", "haw": "Hawaiian",
    "ln": "Lingala", "ha": "Hausa", "ba": "Bashkir", "jw": "Javanese",
    "su": "Sundanese", "yue": "Cantonese",
}


def _build_languages():
    """Auto-detect, then German/English pinned (the DE/EN audience), then the rest
    alphabetical by display name."""
    pinned = [("auto", "Auto-detect"), ("de", "German"), ("en", "English")]
    pinned_codes = {"de", "en"}
    rest = sorted(
        ((code, name) for code, name in _WHISPER_LANGUAGES.items()
         if code not in pinned_codes),
        key=lambda item: item[1])
    return pinned + rest


LANGUAGES = _build_languages()
_LANGUAGE_NAMES = dict(LANGUAGES)


def language_name(code):
    """Display name for a language code; the raw code itself if unknown."""
    if not code or code == "auto":
        return "the detected language"
    return _LANGUAGE_NAMES.get(code, code)


def filter_languages(query):
    """Type-to-filter reducer: LANGUAGES entries whose name contains `query`
    (case-insensitive), preserving LANGUAGES order. An empty query returns all."""
    q = (query or "").strip().lower()
    if not q:
        return list(LANGUAGES)
    return [(code, name) for code, name in LANGUAGES if q in name.lower()]


#: (upper weight bound, phrase template) - how heavily the 2nd language is mixed
#: in, from "a few terms" to "almost equally". Checked in order; the first
#: threshold the weight is BELOW wins.
_MIX_TIERS = (
    (20, "The speaker mostly speaks {a}, with only a few {b} words mixed in."),
    (60, "The speaker mixes {a} and {b}."),
    (101, "The speaker mixes {a} and {b} heavily, almost equally."),
)


def _mixing_phrase(language, second_language, weight):
    a, b = language_name(language), language_name(second_language)
    w = max(0, min(100, int(weight)))
    for threshold, template in _MIX_TIERS:
        if w < threshold:
            return template.format(a=a, b=b)
    return _MIX_TIERS[-1][1].format(a=a, b=b)


def build_initial_prompt(language, game_terms, game_terms_text, second_language,
                         second_language_weight=50):
    """A short Whisper initial_prompt seed (or None). Never hard-forces a language
    in the prompt text (in testing, forcing German wrecked embedded English gaming
    words) - the `language` transcribe argument handles that; this only seeds
    vocabulary and code-switching expectations.

    `game_terms` is checked far more often than not (user feedback), so a bare
    checkbox with no specifics stays a LIGHT nudge rather than injecting a fixed
    jargon list that may not even appear in the clip. `game_terms_text` (freeform,
    e.g. character/item names or callouts) is the actually-useful seed - it tells
    the model specific words to expect instead of guessing at generic ones.
    `second_language_weight` (0-100, "a few terms" to "almost 50/50") picks how
    strongly the mixing phrase reads."""
    bits = []
    if game_terms:
        text = (game_terms_text or "").strip()
        if text:
            bits.append(f"The clip may include these specific words or names: "
                        f"{text}.")
        else:
            bits.append("This may include gaming terms and slang.")
    if second_language:
        bits.append(_mixing_phrase(language, second_language, second_language_weight))
    return " ".join(bits) or None


# ── Pure edit helpers (6.3a) ──────────────────────────────────────────────────────────
# All operate on a Transcript in place and return it. `li`/`wi` are line/word indices.
# Kept pure (no widgets) so the transcript editor's edits are unit-testable and safe to
# call from anywhere.
def set_word_text(t, li, wi, text):
    """Change a word's text; start/end/conf are left untouched."""
    t.lines[li].words[wi].text = text
    return t


def split_word(t, li, wi, at_char):
    """One word -> two, split at character index `at_char`. The time span divides
    PROPORTIONALLY to the character split point, clamped so neither half is ever a
    zero/negative span."""
    word = t.lines[li].words[wi]
    text = word.text
    n = len(text)
    at_char = max(1, min(n - 1, at_char)) if n > 1 else max(1, at_char)
    first_text, second_text = text[:at_char], text[at_char:]
    span = word.end - word.start
    frac = at_char / n if n else 0.5
    mid = word.start + span * frac
    if span > 2 * MIN_SPAN:
        mid = max(word.start + MIN_SPAN, min(word.end - MIN_SPAN, mid))
    else:
        # Too short to guarantee MIN_SPAN on both halves - still guarantee each
        # half is strictly positive by splitting the span itself in two.
        mid = word.start + span / 2
    first = Word(text=first_text, start=word.start, end=mid, conf=word.conf,
                speaker=word.speaker, effects=list(word.effects))
    second = Word(text=second_text, start=mid, end=word.end, conf=word.conf,
                 speaker=word.speaker, effects=list(word.effects))
    t.lines[li].words[wi:wi + 1] = [first, second]
    return t


def merge_words(t, li, wi):
    """Merge word `wi` with `wi+1` into one. Span = start of first .. end of
    second; conf = the lower of the two.

    Text is joined with NO separator (6.5c, was a single space). Merging is the
    fix for a model that split one word in two - "Tem pest" back into "Tempest" -
    and rejoining with a space would leave it looking exactly as it did. It also
    keeps the invariant the whole editor relies on: a space always separates two
    words, so no word ever contains one. A word that did could never be typed
    into again, because the resync scans to the nearest space to find a word's
    bounds."""
    words = t.lines[li].words
    a, b = words[wi], words[wi + 1]
    merged = Word(text=f"{a.text}{b.text}".strip(), start=a.start, end=b.end,
                 conf=min(a.conf, b.conf), speaker=a.speaker, effects=list(a.effects))
    words[wi:wi + 2] = [merged]
    return t


def merge_lines(t, li):
    """Join line `li + 1` onto the end of line `li`, keeping every word separate
    (6.5c).

    This is the text-editor gesture: backspace at the start of a row, or delete
    at the end of one, joins the two rows. It is NOT merge_words - the words on
    either side of the join stay their own words with their own timings; only
    the segment boundary between them goes away."""
    if li < 0 or li + 1 >= len(t.lines):
        return t
    t.lines[li].words.extend(t.lines[li + 1].words)
    del t.lines[li + 1]
    return t


def split_line(t, li, wi):
    """Split line `li` so that word `wi` begins a new line (6.5c: Return in the
    transcript starts a new segment there). A no-op at either end of the line,
    where there is nothing to split off."""
    if li < 0 or li >= len(t.lines):
        return t
    words = t.lines[li].words
    if wi <= 0 or wi >= len(words):
        return t
    tail = words[wi:]
    del words[wi:]
    t.lines.insert(li + 1, Line(words=tail))
    return t


def delete_word(t, li, wi):
    """Remove one word; neighbours' times are unchanged (a gap is fine). A line left
    with no words is dropped so no blank rows remain."""
    line = t.lines[li]
    del line.words[wi]
    if not line.words:
        del t.lines[li]
    return t


def insert_word(t, li, wi, text, start, end):
    """Insert a new word at position `wi` with the GIVEN times (conf=1.0, the
    hand-authored sentinel). Only enforces start<end (min span) - the caller may be
    fixing a bad boundary, so neighbour times are not forced."""
    if end - start < MIN_SPAN:
        end = start + MIN_SPAN
    t.lines[li].words.insert(wi, Word(text=text, start=start, end=end, conf=1.0))
    return t


# Default duration (seconds) carved for a word inserted via insert_word_squeeze.
_INSERTED_WORD_SPAN = 0.2
# 6.5c: roughly how long the text would take to say, for a hand-added word or
# phrase - a bare fixed span makes a long phrase flash past and a short word
# linger. Bounded at both ends so nothing absurd comes out of a stray paste.
_SECONDS_PER_CHAR = 0.075
_MIN_NATURAL_SPAN = 0.30
_MAX_NATURAL_SPAN = 3.00


def natural_span(text):
    """A sensible duration for hand-authored `text`, from its length."""
    return max(_MIN_NATURAL_SPAN,
               min(_MAX_NATURAL_SPAN, _SECONDS_PER_CHAR * len(text.strip())))


def insert_word_squeeze(t, li, wi, text="word", span=None):
    """Insert a new word at position `wi` in line `li`, deriving its [start,end]
    from its neighbours - and, unlike insert_word/nudge_word_time, SHRINKING a
    touching/overlapping neighbour to make room (6.5 B4), never below half of
    that neighbour's own span and never before 0. There's no user-picked
    boundary to respect yet - the new word is jumped-to and selected right after
    insert so any of the three times can be fixed up immediately. No neighbour
    on one or both sides falls back to a bare default span. Every span touched
    keeps at least MIN_SPAN - never produces a zero/negative-length word."""
    words = t.lines[li].words
    prev = words[wi - 1] if wi > 0 else None
    nxt = words[wi] if wi < len(words) else None
    want = _INSERTED_WORD_SPAN if span is None else max(MIN_SPAN, float(span))

    if prev is None and nxt is None:
        start, end = 0.0, want
    elif prev is None:
        end = nxt.start
        start = end - want
    elif nxt is None:
        start = prev.end
        end = start + want
    else:
        mid = (prev.end + nxt.start) / 2.0
        start, end = mid - want / 2.0, mid + want / 2.0
    if start < 0.0:
        # Never before 0: ahead of a first word that starts at 0.0 the new word
        # used to get a negative span and never showed.
        start, end = 0.0, end - start
    # Room is borrowed from the neighbours, but each keeps at least half of its own
    # span: squeezing one down to a millisecond made it vanish from the video
    # (pasting two words ahead of an existing one did exactly that).
    if prev is not None:
        start = max(start, (prev.start + prev.end) / 2.0)
    if nxt is not None:
        end = min(end, (nxt.start + nxt.end) / 2.0)
    if prev is not None and start < prev.end:
        prev.end = max(prev.start + MIN_SPAN, start)
    if nxt is not None and end > nxt.start:
        nxt.start = min(nxt.end - MIN_SPAN, end)
    if prev is not None:
        start = max(start, prev.end)
    if nxt is not None:
        end = min(end, nxt.start)
    if end - start < MIN_SPAN:
        # Neighbours left almost no room even after squeezing - fall back to a
        # bare MIN_SPAN word ending exactly at the next word's own start (or
        # starting at the previous word's own end if there's no next word).
        if nxt is not None:
            end, start = nxt.start, max(0.0, nxt.start - MIN_SPAN)
            if end - start < MIN_SPAN:
                end = start + MIN_SPAN
        else:
            start, end = prev.end, prev.end + MIN_SPAN

    return insert_word(t, li, wi, text, start, end)


def nudge_word_time(t, li, wi, start=None, end=None):
    """Set either boundary of a word's time span. Enforces start<end (min 1 ms) and
    never auto-shifts neighbours - the user is being precise here."""
    w = t.lines[li].words[wi]
    new_start = w.start if start is None else start
    new_end = w.end if end is None else end
    if new_end - new_start < MIN_SPAN:
        if start is not None and end is None:
            new_end = new_start + MIN_SPAN
        elif end is not None and start is None:
            new_start = new_end - MIN_SPAN
        else:
            new_end = new_start + MIN_SPAN
    w.start, w.end = new_start, new_end
    return t


def nudge_event_time(t, idx, start=None, end=None):
    """Set either boundary of an event's time span (6.5 C1 - lets a confirmed
    gap be dragged on the timeline the same way a word can be). Mirrors
    nudge_word_time exactly: enforces start<end (min 1 ms), never auto-shifts
    neighbours."""
    ev = t.events[idx]
    new_start = ev.start if start is None else start
    new_end = ev.end if end is None else end
    if new_end - new_start < MIN_SPAN:
        if start is not None and end is None:
            new_end = new_start + MIN_SPAN
        elif end is not None and start is None:
            new_start = new_end - MIN_SPAN
        else:
            new_end = new_start + MIN_SPAN
    ev.start, ev.end = new_start, new_end
    return t


def time_neighbours(t, li, wi):
    """(previous_word_end, next_word_start) around the word at (li, wi) in TIME
    order, either side being None at the ends. Time order, not document order:
    an inserted or retimed word can sit anywhere on the clock."""
    target = t.lines[li].words[wi]
    flat = sorted((w for line in t.lines for w in line.words),
                  key=lambda w: (w.start, w.end))
    for i, w in enumerate(flat):
        if w is target:
            return (flat[i - 1].end if i > 0 else None,
                    flat[i + 1].start if i + 1 < len(flat) else None)
    return None, None


def move_word_time(t, li, wi, new_start):
    """Shift a word's WHOLE span so it starts at `new_start`, keeping its
    duration (6.5c: dragging a word's body on the timeline, as opposed to
    dragging one edge). Unlike nudge_word_time - which sets a single boundary
    and deliberately allows overlap, because the user is being precise there -
    a whole-word move is clamped into the free slot between its time-order
    neighbours and never goes negative, so a body drag can never reorder the
    transcript. With no room between neighbours the word parks at the earliest
    legal start rather than shrinking."""
    w = t.lines[li].words[wi]
    dur = max(MIN_SPAN, w.end - w.start)
    prev_end, next_start = time_neighbours(t, li, wi)
    lo = max(0.0, prev_end if prev_end is not None else 0.0)
    start = max(lo, float(new_start))
    if next_start is not None:
        start = max(lo, min(start, next_start - dur))
    w.start, w.end = start, start + dur
    return t


def move_event_time(t, idx, new_start):
    """Whole-span move for an event, mirroring move_word_time (6.5c). Clamped
    only against zero: events legitimately overlap words, and the events list is
    deliberately NOT re-sorted here - a drag holds `idx` for its whole duration."""
    ev = t.events[idx]
    dur = max(MIN_SPAN, ev.end - ev.start)
    start = max(0.0, float(new_start))
    ev.start, ev.end = start, start + dur
    return t


def add_event(t, text, start, end, kind="manual"):
    """Add a non-speech Event, kept in time order."""
    t.events.append(Event(text=text, start=start, end=end, kind=kind))
    t.events.sort(key=lambda ev: ev.start)
    return t


def delete_event(t, idx):
    """Remove the event at `idx`, if it exists."""
    if 0 <= idx < len(t.events):
        del t.events[idx]
    return t


def confirm_gap(t, idx, text):
    """Turn a `gap_candidate` Event at `idx` into a labelled `manual` event."""
    if 0 <= idx < len(t.events):
        ev = t.events[idx]
        ev.text = text
        ev.kind = "manual"
    return t


def is_flagged(word, cutoff):
    """True if a word's confidence sits below the shaky-word cutoff. Hand-inserted
    words (conf=1.0) never flag at any cutoff <= 1.0."""
    return word.conf < cutoff


def source_frame_for_output_time(segments_frames, fps, t_out):
    """Map a finished-clip time (seconds) to a SOURCE frame index, walking the kept
    segments (mirrors the editor's output<->source mapping, but on our own segment
    data - never imports the editor). `segments_frames` = [[start_f, end_f], ...]
    kept ranges, in order. Whole-clip (no cuts, a single range starting at 0) is the
    identity round(t_out*fps)."""
    if fps <= 0:
        return 0
    offset = max(0.0, t_out) * fps
    if not segments_frames:
        return int(round(offset))
    consumed = 0.0
    for s, e in segments_frames:
        seg_len = e - s + 1
        if offset < consumed + seg_len:
            return int(s + round(offset - consumed))
        consumed += seg_len
    return int(segments_frames[-1][1])


# ── Engine tuning helpers (6.3b) ──────────────────────────────────────────────────────
_BEAM_FOR_QUALITY = {"fast": 1, "balanced": 5, "best": 8}
# faster-whisper's own temperature fallback ladder - used whenever the Expert
# temperature override is left at 0 (the "use the default" sentinel).
_DEFAULT_TEMPERATURE_LADDER = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]


def build_hotwords(game_terms_text):
    """The user's custom words/names as a single space-joined string for Whisper's
    `hotwords` parameter (purpose-built for proper nouns, unlike the prompt), or
    None if empty. Comma or whitespace separated input both work."""
    text = (game_terms_text or "").strip()
    if not text:
        return None
    joined = " ".join(text.replace(",", " ").split())
    return joined or None


def apply_offset(seconds, offset_ms):
    """Shift a time by a global karaoke offset (ms), never below 0. Applied only
    where word times are CONSUMED for display/playback - never baked into the
    stored transcript, so changing it needs no re-transcribe."""
    return max(0.0, seconds + offset_ms / 1000.0)


def beam_for_quality(quality, expert_override=0):
    """Beam size for faster-whisper: the Expert override if set (>0), else the
    Quality control's own mapping (fast=1/balanced=5/best=8 - no second speed knob)."""
    if expert_override and expert_override > 0:
        return int(expert_override)
    return _BEAM_FOR_QUALITY.get(quality, 5)


def build_transcribe_kwargs(language, initial_prompt, hotwords, quality, expert,
                            vad, reduce_hallucinations):
    """Assemble the full kwargs dict for `WhisperModel.transcribe(wav, **kwargs)`,
    purely from settings - keeps the engine wiring unit-testable without a mocked
    model load. An untouched Expert dict reproduces faster-whisper's own defaults,
    so a Config-untouched user sees no behaviour change from 6.2b."""
    expert = expert or {}
    temp_override = float(expert.get("temperature", 0.0) or 0.0)
    # 6.5 A2 correction: every segment still starts at temperature 0 (greedy) - a
    # clip that decodes cleanly comes out identical run to run either way. But
    # WITHOUT the fallback ladder, a segment that goes wrong (garbled/repetitive)
    # has no escape hatch and can loop indefinitely (confirmed live: ~30s of a
    # single repeated word). Pinning to a bare 0.0 dropped that retry-on-failure
    # safety net entirely, which is worse than the run-to-run variance it was
    # meant to fix - so the ladder (which only ever escalates past 0 on a segment
    # that already failed quality checks) is the default again.
    temperature = temp_override if temp_override > 0 else list(_DEFAULT_TEMPERATURE_LADDER)
    return {
        "word_timestamps": True,
        "language": None if language == "auto" else language,
        "initial_prompt": initial_prompt,
        "hotwords": hotwords,
        "beam_size": beam_for_quality(quality, int(expert.get("beam_override", 0) or 0)),
        "vad_filter": bool(vad),
        "hallucination_silence_threshold": (2.0 if reduce_hallucinations else None),
        "no_speech_threshold": float(expert.get("no_speech_threshold", 0.6)),
        "log_prob_threshold": float(expert.get("log_prob_threshold", -1.0)),
        "compression_ratio_threshold": float(expert.get("compression_ratio_threshold", 2.4)),
        # 6.5 A2: OFF by default (was faster-whisper's own default of ON). ON lets
        # each segment condition on the PREVIOUS segment's actual text - if one
        # segment goes wrong (e.g. the ladder above still lands on garbage), the
        # next segment starts from that bad context and tends to repeat/compound
        # it rather than recovering independently. Off trades a little cross-
        # segment coherence for not letting one bad segment cascade into many.
        "condition_on_previous_text": bool(expert.get("condition_on_previous_text", False)),
        "temperature": temperature,
    }


# 6.5 C2: bounded word-start snap. Small windows on purpose - this corrects a
# touch of pre-word breath/mouth noise or VAD trim lag, not a rewrite of
# Whisper's own timing.
_SNAP_WINDOW_BACK_S = 0.15
_SNAP_WINDOW_FWD_S = 0.08
_SNAP_ENERGY_FLOOR = 0.35   # fraction of the window's own peak that counts as "started"
_SNAP_MIN_MOVE_S = 0.005    # ignore a correction this small - not worth the churn


def snap_word_starts(t, pcm, rate):
    """Nudge each word's start toward the first real energy onset in a SMALL
    window around Whisper's reported start (6.5 C2 - a word often starts a
    touch early, on a pre-word breath/mouth sound, or a touch late, after VAD/
    gap trimming). Bounded: the search window never crosses into the previous
    word's span, and never passes the word's own end - so a snap can only ever
    land somewhere between "right after the previous word" and "before this
    word finishes." Mutates `t` in place; a no-op with no usable audio.
    Deterministic - same PCM in, same result out. OPT-IN (off by default) -
    see the Advanced tab; this is a polish pass, not something to force on
    everyone's transcripts."""
    if pcm is None or rate <= 0 or len(pcm) == 0:
        return t
    samples = np.abs(pcm.astype(np.int32))
    if samples.ndim > 1:
        samples = samples.max(axis=1)
    words = sorted(t.words(), key=lambda w: w.start)
    prev_end = 0.0
    for w in words:
        lo = max(prev_end, w.start - _SNAP_WINDOW_BACK_S)
        hi = min(w.end - MIN_SPAN, w.start + _SNAP_WINDOW_FWD_S)
        if hi > lo:
            i0 = max(0, int(lo * rate))
            i1 = min(len(samples), int(hi * rate))
            if i1 > i0:
                window = samples[i0:i1]
                peak = float(window.max())
                if peak > 0:
                    threshold = peak * _SNAP_ENERGY_FLOOR
                    onset_idx = int(np.argmax(window >= threshold))
                    onset_t = i0 / rate + onset_idx / rate
                    if abs(onset_t - w.start) > _SNAP_MIN_MOVE_S:
                        w.start = max(lo, min(onset_t, hi))
        prev_end = w.end
    return t


def detect_gap_candidates(wav_path, transcript, min_gap_s=2.0, floor=0.25):
    """Flag stretches with audio but no words as `gap_candidate` events (detects the
    GAP, not what's in it - no laughter/music classifier). Reads the cut WAV
    (pcm_s16le mono) with stdlib `wave` + numpy; a span qualifies if it is >=
    `min_gap_s` long AND its mean RMS exceeds `floor` * the clip's overall mean RMS
    (relative, not absolute, so it adapts to loud/quiet clips - a silent pause is
    never suggested, but laughter/music/ambient is). Deterministic; never raises -
    a missing/corrupt WAV just yields no new candidates."""
    try:
        with wave.open(wav_path, "rb") as wf:
            rate = wf.getframerate()
            ch = wf.getnchannels()
            n = wf.getnframes()
            raw = wf.readframes(n)
    except (wave.Error, OSError, EOFError):
        return transcript
    if n == 0 or rate <= 0 or ch == 0:
        return transcript
    pcm = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch).astype(np.float32)
    duration = n / rate

    def rms_for(t0, t1):
        i0 = max(0, int(t0 * rate))
        i1 = min(len(pcm), int(t1 * rate))
        if i1 <= i0:
            return 0.0
        chunk = pcm[i0:i1]
        return float(np.sqrt(np.mean(chunk * chunk)))

    words = sorted(transcript.words(), key=lambda w: w.start)
    spans = []
    prev_end = 0.0
    for w in words:
        if w.start - prev_end >= min_gap_s:
            spans.append((prev_end, w.start))
        prev_end = max(prev_end, w.end)
    if duration - prev_end >= min_gap_s:
        spans.append((prev_end, duration))

    overall_rms = rms_for(0.0, duration) or 1.0
    threshold = floor * overall_rms
    for t0, t1 in spans:
        if rms_for(t0, t1) > threshold:
            transcript.events.append(Event(text="", start=t0, end=t1,
                                           kind="gap_candidate"))
    return transcript


# ── Subtitle style model (6.4a) ───────────────────────────────────────────────────────
@dataclass
class SubtitleStyle:
    """Karaoke burn-in style - one PURE settings object shared by the editor's render
    and the Subtitle Studio preview (6.4a). Outline is a plain style property; the
    "backdrop" is the active-word highlight only - there is no whole-line backdrop.
    px fields (size, outline_width, highlight_pad/radius, shadow_dx/dy) are authored
    against the 1080x1920 reference space and scaled by render_subtitles."""
    font: str = "Anton"              # bundled name | system family | absolute .ttf/.otf path
    size: int = 72
    fill: str = "#FFFFFF"
    outline_on: bool = True
    outline_width: int = 10
    outline_color: str = "#141418"
    highlight_on: bool = True
    highlight_color: str = "#FFD166"
    highlight_pad: int = 14
    highlight_radius: int = 16
    highlight_alpha: int = 235
    highlight_text: str = ""         # "" keeps `fill` on the active word; else recolors it
    shadow_on: bool = False
    shadow_dx: int = 4
    shadow_dy: int = 5
    shadow_color: str = "#000000"
    shadow_alpha: int = 160
    align: str = "center"            # left | center | right - justifies each row within the block
    anchor_x: float = 0.5            # 0..1 of out_w - block horizontal center
    anchor_y: float = 0.72           # 0..1 of out_h - block TOP
    max_width: float = 0.90          # fraction of out_w before wrapping
    max_lines: int = 1
    reveal: str = "full_line"        # one_word | rolling | full_line (only full_line built in 6.4a)
    motion: str = "none"             # none | pop | bump (6.4b)
    transition: str = "cut"          # cut | fade - how one UNIT hands over to the
                                     # next. The unit is a word (one_word) or a
                                     # whole segment (rolling, full_line).
    highlight_mode: str = "box"      # box | outline | image (6.4c) - box is 6.4a's rounded rect
    highlight_image: str = ""        # absolute path, used only when highlight_mode == "image"
    effect_intensity: float = 1.0    # 6.5: scales shake amplitude + size-pop peak (0.5-2.5)
    # 6.5c: multiplier on the gap between words. A plain typographic space is
    # tight at subtitle sizes with a heavy face and a thick outline - two words
    # can read as one. Defaults wider than 1.0 because the tight default was
    # the complaint, not a preference.
    word_gap: float = 1.35

    def to_dict(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @staticmethod
    def from_dict(d):
        """Tolerant: unknown keys ignored, missing keys default - so a style saved
        before a future field (6.4c effects, 6.6 per-speaker styles) never breaks."""
        d = d or {}
        defaults = SubtitleStyle()
        raw = {f.name: d.get(f.name, getattr(defaults, f.name)) for f in fields(SubtitleStyle)}
        try:
            return SubtitleStyle(
                font=str(raw["font"]), size=int(raw["size"]), fill=str(raw["fill"]),
                outline_on=bool(raw["outline_on"]), outline_width=int(raw["outline_width"]),
                outline_color=str(raw["outline_color"]),
                highlight_on=bool(raw["highlight_on"]), highlight_color=str(raw["highlight_color"]),
                highlight_pad=int(raw["highlight_pad"]), highlight_radius=int(raw["highlight_radius"]),
                highlight_alpha=int(raw["highlight_alpha"]), highlight_text=str(raw["highlight_text"]),
                shadow_on=bool(raw["shadow_on"]), shadow_dx=int(raw["shadow_dx"]),
                shadow_dy=int(raw["shadow_dy"]), shadow_color=str(raw["shadow_color"]),
                shadow_alpha=int(raw["shadow_alpha"]), align=str(raw["align"]),
                anchor_x=float(raw["anchor_x"]), anchor_y=float(raw["anchor_y"]),
                max_width=float(raw["max_width"]), max_lines=int(raw["max_lines"]),
                reveal=str(raw["reveal"]), motion=str(raw["motion"]),
                transition=str(raw["transition"]),
                highlight_mode=str(raw["highlight_mode"]),
                highlight_image=str(raw["highlight_image"]),
                effect_intensity=float(raw["effect_intensity"]),
                word_gap=float(raw["word_gap"]),
            )
        except (TypeError, ValueError):
            return SubtitleStyle()


# ── Built-in presets + user save-slots (6.4c) ─────────────────────────────────────────
# Built-ins are READ-ONLY seeds - picking one
# copies its style into the active style, same as a user slot. Ordered list of
# (name, SubtitleStyle) so the Studio can list them in a stable, mockup-matching order.
SUBTITLE_PRESETS = [
    ("Clean Outline", SubtitleStyle()),
    ("Karaoke Pop", SubtitleStyle(
        reveal="one_word", motion="pop", size=84,
        outline_width=8, highlight_on=True, highlight_color="#FF5DA2")),
    ("Bold Sticker", SubtitleStyle(
        reveal="full_line", font="Anton", size=88, outline_width=16,
        highlight_on=False)),
    ("Minimal", SubtitleStyle(
        reveal="full_line", font="Poppins SemiBold", size=58,
        outline_on=False, shadow_on=True, shadow_alpha=140, highlight_on=False)),
    ("Backdrop Box", SubtitleStyle(
        reveal="full_line", highlight_on=True, highlight_mode="box",
        highlight_pad=26, highlight_radius=22, highlight_alpha=255,
        highlight_color="#1b1b1f", highlight_text="#FFD166", outline_width=6)),
    ("Neon", SubtitleStyle(
        reveal="one_word", motion="bump", font="Bebas Neue", fill="#39FF88",
        outline_color="#0a0a0a", outline_width=10, highlight_on=True,
        highlight_mode="outline", highlight_color="#39FF88")),
]


def list_slots(config):
    """The user's saved subtitle-style slots - list of {"name", "style"} dicts, in
    save order. Read-only."""
    return list(((config.get("subtitles") or {}).get("slots")) or [])


def save_slot(config, name, style, index=None):
    """Save `style` (a SubtitleStyle or its dict) as a named user slot. `index`
    overwrites that slot in place if valid; otherwise appends a new one. Mirrors
    the editor's layout-slot pattern (mutates and returns the same list config
    already holds - the caller still owns persisting `config`)."""
    slots = config.setdefault("subtitles", {}).setdefault("slots", [])
    style_dict = style.to_dict() if isinstance(style, SubtitleStyle) else dict(style)
    entry = {"name": (name or "").strip() or f"Slot {len(slots) + 1}", "style": style_dict}
    if index is not None and 0 <= index < len(slots):
        slots[index] = entry
    else:
        slots.append(entry)
    return slots


def delete_slot(config, index):
    """Remove the user slot at `index`. Returns True if a slot was removed."""
    slots = config.setdefault("subtitles", {}).setdefault("slots", [])
    if 0 <= index < len(slots):
        del slots[index]
        return True
    return False


def find_named_style(config, name):
    """The SubtitleStyle for `name` - checked against the user's saved slots first,
    then the built-in presets - or None if it matches neither. Used to restore the
    last-used preset/slot (config["subtitles"]["active_slot"]) on open."""
    if not name:
        return None
    for entry in list_slots(config):
        if entry.get("name") == name:
            return SubtitleStyle.from_dict(entry.get("style"))
    for preset_name, preset_style in SUBTITLE_PRESETS:
        if preset_name == name:
            return preset_style
    return None


# ── Font system (6.4a) ────────────────────────────────────────────────────────────────
# Four bundled OFL faces under assets/fonts/ - display name -> filename.
_BUNDLED_FONTS = {
    "Anton": "Anton-Regular.ttf",
    "Bebas Neue": "BebasNeue-Regular.ttf",
    "Montserrat ExtraBold": "Montserrat-ExtraBold.ttf",
    "Poppins SemiBold": "Poppins-SemiBold.ttf",
}
_DEFAULT_BUNDLED_FONT = "Anton"

_system_fonts_cache = None    # {family: path}, populated ONCE by _scan_system_fonts()
_font_cache = {}              # (name_or_path, size) -> ImageFont.FreeTypeFont
_FONT_CACHE_MAX = 96


def _bundled_font_path(name):
    fname = _BUNDLED_FONTS.get(name)
    if not fname:
        return None
    path = os.path.join(paths.fonts_bundled_dir(), fname)
    return path if os.path.isfile(path) else None


def _system_font_dirs():
    dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    return dirs


def _scan_system_fonts():
    """Scan installed system font dirs for .ttf/.otf ONCE, caching family name ->
    path in memory. SLOW (opens and reads every font's name table) - callers must
    run this off the UI thread. Never called from the render path - resolve_font
    caches its own result per (name, size), so a repeated render never re-scans."""
    global _system_fonts_cache
    if _system_fonts_cache is not None:
        return _system_fonts_cache
    found = {}
    for d in _system_font_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.lower().endswith((".ttf", ".otf")):
                continue
            path = os.path.join(d, n)
            try:
                family = ImageFont.truetype(path, 10).getname()[0]
            except Exception:  # noqa: BLE001 - a corrupt/odd font must never abort the scan
                continue
            found.setdefault(family, path)
    _system_fonts_cache = found
    return found


def _imported_fonts():
    """{family: path} for every .ttf/.otf under fonts_user_dir(). Re-scanned live
    (not cached like the system scan) - a handful of user-imported files at most."""
    d = paths.fonts_user_dir()
    found = {}
    try:
        names = os.listdir(d)
    except OSError:
        return found
    for n in names:
        if not n.lower().endswith((".ttf", ".otf")):
            continue
        path = os.path.join(d, n)
        try:
            family = ImageFont.truetype(path, 10).getname()[0]
        except Exception:  # noqa: BLE001
            continue
        found.setdefault(family, path)
    return found


def available_fonts():
    """{"bundled": [...], "system": [...], "imported": [...]} display names. The
    system scan is SLOW on first call - run this off the UI thread (6.4b)."""
    return {
        "bundled": list(_BUNDLED_FONTS.keys()),
        "system": sorted(_scan_system_fonts().keys()),
        "imported": sorted(_imported_fonts().keys()),
    }


def import_font(src_path):
    """Copy a user .ttf/.otf into fonts_user_dir() and return its family name.
    Imported faces are the user's own files - never bundled, never in the repo."""
    dst = os.path.join(paths.fonts_user_dir(), os.path.basename(src_path))
    shutil.copyfile(src_path, dst)
    try:
        return ImageFont.truetype(dst, 10).getname()[0]
    except Exception:  # noqa: BLE001 - degrade to the filename, never crash the import
        return os.path.splitext(os.path.basename(src_path))[0]


def _resolve_font_path(name_or_path):
    if not name_or_path:
        return None
    if os.path.isabs(name_or_path) and os.path.isfile(name_or_path):
        return name_or_path
    path = _bundled_font_path(name_or_path)
    if path:
        return path
    path = _scan_system_fonts().get(name_or_path)
    if path:
        return path
    return _imported_fonts().get(name_or_path)


def resolve_font(name_or_path, size):
    """Resolve a bundled name | system family | absolute path to a cached
    ImageFont.FreeTypeFont at `size`, keyed by (name_or_path, size) so a repeated
    call never re-touches disk or re-scans. Falls back to the default bundled face
    on anything missing or malformed - a render must never crash on a bad font."""
    key = (name_or_path, size)
    font = _font_cache.get(key)
    if font is not None:
        return font
    if len(_font_cache) >= _FONT_CACHE_MAX:
        _font_cache.clear()   # bounded: every preview size and slider value is a new key
    path = _resolve_font_path(name_or_path) or _bundled_font_path(_DEFAULT_BUNDLED_FONT)
    font = None
    for candidate in (path, _bundled_font_path(_DEFAULT_BUNDLED_FONT)):
        if not candidate:
            continue
        try:
            font = ImageFont.truetype(candidate, size)
            break
        except (OSError, ValueError):
            continue
    if font is None:
        # Not even the bundled face is there (a broken install): Pillow's own font
        # still draws readable text instead of taking the whole editor down.
        font = ImageFont.load_default(size)
    _font_cache[key] = font
    return font


# ── Render pickup (6.4b): the editor render loop reads this, read-only ──────────────
def subtitles_for_clip(config, clip_key, transcript):
    """The render-time subtitles payload (minus `t`, which the caller fills in per
    frame) for `clip_key`, or None if subtitles aren't enabled for this clip or the
    transcript has no words to burn in. Read-only - never mutates `config`. This is
    the coordination seam between Subtitle Studio (which writes
    config["subtitles"]["per_clip"][clip_key]) and the editor's render loop (which
    reads it) - the two modules never import each other."""
    if transcript is None or not any(line.words for line in transcript.lines):
        return None
    block = ((config.get("subtitles") or {}).get("per_clip") or {}).get(clip_key)
    if not block or not block.get("enabled"):
        return None
    return {"transcript": transcript, "style": SubtitleStyle.from_dict(block.get("style")),
            "offset_ms": timing_offset_ms(config)}


def same_style(a, b):
    """True if two style dicts (or SubtitleStyles) describe the same look once
    defaults are filled in - so a dict missing a key equals one carrying that
    key's default."""
    da = (a if isinstance(a, SubtitleStyle) else SubtitleStyle.from_dict(a or {})).to_dict()
    db = (b if isinstance(b, SubtitleStyle) else SubtitleStyle.from_dict(b or {})).to_dict()
    return da == db


def clip_style(config, clip_key):
    """The style a clip was last set up with (its per-clip block), or None."""
    block = ((config.get("subtitles") or {}).get("per_clip") or {}).get(clip_key)
    if not block or not isinstance(block.get("style"), dict):
        return None
    return block["style"]


def timing_offset_ms(config):
    """The global karaoke timing offset (ms) from config. Positive shows every
    caption later, negative earlier."""
    try:
        return int((config.get("subtitles") or {}).get("timing_offset_ms", 0) or 0)
    except (TypeError, ValueError):
        return 0


def is_empty_transcript(transcript):
    """True if `transcript` is None or has no words in any line."""
    return transcript is None or not any(line.words for line in transcript.lines)


def would_shrink_to_empty(new_transcript, existing_transcript):
    """True if writing `new_transcript` would overwrite a GOOD `existing_transcript`
    (already on disk) with an empty one (6.5 P0 safety net). Pure - callers supply
    both sides (Subtitle Studio's autosave/leave-save reads `existing_transcript`
    off disk first) - an autosave must never destroy real work, no matter what
    transient state produced an empty in-memory transcript."""
    if not is_empty_transcript(new_transcript):
        return False
    return not is_empty_transcript(existing_transcript)


# ── Pure subtitle renderer (6.4a) ─────────────────────────────────────────────────────
def _hex_to_rgba(hex_str, alpha=255):
    s = (hex_str or "").lstrip("#")
    if len(s) != 6:
        return (255, 255, 255, alpha)
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return (255, 255, 255, alpha)
    return (r, g, b, alpha)


def _composite_overlay(canvas, overlay, x, y):
    """Alpha-composite a PIL RGBA `overlay` onto BGR `canvas` at (x, y), clipped to
    the canvas bounds."""
    ov = np.asarray(overlay)
    oh, ow = ov.shape[:2]
    ch, cw = canvas.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(cw, x + ow), min(ch, y + oh)
    if x1 <= x0 or y1 <= y0:
        return
    sub = ov[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.float32)
    alpha = sub[:, :, 3:4] / 255.0
    rgb_as_bgr = sub[:, :, [2, 1, 0]]
    region = canvas[y0:y1, x0:x1].astype(np.float32)
    canvas[y0:y1, x0:x1] = (rgb_as_bgr * alpha + region * (1.0 - alpha)).astype(np.uint8)


# block_alpha: None normally. A float in (0, 1] fades the WHOLE caption at that
# alpha - a segment fading in at its start, or fading out into a gap.
# render_subtitles applies it to the finished overlay, never per word, so the
# two-pass outline order across the line is preserved.
RevealResult = namedtuple("RevealResult",
                         ["words", "active", "outgoing", "block_alpha"],
                         defaults=[None, None])


def _line_at(transcript, t):
    for line in transcript.lines:
        if line.words and line.start <= t <= line.end:
            return line
    return None


def _pack_groups(words, measure, max_width_px, max_lines):
    """Greedily group `words` (time order) into GROUPS whose wrapped row count
    fits `max_lines` rows of `max_width_px` (measured with `measure(text)->px`).
    NO carry-over: the instant a word would need a row beyond max_lines, the
    current group seals and a fresh group starts with just that word - "build the
    group of words, fill the lines, reset, show the next segment" (locked design
    decision, shared by rolling + full_line). Returns list[list[Word]]."""
    if not words:
        return []
    groups = []
    group = []
    rows = [[]]
    row_width = 0.0
    space_w = measure(" ")
    for w in words:
        ww = measure(w.text)
        row = rows[-1]
        candidate_width = ww if not row else row_width + space_w + ww
        if row and candidate_width > max_width_px:
            if len(rows) < max_lines:
                rows.append([w])
                row_width = ww
            else:
                groups.append(group)
                group = []
                rows = [[w]]
                row_width = ww
        else:
            row.append(w)
            row_width = candidate_width
        group.append(w)
    if group:
        groups.append(group)
    return groups


def _group_at(groups, t):
    """The group whose span [first_word.start, next_group.first_word.start)
    contains `t` - the last group runs to +inf (the line's end already bounds the
    caller). None if `groups` is empty."""
    for i, g in enumerate(groups):
        if not g:
            continue
        lo = g[0].start
        hi = groups[i + 1][0].start if i + 1 < len(groups) else float("inf")
        if lo <= t < hi:
            return g
    return None


# 6.5c: a gap is not content, so nothing renders during one. The only hold is
# this short bridge, which exists purely so ordinary word-boundary jitter (a few
# tens of ms between consecutive words, and the small pauses inside a sentence)
# does not make the caption blink on and off. Anything a viewer would read as an
# actual pause is longer than this and clears. Replaces the old 1.5s hold, which
# was long enough to read as a word LINGERING over a silence.
_GAP_BRIDGE_S = 0.25


def _gap_result(shown, active, style, dt_past_end):
    """What to show once every word in `shown` has finished and nothing new has
    started yet - `dt_past_end` is how far past that end `t` sits.

    Within the bridge the caption simply stays (anti-flicker). After it,
    transition='fade' gets a bounded fade to nothing and transition='cut' ends
    immediately. Either way it always ends - there is no path here that holds a
    caption indefinitely."""
    if dt_past_end <= _GAP_BRIDGE_S:
        return RevealResult(words=list(shown), active=active)
    if style.transition == "fade":
        alpha = 1.0 - (dt_past_end - _GAP_BRIDGE_S) / (_FADE_MS / 1000.0)
        if alpha > 0.0:
            # `active` is preserved, not blanked: in one_word the word on screen
            # is still THE current word while it fades, and dropping it to
            # inactive would swap it from the highlight color back to the plain
            # fill for the length of the fade - a visible color blink.
            return RevealResult(words=list(shown), active=active, block_alpha=alpha)
    return RevealResult(words=[], active=None)


def _trailing_gap_result(transcript, style, t, measure, max_width_px):
    """`t` sits past the end of every line that has started - between two
    sentences, or after the last one. The most recently finished segment still
    owns the bridge and fade-out window.

    This path matters more than it looks: rebuild_lines splits on sentence-final
    punctuation, so in a real transcript most segment boundaries ARE line
    boundaries, and _line_at returns None across them. Without this the
    segment fade-out would almost never actually run."""
    prev = None
    for ln in transcript.lines:
        if ln.words and ln.end <= t and (prev is None or ln.end > prev.end):
            prev = ln
    if prev is None:
        return RevealResult(words=[], active=None)
    if t > prev.end + _GAP_BRIDGE_S + _FADE_MS / 1000.0:
        return RevealResult(words=[], active=None)   # long past it, skip the packing
    groups = _pack_groups(prev.words, measure, max_width_px, max(1, style.max_lines))
    if not groups or not groups[-1]:
        return RevealResult(words=[], active=None)
    group = groups[-1]
    return _gap_result(group, None, style, t - max(w.end for w in group))


def _segment_fade_in(style, group, t):
    """Whole-segment fade-IN alpha at a segment's start, or None.

    rolling/full_line only: one_word does its own word-to-word crossfade
    instead (_one_word_reveal_result), so a block fade there would double up.
    The fade is sequential, not an overlap - the outgoing segment finishes
    fading before the next one starts. Two whole sentences dissolving through
    each other is unreadable; one word through another is not, which is why
    one_word overlaps and this does not."""
    if style.transition != "fade" or not group:
        return None
    dur = _FADE_MS / 1000.0
    dt = t - min(w.start for w in group)
    if 0.0 <= dt < dur:
        return max(0.0, min(1.0, dt / dur))
    return None


def _one_word_reveal_result(words, i, style, t):
    """RevealResult for one_word style when `words[i]` is the active word at
    time `t` - also resolves the fade transition's outgoing (previous) word,
    so render_subtitles can draw a real crossfade instead of a hard swap."""
    active = words[i]
    outgoing = None
    if style.transition == "fade" and i > 0:
        dt = t - active.start
        # Only crossfade from a word that was still ON SCREEN when this one
        # started. Across a gap the previous word has already cleared (bridge,
        # then fade, then nothing), so fading it back in is a ghost of a word
        # the viewer stopped seeing seconds ago - which is exactly what showed
        # up as an old caption faintly overlapping a new one (6.5c). After a
        # gap the incoming word simply fades in on its own.
        gap = active.start - words[i - 1].end
        if 0 <= dt < _FADE_MS / 1000.0 and gap <= _GAP_BRIDGE_S:
            outgoing = words[i - 1]
    shown = [active] if outgoing is None else [outgoing, active]
    return RevealResult(words=shown, active=active, outgoing=outgoing)


def _reveal_one_word(transcript, t, style):
    """Show ONLY the word whose [start,end] contains `t`. Once that word ends
    and the next has not begun, the caption is bridged for _GAP_BRIDGE_S and
    then goes away (fading first if the style asks for it) - see _gap_result.
    Nothing shown before the first word."""
    words = sorted(transcript.words(), key=lambda w: w.start)
    if not words:
        return RevealResult(words=[], active=None)
    for i, w in enumerate(words):
        if w.start <= t <= w.end:
            return _one_word_reveal_result(words, i, style, t)
    candidates = [i for i, w in enumerate(words) if w.start <= t]
    if not candidates:
        return RevealResult(words=[], active=None)
    i = max(candidates)
    held = words[i]
    bridged = _one_word_reveal_result(words, i, style, t)
    return _gap_result(bridged.words, bridged.active, style, t - held.end)


def _event_at(transcript, t):
    """The confirmed (kind='manual') Event active at `t`, or None. A gap_candidate
    renders nothing until confirmed into a manual event, per Event's own
    docstring - this is the piece that was never actually wired up (6.5 bugfix)."""
    for ev in transcript.events:
        if ev.kind == "manual" and ev.start <= t <= ev.end:
            return ev
    return None


def resolve_reveal(transcript, style, t, *, measure, max_width_px):
    """Pure: decide which words are visible and which is active, for `style.reveal`
    (one_word | rolling | full_line). `measure(text)->px` + `max_width_px` drive the
    group packer shared by rolling/full_line - a caller doing standalone testing
    supplies its own fake measure; render_subtitles supplies the real font metrics.
    Deterministic - same inputs, same output; no wall clock, no widget reads.

    A confirmed manual Event (e.g. a labelled gap - "[*laughter*]") active at `t`
    always wins and shows as its own single caption for its whole span, regardless
    of `style.reveal` - it isn't word-timed content, so there's nothing to reveal
    word-by-word. Takes priority over an overlapping line (a labelled gap is, by
    construction, over a stretch with no transcribed words)."""
    ev = _event_at(transcript, t)
    if ev is not None:
        # effects carry across: a non-speech entry animates like any tagged word.
        w = Word(text=ev.text, start=ev.start, end=ev.end, conf=1.0,
                effects=list(ev.effects))
        return RevealResult(words=[w], active=w)
    if style.reveal == "one_word":
        return _reveal_one_word(transcript, t, style)
    line = _line_at(transcript, t)
    if line is None:
        return _trailing_gap_result(transcript, style, t, measure, max_width_px)
    groups = _pack_groups(line.words, measure, max_width_px, max(1, style.max_lines))
    group = _group_at(groups, t)
    if group is None:
        return RevealResult(words=[], active=None)
    # 6.5c: _group_at holds a group until the NEXT group starts, which over a
    # long silence between groups left a finished caption sitting on screen for
    # the whole gap. Once the group's own last word has ended, hand over to the
    # same bridge-then-clear rule every style uses.
    # 6.5c: rolling and full_line treat a SEGMENT as the unit. The cut/fade
    # transition happens at segment boundaries; inside a segment the words just
    # highlight or build up on their own timings, with no transition of their
    # own. _group_at otherwise holds a group until the NEXT group starts, which
    # over a long silence left a finished caption sitting on screen for the
    # whole gap.
    group_end = max(w.end for w in group)
    fade_in = _segment_fade_in(style, group, t)
    if style.reveal == "rolling":
        shown = [w for w in group if w.start <= t]
        active = shown[-1] if shown else None
        if t > group_end:
            return _gap_result(shown, None, style, t - group_end)
        return RevealResult(words=shown, active=active, block_alpha=fade_in)
    # full_line: every word of the spanning group at once, no build-up.
    active = next((w for w in group if w.start <= t <= w.end), None)
    if t > group_end:
        return _gap_result(group, None, style, t - group_end)
    return RevealResult(words=list(group), active=active, block_alpha=fade_in)


# ── Transcript editing rows (6.5c, pure) ─────────────────────────────────────────────
DisplayRow = namedtuple("DisplayRow", ["kind", "items", "start", "end"])
# kind "words": items = [(li, wi), ...] for the words shown together on screen.
# kind "event": items = [event_index].


_TEXT_WIDTH_CACHE = {}
_TEXT_WIDTH_CACHE_MAX = 20000


def _text_width(probe, font, text):
    """probe.textlength, memoized per (font file, size, text). The group packer
    measures every word on every call, and the same words come back on every
    frame and every transcript re-render."""
    key = (getattr(font, "path", None), getattr(font, "size", None), text)
    width = _TEXT_WIDTH_CACHE.get(key)
    if width is None:
        if key[0] is None or len(_TEXT_WIDTH_CACHE) >= _TEXT_WIDTH_CACHE_MAX:
            if key[0] is None:
                return probe.textlength(text, font=font)
            _TEXT_WIDTH_CACHE.clear()
        width = probe.textlength(text, font=font)
        _TEXT_WIDTH_CACHE[key] = width
    return width


def _gap_measure(probe, font, style):
    """A `measure(text)` that widens a plain space by the style's word_gap."""
    gap = max(0.5, float(getattr(style, "word_gap", 1.0)))

    def measure(text):
        width = _text_width(probe, font, text)
        return width * gap if text == " " else width

    return measure


def reference_packer(style):
    """(measure, max_width_px) at the 1080-wide REFERENCE resolution.

    Safe to resolve grouping here rather than at the real output size: font size
    and max_width_px both scale by the same factor at every resolution, so the
    wrap ratio - and therefore the grouping - never changes with it."""
    font = resolve_font(style.font, max(1, style.size))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    return _gap_measure(probe, font, style), style.max_width * _REF_W


def display_rows(transcript, style, measure, max_width_px):
    """The transcript's editing rows, grouped the way the CURRENT reveal style
    will actually put them on screen.

    one_word gets one row per word, because that is literally what each word
    will be: its own caption. rolling and full_line get one row per packed
    group, matching the segment that appears and clears as a unit. Events -
    confirmed non-speech entries and unconfirmed gap suggestions alike - get
    their own rows, interleaved by time.

    Pure: `measure(text) -> px` and `max_width_px` are the same inputs the
    renderer's packer takes, so the rows a user edits are the units they will
    see burned in. Rows are returned in time order."""
    rows = []
    if style.reveal == "one_word":
        for li, line in enumerate(transcript.lines):
            for wi, w in enumerate(line.words):
                rows.append(DisplayRow("words", [(li, wi)], w.start, w.end))
    else:
        max_lines = max(1, style.max_lines)
        for li, line in enumerate(transcript.lines):
            if not line.words:
                continue
            wi = 0
            for group in _pack_groups(line.words, measure, max_width_px, max_lines):
                if not group:
                    continue
                items = [(li, wi + n) for n in range(len(group))]
                wi += len(group)
                rows.append(DisplayRow("words", items,
                                      min(w.start for w in group),
                                      max(w.end for w in group)))
    for idx, ev in enumerate(transcript.events):
        rows.append(DisplayRow("event", [idx], ev.start, ev.end))
    rows.sort(key=lambda r: (r.start, r.end))
    return rows


# ── SRT sidecar (6.4c, pure) ──────────────────────────────────────────────────────────
def _srt_timestamp(seconds):
    seconds = max(0.0, seconds)
    ms_total = int(round(seconds * 1000))
    h, ms_total = divmod(ms_total, 3600000)
    m, ms_total = divmod(ms_total, 60000)
    s, ms = divmod(ms_total, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def transcript_to_srt(transcript, style, offset_ms=0):
    """Plain-text SRT sidecar (pure). Cue boundaries follow the SAME reveal-group
    packer render_subtitles uses, so cues match what the burn-in actually shows on
    screen - resolved at the 1080-wide REFERENCE resolution, which is safe because
    font size and max_width_px scale by the same factor `k` at every real output
    size, so the wrap ratio (and therefore the grouping) never changes with
    resolution. Styling, effects, and reveal animation are NOT preserved - SRT is
    plain text by format. Confirmed manual events become their own cues, as they
    do in the burn-in. `offset_ms` is the global karaoke timing offset, shifted in
    the same direction as the burn-in. Empty transcript -> empty string."""
    if not isinstance(style, SubtitleStyle):
        style = SubtitleStyle.from_dict(style)
    measure, max_width_px = reference_packer(style)
    shift = (offset_ms or 0) / 1000.0

    cues = []
    for line in transcript.lines:
        if not line.words:
            continue
        groups = _pack_groups(line.words, measure, max_width_px,
                              max(1, style.max_lines))
        for group in groups:
            if group:
                cues.append((group[0].start + shift, group[-1].end + shift,
                            " ".join(w.text for w in group)))
    for ev in transcript.events:
        if ev.kind == "manual" and ev.text.strip() and ev.end > ev.start:
            cues.append((ev.start + shift, ev.end + shift, ev.text.strip()))
    cues = [c for c in cues if c[1] > 0]
    cues.sort(key=lambda c: c[0])

    blocks = [f"{i}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{text}"
             for i, (start, end, text) in enumerate(cues, start=1)]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


# ── Active-word motion + one-word transition (6.4b) ──────────────────────────────────
# Deterministic time-functions of (t - active.start). motion == "none" (the default)
# and transition == "cut" are the guaranteed zero-cost path - render_subtitles skips
# the per-word overlay entirely when scale == 1.0 and alpha == 1.0.
_POP_MS = 120
_BUMP_MS = 160
# 6.5 C3: raised from 90ms - too quick to read as a real crossfade, easy to
# mistake for the "cut" hard swap it's meant to visibly differ from.
_FADE_MS = 220
# 6.5 live-test fix: reference-space vertical separation during a one_word
# crossfade, so two very different-length words don't dissolve into an
# illegible overlap at the exact same spot.
_FADE_DRIFT_PX = 22


def _motion_scale(style, active, t):
    """1.0 outside the motion window or with motion off. 'pop' eases 0.6 -> 1.0 over
    POP_MS at the active word's start; 'bump' swings 1.0 -> ~1.15 -> 1.0 over
    BUMP_MS (a half-sine)."""
    if active is None or style.motion == "none":
        return 1.0
    dt = t - active.start
    if style.motion == "pop":
        dur = _POP_MS / 1000.0
        if dt < 0 or dt > dur:
            return 1.0
        p = dt / dur
        eased = 1.0 - (1.0 - p) ** 2   # ease-out quad
        return 0.6 + 0.4 * eased
    if style.motion == "bump":
        dur = _BUMP_MS / 1000.0
        if dt < 0 or dt > dur:
            return 1.0
        p = dt / dur
        return 1.0 + 0.15 * math.sin(p * math.pi)
    return 1.0


def _transition_alpha(style, active, t):
    """one_word only: 0 -> 1 over FADE_MS at the active word's start when
    transition == 'fade'; full alpha immediately ('cut', the default, or any
    other reveal style)."""
    if active is None or style.reveal != "one_word" or style.transition != "fade":
        return 1.0
    dt = t - active.start
    dur = _FADE_MS / 1000.0
    if dt < 0:
        return 0.0
    if dt >= dur:
        return 1.0
    return dt / dur


def _draw_motion_word(overlay_img, word, x, y, ww, line_height, font, fill_col,
                      outline_rgba, outline_w, scale, alpha, dx=0.0, dy=0.0):
    """Render `word` ALONE onto its own small overlay (two-pass is trivially
    satisfied - a single word cannot smear into itself), scale it about its own
    center (active-word motion or the size-pop effect), multiply its alpha (fade
    transition), and shift it by (dx, dy) (the shake effect), then paste it onto
    `overlay_img` - neighbors never reflow. Any word needing this (active-word
    motion/fade OR a tagged per-word effect) takes this path; an untagged,
    non-active word never does (see the zero-cost contract in render_subtitles)."""
    pad = outline_w + 2
    w_img = max(1, int(round(ww + 2 * pad)))
    h_img = max(1, int(round(line_height + 2 * pad)))
    tmp = Image.new("RGBA", (w_img, h_img), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(tmp)
    if outline_w > 0:
        tdraw.text((pad, pad), word.text, font=font, fill=outline_rgba,
                  stroke_width=outline_w, stroke_fill=outline_rgba)
    tdraw.text((pad, pad), word.text, font=font, fill=fill_col)
    if scale != 1.0:
        new_w = max(1, int(round(w_img * scale)))
        new_h = max(1, int(round(h_img * scale)))
        tmp = tmp.resize((new_w, new_h), Image.LANCZOS)
    else:
        new_w, new_h = w_img, h_img
    if alpha < 1.0:
        arr = np.array(tmp)
        arr[:, :, 3] = (arr[:, :, 3].astype(np.float32) * alpha).astype(np.uint8)
        tmp = Image.fromarray(arr, "RGBA")
    cx = x - pad + w_img / 2.0 + dx
    cy = y - pad + h_img / 2.0 + dy
    dest_x = int(round(cx - new_w / 2.0))
    dest_y = int(round(cy - new_h / 2.0))
    overlay_img.paste(tmp, (dest_x, dest_y), tmp)


def render_word_chip(word, style, *, k=1.0):
    """A small standalone RGBA image of ONE word, styled per `style` (fill +
    outline only - no highlight/motion/effects, this is a Timing-view timeline
    chip, not a burn-in frame). Pure - cheap enough to call from the UI thread for
    the handful of words visible in a zoomed timeline window. `k` scales font/
    outline size the same way render_subtitles does."""
    if not isinstance(style, SubtitleStyle):
        style = SubtitleStyle.from_dict(style)
    scaled_size = max(1, round(style.size * k))
    font = resolve_font(style.font, scaled_size)
    outline_w = max(0, round(style.outline_width * k)) if style.outline_on else 0
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    ww = probe.textlength(word.text, font=font)
    ascent, descent = font.getmetrics()
    line_height = ascent + descent
    pad = outline_w + 2
    w_img = max(1, int(round(ww + 2 * pad)))
    h_img = max(1, int(round(line_height + 2 * pad)))
    img = Image.new("RGBA", (w_img, h_img), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outline_rgba = _hex_to_rgba(style.outline_color)
    fill_rgba = _hex_to_rgba(style.fill)
    if outline_w > 0:
        draw.text((pad, pad), word.text, font=font, fill=outline_rgba,
                 stroke_width=outline_w, stroke_fill=outline_rgba)
    draw.text((pad, pad), word.text, font=font, fill=fill_rgba)
    return img


# ── Per-word effects (6.4c) - zero-cost when Word.effects is empty ──────────────────
_SHAKE_HZ = 14.0
_SHAKE_AMPLITUDE_PX = 3.0
_SIZE_POP_MS = 150
# 6.5 C3: raised from 1.25 - tagging a word is the deliberate "emphasize THIS
# one" tool, so it needs to read as clearly stronger than the style's own
# always-on bump/pop motion (bump peaks at 1.15x), not roughly the same size.
_SIZE_POP_PEAK = 1.4


def _shake_offset(word, t, k, intensity=1.0):
    """(dx, dy) reference-scaled jitter, deterministic from t - word.start. Runs
    for as long as the word is drawn - a continuous small wobble, not a one-shot.
    `intensity` (style.effect_intensity, 6.5) linearly scales the amplitude -
    shake/pop read as "too tame" at the fixed default for some users/styles."""
    dt = t - word.start
    phase = dt * _SHAKE_HZ * 2 * math.pi
    amp = _SHAKE_AMPLITUDE_PX * k * intensity
    dx = math.sin(phase) * amp
    dy = math.cos(phase * 1.3) * amp * 0.6
    return dx, dy


def _size_pop_scale(word, t, intensity=1.0):
    """A brief size bump (1.0 -> PEAK -> 1.0, a half-sine) over SIZE_POP_MS right as
    the word appears; 1.0 outside that window. `intensity` (6.5) scales how far
    above 1.0 the peak reaches."""
    dt = t - word.start
    dur = _SIZE_POP_MS / 1000.0
    if dt < 0 or dt > dur:
        return 1.0
    p = dt / dur
    return 1.0 + (_SIZE_POP_PEAK - 1.0) * intensity * math.sin(p * math.pi)


def _word_transform(style, word, active, t, k):
    """(scale, dx, dy, alpha) combining active-word motion/transition with any
    per-word effects tagged on word.effects. Identity (1.0, 0.0, 0.0, 1.0) is the
    zero-cost signal - an untagged, non-(active-with-motion) word draws through
    the plain per-line passes with no per-word overlay.

    6.5 C3: a tagged per-word effect OVERRIDES the style's active-word motion on
    THAT word rather than stacking with it - they read as the same kind of
    "make this word pop" treatment, so combining them just doubled up instead of
    layering two distinct ideas. The active-word alpha (fade transition) still
    applies regardless - that's about revealing the word, not emphasizing it."""
    is_active = active is not None and word is active
    alpha = _transition_alpha(style, active, t) if is_active else 1.0
    effects = word.effects
    dx = dy = 0.0
    if effects:
        scale = 1.0
        intensity = style.effect_intensity
        if "shake" in effects:
            dx, dy = _shake_offset(word, t, k, intensity)
        if "size-pop" in effects:
            scale *= _size_pop_scale(word, t, intensity)
    else:
        scale = _motion_scale(style, active, t) if is_active else 1.0
    return scale, dx, dy, alpha


# ── Active-word highlight modes (6.4c): box | outline | image ───────────────────────
_highlight_image_cache = {}


def _load_highlight_image(path, w, h):
    """Decoded+resized RGBA PIL image for a highlight_mode="image" backdrop,
    cached by (path, w, h) so a render never re-decodes it per frame. None on any
    failure (missing file, bad image, anything) - the caller falls back to "box"."""
    key = (path, w, h)
    if key in _highlight_image_cache:
        return _highlight_image_cache[key]
    if len(_highlight_image_cache) >= 64:
        _highlight_image_cache.clear()   # one entry per word width: bound it
    try:
        img = Image.open(path).convert("RGBA").resize((max(1, w), max(1, h)))
    except Exception:  # noqa: BLE001 - a bad user image must never crash a render
        img = None
    _highlight_image_cache[key] = img
    return img


def _outline_color_for(style, word, active, outline_rgba, highlight_rgba):
    """The active word's stroke recolors to highlight_color when
    highlight_mode == "outline" (a cheap per-word color pick, not the temp-canvas
    overlay path - every other word/mode uses the plain outline_color)."""
    if (style.highlight_on and style.highlight_mode == "outline"
            and active is not None and word is active):
        return highlight_rgba
    return outline_rgba


def _wrap_words(draw, words, font, max_width_px, space_width=None):
    """Word-wrap `words` (in order) into rows no wider than max_width_px, using real
    glyph metrics (draw.textlength), not a char-count estimate. Returns
    list[list[(Word, width)]] - each word's width is measured ONCE here and cached,
    so the caller's placement pass never re-measures it. `space_width` overrides
    the plain space measurement so wrapping agrees with the style's word gap."""
    rows = []
    current = []
    current_width = 0.0
    if space_width is None:
        space_width = draw.textlength(" ", font=font)
    for w in words:
        ww = draw.textlength(w.text, font=font)
        candidate_width = ww if not current else current_width + space_width + ww
        if current and candidate_width > max_width_px:
            rows.append(current)
            current = [(w, ww)]
            current_width = ww
        else:
            current.append((w, ww))
            current_width = candidate_width
    if current:
        rows.append(current)
    return rows


def render_subtitles(canvas_bgr, transcript, style, t, *, out_w, out_h):
    """Draw the subtitle for output time `t` (seconds) onto canvas_bgr IN PLACE and
    return it. PURE: no widget reads, no globals except the font caches above -
    callable from the render thread. `style` is a SubtitleStyle or its dict form.

    Two-pass outline (hard requirement): every visible word's glyph STROKE is drawn
    first (as a same-color fill+stroke silhouette), THEN every word's fill on top -
    across the whole line, in two separate loops. A per-word stroke-then-fill loop
    lets a thick stroke smear over an already-drawn neighbour; this never does that.
    """
    if not isinstance(style, SubtitleStyle):
        style = SubtitleStyle.from_dict(style)

    k = out_h / _REF_H   # uniform output scale - matches compositor.py's ky (aspect is locked)
    scaled_size = max(1, round(style.size * k))
    font = resolve_font(style.font, scaled_size)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    max_width_px = style.max_width * out_w

    # The word gap has to apply everywhere a space is measured - the reveal
    # packer, the wrap, and the placement below - or the three disagree about
    # how wide a line is.
    gap_measure = _gap_measure(probe, font, style)
    reveal = resolve_reveal(transcript, style, t, measure=gap_measure,
                            max_width_px=max_width_px)
    active, outgoing = reveal.active, reveal.outgoing
    # 6.5 C3: the outgoing (fading-out) word during a one_word crossfade is
    # drawn as its own independent overlay below, centered on the incoming
    # word's slot - it must NOT go through the normal wrap/placement pass,
    # which would lay it out side-by-side with the incoming word instead of
    # overlapping it.
    words = [w for w in reveal.words if w is not outgoing]
    if not words:
        return canvas_bgr

    rows = _wrap_words(probe, words, font, max_width_px, gap_measure(" "))
    if style.max_lines > 0 and len(rows) > style.max_lines:
        rows = rows[-style.max_lines:]

    ascent, descent = font.getmetrics()
    line_height = ascent + descent
    row_step = max(1, round(line_height * 1.15))
    space_width = gap_measure(" ")

    row_words, row_widths = [], []
    for row in rows:
        cx = 0.0
        placed = []
        for w, ww in row:
            placed.append((w, cx, ww))
            cx += ww + space_width
        row_widths.append(cx - space_width if placed else 0.0)
        row_words.append(placed)

    block_width = max(row_widths) if row_widths else 0.0
    block_height = row_step * len(rows)
    block_left = style.anchor_x * out_w - block_width / 2.0
    block_top = style.anchor_y * out_h

    outline_w = max(0, round(style.outline_width * k)) if style.outline_on else 0
    pad = max(0, round(style.highlight_pad * k))
    radius = max(0, round(style.highlight_radius * k))
    shadow_dx = round(style.shadow_dx * k)
    shadow_dy = round(style.shadow_dy * k)
    margin = outline_w + pad + max(abs(shadow_dx), abs(shadow_dy)) + 4

    ov_w = max(1, int(round(block_width + 2 * margin)))
    ov_h = max(1, int(round(block_height + 2 * margin)))
    origin_x = int(round(block_left - margin))
    origin_y = int(round(block_top - margin))

    overlay = Image.new("RGBA", (ov_w, ov_h), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    fill_rgba = _hex_to_rgba(style.fill)
    outline_rgba = _hex_to_rgba(style.outline_color)
    highlight_rgba = _hex_to_rgba(style.highlight_color, style.highlight_alpha)
    shadow_rgba = _hex_to_rgba(style.shadow_color, style.shadow_alpha)
    highlight_text_rgba = _hex_to_rgba(style.highlight_text) if style.highlight_text else None

    placed_all = []
    for row_idx, placed in enumerate(row_words):
        row_width = row_widths[row_idx]
        if style.align == "left":
            row_x = 0.0
        elif style.align == "right":
            row_x = block_width - row_width
        else:
            row_x = (block_width - row_width) / 2.0
        row_y = row_idx * row_step
        for w, cx, ww in placed:
            placed_all.append((w, margin + row_x + cx, margin + row_y, ww))

    # a. active-word highlight - box | outline | image, behind the active word
    # only, never the whole line. "outline" draws nothing here (it recolors the
    # active word's own stroke in pass c instead); a missing "image" falls back
    # to "box" rather than crashing the render.
    active_entry = None
    if active is not None:
        for entry in placed_all:
            if entry[0] is active:
                active_entry = entry
                break
    if style.highlight_on and active_entry is not None:
        _, x, y, ww = active_entry
        mode = style.highlight_mode
        # 6.5c: the backdrop fades WITH its word. It used to be drawn at full
        # opacity while the text underneath faded in and out, so a solid box
        # snapped on and off around text that was still dissolving - which read
        # as the caption overlapping itself.
        box_alpha = _transition_alpha(style, active, t)
        if box_alpha < 1.0:
            hr, hg, hb, ha = highlight_rgba
            highlight_rgba = (hr, hg, hb, int(round(ha * box_alpha)))
        if mode == "image" and style.highlight_image:
            img = _load_highlight_image(style.highlight_image,
                                        int(round(ww + 2 * pad)),
                                        int(round(line_height + 2 * pad)))
            if img is not None:
                overlay.paste(img, (int(round(x - pad)), int(round(y - pad))), img)
            else:
                mode = "box"
        if mode == "box":
            box = [x - pad, y - pad, x + ww + pad, y + line_height + pad]
            odraw.rounded_rectangle(box, radius=radius, fill=highlight_rgba)

    # Per-word transforms (6.4c: active-word motion/fade + tagged shake/size-pop
    # effects). Identity == the guaranteed zero-cost path - an untagged,
    # non-(active-with-motion) word never touches the temp-canvas overlay below.
    transforms = {id(w): _word_transform(style, w, active, t, k)
                 for w, _x, _y, _ww in placed_all}
    if outgoing is not None and active is not None and id(active) in transforms:
        # 6.5 live-test fix: a pure dissolve at the SAME anchor reads as
        # illegible mush when the two words are very different lengths (the
        # shorter one's letters land squarely on top of a slice of the longer
        # one's middle). A small vertical drift keeps them physically apart for
        # most of the transition - incoming settles DOWN into place as it fades
        # in, outgoing drifts UP and away as it fades out (mirrored below).
        sc0, dx0, dy0, al0 = transforms[id(active)]
        dy0 += _FADE_DRIFT_PX * k * (1.0 - al0)
        transforms[id(active)] = (sc0, dx0, dy0, al0)
    special_ids = {wid for wid, tr in transforms.items() if tr != (1.0, 0.0, 0.0, 1.0)}
    main_entries = ([e for e in placed_all if id(e[0]) not in special_ids]
                    if special_ids else placed_all)

    # b. shadow - text fills only, offset.
    if style.shadow_on:
        for w, x, y, _ww in main_entries:
            odraw.text((x + shadow_dx, y + shadow_dy), w.text, font=font, fill=shadow_rgba)

    # c. PASS 1 - every word's stroke, across the whole line (per-word color pick
    # for the "outline" highlight mode - cheap, not a per-word overlay).
    if outline_w > 0:
        for w, x, y, _ww in main_entries:
            oc = _outline_color_for(style, w, active, outline_rgba, highlight_rgba)
            odraw.text((x, y), w.text, font=font, fill=oc,
                       stroke_width=outline_w, stroke_fill=oc)

    # d. PASS 2 - every word's fill, on top.
    for w, x, y, _ww in main_entries:
        is_active = active is not None and w is active
        col = highlight_text_rgba if (is_active and highlight_text_rgba) else fill_rgba
        odraw.text((x, y), w.text, font=font, fill=col)

    # e. every special word's own scaled/shifted/faded overlay, drawn last so it
    # sits on top - centered on (or offset from) its normal, un-scaled slot.
    for w, x, y, ww in placed_all:
        if id(w) not in special_ids:
            continue
        scale, dx, dy, alpha = transforms[id(w)]
        is_active = active is not None and w is active
        col = (highlight_text_rgba if (is_active and highlight_text_rgba) else fill_rgba)
        oc = _outline_color_for(style, w, active, outline_rgba, highlight_rgba)
        _draw_motion_word(overlay, w, x, y, ww, line_height, font, col, oc,
                          outline_w, scale, alpha, dx=dx, dy=dy)

    # f. the crossfading OUTGOING word (one_word + fade only, 6.5 C3) - an
    # independent overlay centered on the incoming word's own slot (same
    # center point, its own width), fading 1.0 -> 0.0 over the same window the
    # incoming word fades 0.0 -> 1.0 in, so the two visibly cross-fade. Drifts
    # UP as it fades (mirrors the incoming word's down-into-place drift above)
    # so differently-sized words don't dissolve into an illegible overlap.
    if outgoing is not None and active_entry is not None:
        _, ax, ay, aww = active_entry
        out_ww = probe.textlength(outgoing.text, font=font)
        out_x = ax + aww / 2.0 - out_ww / 2.0
        dt = t - active.start
        out_alpha = max(0.0, 1.0 - dt / (_FADE_MS / 1000.0))
        out_dy = -_FADE_DRIFT_PX * k * (1.0 - out_alpha)
        # Styled as a CURRENT word, not a plain one: in one_word every word on
        # screen is the active word, so the outgoing half of a crossfade keeps
        # the highlight text and outline colors. Drawing it with the plain fill
        # made the caption blink between the two colors on every word change.
        oc = _outline_color_for(style, outgoing, outgoing, outline_rgba, highlight_rgba)
        out_col = highlight_text_rgba or fill_rgba
        _draw_motion_word(overlay, outgoing, out_x, ay, out_ww, line_height, font,
                          out_col, oc, outline_w, 1.0, out_alpha, dy=out_dy)

    # g. whole-caption fade (a segment fading in at its start, or anything
    # fading out into a gap). Applied to the FINISHED overlay, deliberately not
    # per word: routing a multi-word line through the per-word overlay path
    # would draw each word's stroke and fill together, letting a thick stroke
    # smear over an already-drawn neighbour - exactly what the two-pass order
    # above exists to prevent.
    if reveal.block_alpha is not None:
        ba = max(0.0, min(1.0, reveal.block_alpha))
        if ba < 1.0:
            overlay.putalpha(overlay.getchannel("A").point(
                lambda v, _a=ba: int(v * _a)))

    _composite_overlay(canvas_bgr, overlay, origin_x, origin_y)
    return canvas_bgr
