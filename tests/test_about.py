"""Phase 4.3b - About tab (stable invariants only).

Boots a REAL AboutModule against a lightweight fake app (a plain-dict config, NEVER the
user's config.json; supporters_url="" so no network). Covers the non-timing-dependent
acceptance criteria: registry order/always_on, decoupling, seed render, pinned-first thanks,
a lane per rendered tier, and a clean on_close. The resize/auto-scroll timing path is
exercised by the build-time harness, not here (it would be flaky under update()).
"""

import copy
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shared.config as config_mod          # noqa: E402
import shared.supporters as supporters      # noqa: E402
from shared import paths as shared_paths    # noqa: E402
from shared import registry                 # noqa: E402


class _FakePaths:
    """Real assets dir (for the seed + icons) + a clean temp user-data dir, so load_local
    deterministically reads the bundled seed and never a stray runtime supporters cache."""

    def __init__(self, userdata, assets):
        self.USER_DATA_DIR = userdata
        self._assets = assets

    def get_path(self, name, create=True):
        return self._assets if name == "assets" else self.USER_DATA_DIR


class _FakeApp:
    def __init__(self):
        self.config = copy.deepcopy(config_mod.DEFAULTS)
        self.config["supporters_url"] = ""     # offline: render from seed, no thread
        self.paths = _FakePaths(tempfile.mkdtemp(),
                                shared_paths.get_path("assets", create=False))


class TestRegistry(unittest.TestCase):
    def test_about_registered_last_and_always_on(self):
        keys = list(registry.MODULE_REGISTRY)
        self.assertEqual(keys[-1], "about")
        self.assertTrue(registry.MODULE_REGISTRY["about"]["always_on"])

    def test_about_present_with_all_optional_disabled(self):
        enabled = registry.enabled_modules({"enabled_modules": {}})
        self.assertEqual(enabled, ["home", "about"])   # always_on home + about, in order

    def test_decoupled_no_cross_module_imports(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "modules", "about.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("from modules", src)
        self.assertNotIn("import modules", src)


class TestAboutModule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        try:
            cls.root = tk.Tk()
        except tk.TclError as ex:
            raise unittest.SkipTest(f"no Tk display: {ex}")
        from shared.ui_helpers import apply_dark_theme
        apply_dark_theme(cls.root)
        import modules.about as about_mod
        cls.about_mod = about_mod
        cls.about = about_mod.AboutModule(cls.root, _FakeApp())
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.about.on_close()
            cls.root.destroy()
        except Exception:
            pass

    def test_renders_from_seed(self):
        data = self.about._data
        self.assertTrue(data["tiers"]["byte"])             # seed has byte names
        self.assertEqual(self.about.app.config["supporters_url"], "")

    def test_lane_per_rendered_tier(self):
        for key in supporters.tier_render_order(self.about._data):
            self.assertIn(key, self.about._lanes)
        self.assertNotIn("terabyte", self.about._lanes)    # no TIER_META yet

    def test_pinned_first_is_oidasama(self):
        pinned = supporters.thanks_pinned(self.about._data)
        self.assertTrue(pinned)
        self.assertEqual(pinned[0]["name"], "oidasama")

    def test_on_close_cancels_jobs(self):
        # build a fresh one and close it: pump + lane jobs cleared, no exception
        about = self.about_mod.AboutModule(self.root, _FakeApp())
        self.root.update()
        about.on_close()
        self.assertIsNone(about._ui_pump_job)
        self.assertTrue(all(v is None for v in about._scroll_jobs.values()))


if __name__ == "__main__":
    unittest.main()
