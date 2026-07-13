"""Phase 5.1-comments - enforce the no-em-dash / no-AI-tells rule going forward.

Scans the SHIPPED source (main.py, shared/, modules/) for em/en dashes and a small
banned-phrase list, so drift gets caught by the suite instead of relying on a manual
sweep before each public push. tests/ and build/ are dev-only and out of scope; the
rule itself is claude-guidance/2_ARCHITECTURE.md "Public-facing cleanliness".
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # noqa: E402

BANNED_DASHES = ("—", "–")  # em dash, en dash

BANNED_PHRASES = (
    "as an ai",
    "as an ai language model",
    "as an ai model",
    "i'm claude",
    "i am claude",
    "as claude",
    "feel free to",
    "happy to help",
)


def _scan_files():
    paths = [os.path.join(ROOT, "main.py")]
    for folder in ("shared", "modules"):
        for dirpath, _dirnames, filenames in os.walk(os.path.join(ROOT, folder)):
            for name in filenames:
                if name.endswith(".py"):
                    paths.append(os.path.join(dirpath, name))
    return sorted(paths)


class TestNoAiLingo(unittest.TestCase):
    def test_no_em_or_en_dash(self):
        violations = []
        for path in _scan_files():
            with open(path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, start=1):
                    if any(ch in line for ch in BANNED_DASHES):
                        rel = os.path.relpath(path, ROOT)
                        violations.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(
            violations, [],
            "em/en dash found (use a hyphen or rephrase):\n" + "\n".join(violations),
        )

    def test_no_banned_phrases(self):
        violations = []
        for path in _scan_files():
            with open(path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, start=1):
                    lowered = line.lower()
                    for phrase in BANNED_PHRASES:
                        if phrase in lowered:
                            rel = os.path.relpath(path, ROOT)
                            violations.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(
            violations, [],
            "AI-authorship / conversational phrasing found:\n" + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
