"""The digest warns before a new hiring season makes the class-standing rules wrong."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import clock, profile  # noqa: E402
from deliver import digest  # noqa: E402

SOURCES = {
    "simplify-2027": {"method": "github_readme", "url": "SimplifyJobs/Summer2027-Internships"},
    "nuft-2027": {"method": "github_readme", "url": "northwesternfintech/2027QuantInternships"},
    "janestreet-greenhouse": {"method": "greenhouse", "url": "janestreet"},
    "old-manual-2027": {"method": "manual", "url": "https://example.com/2027"},
}


class RolloverLineTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, clock, "today_iso", clock.today_iso)

    def at(self, day: str):
        clock.today_iso = lambda: day
        return digest._rollover_line(SOURCES)

    def test_silent_inside_the_season(self):
        self.assertIsNone(self.at(f"{profile.RULES_CYCLE - 1}-10-11"))
        self.assertIsNone(self.at(f"{profile.RULES_CYCLE}-05-01"))

    def test_warns_before_the_next_season_and_names_the_sources(self):
        line = self.at(f"{profile.RULES_CYCLE}-05-20")
        self.assertIn(f"Summer {profile.RULES_CYCLE + 1}", line)
        self.assertIn("nuft-2027, simplify-2027", line)
        self.assertNotIn("janestreet", line)
        self.assertNotIn("old-manual", line, "a manual row is not fetched, so not stale")
        self.assertIn("RULES_CYCLE", line)


if __name__ == "__main__":
    unittest.main()
