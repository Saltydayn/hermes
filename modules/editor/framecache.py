"""LRU decoded-frame cache (extracted in 2.9.2). Verbatim move from clip_editor."""

from collections import OrderedDict

CACHE_FRAMES = 80      # LRU depth - the proven sweet spot from the old app


class FrameCache:
    """LRU cache of decoded BGR frames keyed by frame index (lifted from the old app -
    guidance 5: the single biggest scrub-performance win). Module-local for now; promote
    to shared/ if the Subtitle module later needs the same cache."""

    def __init__(self, maxsize=CACHE_FRAMES):
        self._cache = OrderedDict()
        self._maxsize = maxsize

    def get(self, key):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key, value):
        if key in self._cache:
            self._cache.move_to_end(key)
        elif len(self._cache) >= self._maxsize:
            self._cache.popitem(last=False)
        self._cache[key] = value

    def clear(self):
        self._cache.clear()
