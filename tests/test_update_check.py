"""Phase 5.3 - shared/update_check.py (headless update-check data layer).

Pure stdlib tests: no network (check() runs against a monkeypatched fetch), no tkinter.
Covers every headless acceptance criterion in SPEC_5.3.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shared.update_check as uc   # noqa: E402


class TestParseVersion(unittest.TestCase):
    def test_full_semver(self):
        self.assertEqual(uc.parse_version("1.2.0"), (1, 2, 0))

    def test_v_prefix_and_padding(self):
        self.assertEqual(uc.parse_version("v1.2"), (1, 2, 0))
        self.assertEqual(uc.parse_version("V2.0.1"), (2, 0, 1))

    def test_single_part_pads(self):
        self.assertEqual(uc.parse_version("1"), (1, 0, 0))

    def test_whitespace_tolerated(self):
        self.assertEqual(uc.parse_version("  1.0.0 "), (1, 0, 0))

    def test_invalid_inputs(self):
        for bad in ("", "abc", "1.2.x", None, "1.2.3.4", "1..2", "v", 120, "1.2-rc1"):
            self.assertIsNone(uc.parse_version(bad), f"expected None for {bad!r}")


class TestCompareVersions(unittest.TestCase):
    def test_less(self):
        self.assertEqual(uc.compare_versions((0, 9, 0), (1, 0, 0)), -1)

    def test_equal(self):
        self.assertEqual(uc.compare_versions((1, 0, 0), (1, 0, 0)), 0)

    def test_greater(self):
        self.assertEqual(uc.compare_versions((1, 0, 1), (1, 0, 0)), 1)

    def test_numeric_not_lexical(self):
        self.assertEqual(uc.compare_versions((1, 10, 0), (1, 9, 0)), 1)


class TestParseInfo(unittest.TestCase):
    def test_full_object(self):
        info = uc.parse_info(
            {"latest": "1.0.1", "download_url": "https://x/y", "notes": "n"})
        self.assertEqual(info, {"latest": "1.0.1",
                                "download_url": "https://x/y", "notes": "n"})

    def test_missing_optionals_coerce_to_empty(self):
        info = uc.parse_info({"latest": "1.0.1"})
        self.assertEqual(info["download_url"], "")
        self.assertEqual(info["notes"], "")

    def test_non_http_download_url_dropped(self):
        info = uc.parse_info({"latest": "1.0.1", "download_url": "ftp://x"})
        self.assertEqual(info["download_url"], "")

    def test_http_url_kept(self):
        info = uc.parse_info({"latest": "1.0.1", "download_url": "http://x/y"})
        self.assertEqual(info["download_url"], "http://x/y")

    def test_invalid_notes_coerce(self):
        info = uc.parse_info({"latest": "1.0.1", "notes": 42})
        self.assertEqual(info["notes"], "")

    def test_notes_clamped_to_200(self):
        info = uc.parse_info({"latest": "1.0.1", "notes": "x" * 500})
        self.assertEqual(len(info["notes"]), 200)

    def test_rejects_bad_objects(self):
        self.assertIsNone(uc.parse_info({}))
        self.assertIsNone(uc.parse_info({"latest": "nope"}))
        self.assertIsNone(uc.parse_info("junk"))
        self.assertIsNone(uc.parse_info(None))
        self.assertIsNone(uc.parse_info(["latest"]))

    def test_unknown_keys_ignored(self):
        info = uc.parse_info({"latest": "1.0.1", "sha256": "ab", "size": 5,
                              "channel": "beta"})
        self.assertIsNotNone(info)
        self.assertEqual(set(info), {"latest", "download_url", "notes"})


class TestCheck(unittest.TestCase):
    """check() with fetch monkeypatched; restores it after each test."""

    def setUp(self):
        self._real_fetch = uc.fetch

    def tearDown(self):
        uc.fetch = self._real_fetch

    def _with_fetch(self, fn, current):
        uc.fetch = fn
        return uc.check("https://example.test/version.json", current=current)

    def test_newer(self):
        result = self._with_fetch(lambda url, timeout=4: {"latest": "1.0.0"}, "0.9.0")
        self.assertEqual(result["status"], "newer")
        self.assertEqual(result["latest"], "1.0.0")

    def test_current(self):
        result = self._with_fetch(lambda url, timeout=4: {"latest": "1.0.0"}, "1.0.0")
        self.assertEqual(result["status"], "current")

    def test_ahead(self):
        result = self._with_fetch(lambda url, timeout=4: {"latest": "1.0.0"}, "1.1.0")
        self.assertEqual(result["status"], "ahead")

    def test_fetch_oserror_is_error(self):
        def boom(url, timeout=4):
            raise OSError("offline")
        result = self._with_fetch(boom, "1.0.0")
        self.assertEqual(result["status"], "error")

    def test_fetch_valueerror_is_error(self):
        def boom(url, timeout=4):
            raise ValueError("bad json")
        result = self._with_fetch(boom, "1.0.0")
        self.assertEqual(result["status"], "error")

    def test_unparsable_payload_is_error(self):
        result = self._with_fetch(lambda url, timeout=4: {"latest": "nope"}, "1.0.0")
        self.assertEqual(result["status"], "error")

    def test_unparsable_current_is_error(self):
        result = self._with_fetch(lambda url, timeout=4: {"latest": "1.0.0"}, "garbage")
        self.assertEqual(result["status"], "error")

    def test_error_result_shape(self):
        def boom(url, timeout=4):
            raise OSError("offline")
        result = self._with_fetch(boom, "1.0.0")
        self.assertEqual(result, {"status": "error", "latest": "",
                                  "download_url": "", "notes": ""})

    def test_newer_carries_url_and_notes(self):
        payload = {"latest": "1.0.1", "download_url": "https://d/l", "notes": "fixes"}
        result = self._with_fetch(lambda url, timeout=4: payload, "1.0.0")
        self.assertEqual(result["download_url"], "https://d/l")
        self.assertEqual(result["notes"], "fixes")

    def test_default_current_is_app_version(self):
        from shared import version
        uc.fetch = lambda url, timeout=4: {"latest": version.VERSION}
        try:
            self.assertEqual(uc.check("https://example.test/v.json")["status"],
                             "current")
        finally:
            uc.fetch = self._real_fetch


class TestEffectiveUrl(unittest.TestCase):
    def test_empty_override_falls_back_to_constant(self):
        self.assertEqual(uc.effective_url({"update_url": ""}), uc.DEFAULT_UPDATE_URL)

    def test_override_wins(self):
        self.assertEqual(uc.effective_url({"update_url": "https://a/b"}), "https://a/b")

    def test_missing_key_and_bad_config(self):
        self.assertEqual(uc.effective_url({}), uc.DEFAULT_UPDATE_URL)
        self.assertEqual(uc.effective_url(None), uc.DEFAULT_UPDATE_URL)


class TestImportPurity(unittest.TestCase):
    def test_no_tkinter_or_modules_import(self):
        """A fresh interpreter importing update_check must pull in neither tkinter
        nor anything from modules/ (headless data layer contract)."""
        code = ("import sys; import shared.update_check; "
                "bad = [m for m in sys.modules "
                "if m == 'tkinter' or m.startswith('tkinter.') "
                "or m == 'modules' or m.startswith('modules.')]; "
                "sys.exit(1 if bad else 0)")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
