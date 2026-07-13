"""Phase 4.3a - shared/supporters.py (headless data layer).

Pure stdlib tests against the SPEC fixtures (tests/fixtures/supporters_large.json and
_min.json). No network (the offline path uses a dead port), no tkinter. The seed under
assets/ is the real bundled file.
"""

import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shared.supporters as sup            # noqa: E402
from shared import paths as shared_paths   # noqa: E402

FIX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load_fixture(name):
    with open(os.path.join(FIX_DIR, name), "r", encoding="utf-8") as f:
        return json.load(f)


class _FakePaths:
    """The two surfaces supporters.py touches: USER_DATA_DIR (cache) + get_path('assets')."""

    def __init__(self, userdata, assets):
        self.USER_DATA_DIR = userdata
        self._assets = assets

    def get_path(self, name, create=True):
        return self._assets if name == "assets" else self.USER_DATA_DIR


class TestValidate(unittest.TestCase):
    def test_tier_counts_match_fixture(self):
        raw = _load_fixture("supporters_large.json")
        data = sup.validate(raw)
        for key, arr in raw["tiers"].items():
            self.assertEqual(len(data["tiers"][key]), len(arr), key)

    def test_empty_is_usable(self):
        data = sup.validate({})
        self.assertEqual(data["tier_order"], list(sup.DEFAULT_TIER_ORDER))
        for key in sup.DEFAULT_TIER_ORDER:
            self.assertEqual(data["tiers"][key], [])
            self.assertEqual(data["anonymous"][key], 0)
        self.assertEqual(data["thanks"], [])

    def test_garbage_does_not_raise(self):
        for junk in (None, [], 7, "x", {"tiers": "nope", "thanks": 3, "anonymous": []}):
            data = sup.validate(junk)
            self.assertIn("tiers", data)
            self.assertIsInstance(data["thanks"], list)

    def test_bare_string_entries_normalized(self):
        data = sup.validate({"tiers": {"byte": ["solo", {"name": "  spaced  "}, {"nope": 1}]}})
        names = [e["name"] for e in data["tiers"]["byte"]]
        self.assertEqual(names, ["solo", "spaced"])   # bad entry dropped, name trimmed


class TestShuffle(unittest.TestCase):
    def setUp(self):
        self.data = sup.validate(_load_fixture("supporters_large.json"))
        self.byte_names = {e["name"] for e in self.data["tiers"]["byte"]}

    def test_name_set_stable_every_call(self):
        for _ in range(5):
            got = {e["name"] for e in sup.shuffled_tier(self.data, "byte")}
            self.assertEqual(got, self.byte_names)

    def test_order_varies_across_calls(self):
        orders = {tuple(e["name"] for e in sup.shuffled_tier(self.data, "byte"))
                  for _ in range(20)}
        self.assertGreater(len(orders), 1)   # not frozen

    def test_does_not_mutate_source(self):
        before = [e["name"] for e in self.data["tiers"]["byte"]]
        sup.shuffled_tier(self.data, "byte", rng=random.Random(1))
        after = [e["name"] for e in self.data["tiers"]["byte"]]
        self.assertEqual(before, after)


class TestThanks(unittest.TestCase):
    def setUp(self):
        self.data = sup.validate(_load_fixture("supporters_large.json"))

    def test_pinned_first_and_stable(self):
        pinned = sup.thanks_pinned(self.data)
        names = [e["name"] for e in pinned]
        self.assertEqual(names[0], "oidasama")
        self.assertEqual(names, [e["name"] for e in sup.thanks_pinned(self.data)])  # stable

    def test_pinned_not_in_groups(self):
        pinned_names = {e["name"] for e in sup.thanks_pinned(self.data)}
        groups = sup.thanks_groups(self.data)
        for role, entries in groups.items():
            for e in entries:
                self.assertNotIn(e["name"], pinned_names)

    def test_groups_only_known_roles(self):
        groups = sup.thanks_groups(self.data)
        self.assertEqual(set(groups), set(sup.THANKS_ROLES))

    def test_unknown_role_buckets_friend(self):
        data = sup.validate({"thanks": [{"name": "mystery", "role": "wizard"}]})
        groups = sup.thanks_groups(data)
        self.assertIn("mystery", [e["name"] for e in groups["friend"]])
        self.assertEqual(groups["streamer"], [])
        self.assertEqual(groups["tester"], [])


class TestMergeImmortal(unittest.TestCase):
    def test_immortal_kept_mortal_dropped_new_added(self):
        old = sup.validate({"tiers": {
            "megabyte": [{"name": "mega_keep", "immortal": True}],
            "byte": [{"name": "gone_guy"}],
        }})
        new = sup.validate({"tiers": {
            "megabyte": [],
            "byte": [{"name": "fresh_face"}],
        }})
        merged = sup.merge_immortal(old, new)
        mega = {e["name"] for e in merged["tiers"]["megabyte"]}
        byte = {e["name"] for e in merged["tiers"]["byte"]}
        self.assertIn("mega_keep", mega)        # immortal survived the resync
        self.assertNotIn("gone_guy", byte)      # non-immortal stays dropped
        self.assertIn("fresh_face", byte)       # brand-new name present

    def test_megabyte_tier_immortal_even_without_flag(self):
        # any megabyte entry is immortal by tier membership, even without immortal=True
        old = sup.validate({"tiers": {"megabyte": [{"name": "no_flag"}]}})
        new = sup.validate({"tiers": {"megabyte": []}})
        merged = sup.merge_immortal(old, new)
        self.assertIn("no_flag", {e["name"] for e in merged["tiers"]["megabyte"]})


class TestLocalLoad(unittest.TestCase):
    def setUp(self):
        self.assets = shared_paths.get_path("assets", create=False)

    def test_no_cache_returns_seed(self):
        with tempfile.TemporaryDirectory() as td:
            data = sup.load_local(_FakePaths(td, self.assets))
        seed = sup.validate(_load_fixture_seed(self.assets))
        self.assertEqual({e["name"] for e in data["tiers"]["byte"]},
                         {e["name"] for e in seed["tiers"]["byte"]})

    def test_corrupt_cache_falls_back_to_seed(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "supporters_cache.json"), "w", encoding="utf-8") as f:
                f.write("{ this is not json ]")
            data = sup.load_local(_FakePaths(td, self.assets))
        self.assertTrue(data["tiers"]["byte"])      # got the seed, no raise

    def test_valid_cache_preferred_over_seed(self):
        with tempfile.TemporaryDirectory() as td:
            cache = {"tier_order": list(sup.DEFAULT_TIER_ORDER),
                     "tiers": {"byte": [{"name": "cache_only"}]}}
            with open(os.path.join(td, "supporters_cache.json"), "w", encoding="utf-8") as f:
                json.dump(cache, f)
            data = sup.load_local(_FakePaths(td, self.assets))
        self.assertEqual([e["name"] for e in data["tiers"]["byte"]], ["cache_only"])

    def test_load_cache_no_seed_fallback(self):
        # load_cache must NOT fall back to the seed (so seed placeholders can't leak into a merge)
        with tempfile.TemporaryDirectory() as td:
            data = sup.load_cache(_FakePaths(td, self.assets))
        self.assertEqual(data["tiers"]["byte"], [])
        self.assertEqual(data["tiers"]["gigabyte"], [])

    def test_load_cache_returns_cache(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "supporters_cache.json"), "w", encoding="utf-8") as f:
                json.dump({"tiers": {"byte": [{"name": "cached"}]}}, f)
            data = sup.load_cache(_FakePaths(td, self.assets))
        self.assertEqual([e["name"] for e in data["tiers"]["byte"]], ["cached"])

    def test_save_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            fp = _FakePaths(td, self.assets)
            sup.save_cache(fp, sup.validate({"tiers": {"byte": [{"name": "written"}]}}))
            data = sup.load_local(fp)
        self.assertEqual([e["name"] for e in data["tiers"]["byte"]], ["written"])


class TestFetchOffline(unittest.TestCase):
    def test_fetch_raises_offline(self):
        with self.assertRaises(Exception):
            sup.fetch("http://127.0.0.1:9/none", timeout=1)

    def test_load_local_still_valid_when_offline(self):
        # the offline failure must not poison the local path
        assets = shared_paths.get_path("assets", create=False)
        with tempfile.TemporaryDirectory() as td:
            data = sup.load_local(_FakePaths(td, assets))
        self.assertIn("tiers", data)


class TestRenderOrderAndSeed(unittest.TestCase):
    def test_tier_render_order_skips_terabyte(self):
        data = sup.validate(_load_fixture("supporters_large.json"))
        order = sup.tier_render_order(data)
        self.assertNotIn("terabyte", order)               # no TIER_META yet
        self.assertEqual(order, ["gigabyte", "megabyte", "kilobyte", "byte"])

    def test_seed_file_is_valid(self):
        assets = shared_paths.get_path("assets", create=False)
        seed = _load_fixture_seed(assets)
        data = sup.validate(seed)
        self.assertEqual([e["name"] for e in sup.thanks_pinned(data)][0], "oidasama")
        self.assertTrue(data["tiers"]["gigabyte"])        # immortal placeholder present

    def test_anonymous_count(self):
        data = sup.validate(_load_fixture("supporters_large.json"))
        self.assertEqual(sup.anonymous_count(data, "byte"), 9)
        self.assertEqual(sup.anonymous_count(data, "nope"), 0)


class TestNoTkinter(unittest.TestCase):
    def test_imports_without_tkinter(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = "import sys; import shared.supporters; assert 'tkinter' not in sys.modules"
        proc = subprocess.run([sys.executable, "-c", code], cwd=root,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


def _load_fixture_seed(assets_dir):
    with open(os.path.join(assets_dir, "supporters.seed.json"), "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    unittest.main()
