"""Phase 5.5.2c - the long-file guardrail's pure tier function + mm:ss wording.

Headless: duration_tier / _mmss are module-level and touch no Tk.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.importer import duration_tier, _mmss   # noqa: E402


class TestDurationTier(unittest.TestCase):
    def test_exact_boundaries_belong_to_lower_tier(self):
        self.assertEqual(duration_tier(90), 0)
        self.assertEqual(duration_tier(90.1), 1)
        self.assertEqual(duration_tier(300), 1)
        self.assertEqual(duration_tier(300.1), 2)
        self.assertEqual(duration_tier(600), 2)
        self.assertEqual(duration_tier(600.1), 3)

    def test_representative_values(self):
        self.assertEqual(duration_tier(0), 0)
        self.assertEqual(duration_tier(30), 0)
        self.assertEqual(duration_tier(120), 1)      # 2 min
        self.assertEqual(duration_tier(444), 2)      # 7:24
        self.assertEqual(duration_tier(3600), 3)     # an hour


class TestMmss(unittest.TestCase):
    def test_wording(self):
        self.assertEqual(_mmss(444), "7:24 minutes")
        self.assertEqual(_mmss(90.1), "1:30 minutes")
        self.assertEqual(_mmss(600), "10:00 minutes")
        self.assertEqual(_mmss(3600), "60:00 minutes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
