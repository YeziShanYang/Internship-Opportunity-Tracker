"""Watched pages must not report churn as news.

Relative ages must not churn the snapshot, and a page that moved only by reordering,
recounting or rotating its furniture must not print a row. Every case below is from the
2026-10-11 digest, which carried thirteen "page updated" rows and three real postings.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from process import parse_page  # noqa: E402


class BuiltInAgeTests(unittest.TestCase):
    def test_card_ages_normalise_to_one_placeholder(self):
        monday = parse_page.normalise("<div>Software Engineer Intern</div><div>11 Hours Ago Saved</div>")
        tuesday = parse_page.normalise("<div>Software Engineer Intern</div><div>Yesterday Saved</div>")
        reposted = parse_page.normalise("<div>Software Engineer Intern</div><div>Reposted 2 Days Ago Saved</div>")
        self.assertEqual(monday, tuesday)
        self.assertEqual(monday, reposted)

    def test_prose_mentioning_yesterday_is_untouched(self):
        (line,) = parse_page.normalise("<p>Applications opened yesterday for the 2027 cohort.</p>")
        self.assertIn("yesterday", line)


JOB_LIST = {"source_id": "vc", "program_names": "VC portfolio internships", "job_list": "true"}
PROGRAMME = {"source_id": "prog", "program_names": "Some Programme", "job_list": "false"}


class PageNewsTests(unittest.TestCase):
    def test_moved_blocks_are_not_news(self):
        """Lightspeed swapped two Amca cards: "40 added, 40 removed", nothing posted."""
        a = ["Amca Sourcing Intern", "El Segundo", "Amca Software Engineering Internship", "El Segundo, CA"]
        b = a[2:] + a[:2]
        self.assertEqual(parse_page.page_news(a, b), ([], []))
        self.assertEqual(parse_page.page_verdict(a, b, True), parse_page.PAGE_CHURN)
        self.assertEqual(parse_page.diff_pages("vc", a, b, JOB_LIST), [])

    def test_recounts_reshuffled_tags_and_emoji_are_not_news(self):
        old = ["689 companies. 18,442 jobs.", "Git SciPy Pandas", "\U0001f335", "Showing 12 out of 19"]
        new = ["689 companies. 18,457 jobs.", "SciPy Pandas Git", "\U0001f983", "Showing 12 out of 18"]
        self.assertEqual(parse_page.page_news(old, new), ([], []))
        self.assertEqual(parse_page.diff_pages("prog", old, new, PROGRAMME), [])

    def test_a_changed_deadline_is_news(self):
        """Digits are masked only on a line that is not a date."""
        old, new = ["Applications close October 15"], ["Applications close October 22"]
        self.assertEqual(parse_page.page_news(old, new), (new, old))
        (change,) = parse_page.diff_pages("prog", old, new, PROGRAMME)
        self.assertIn("(1 added, 1 removed)", change.key, "a programme page keeps 'page updated'")

    def test_a_job_list_is_named_by_its_new_posting(self):
        """8VC's one real change that morning, among a salary band and a removal."""
        old = ["Summer 2027 Internship - Supply Chain", "Compensation: USD 28-36 / hour", "Footer"]
        new = ["Summer 2027 Internship - Embedded Software Engineering",
               "Compensation: USD 32-46 / hour", "Footer"]
        (change,) = parse_page.diff_pages("vc", old, new, JOB_LIST)
        self.assertEqual(
            change.key, "VC portfolio internships: Summer 2027 Internship - Embedded Software Engineering")

    def test_a_job_list_that_added_no_posting_prints_nothing(self):
        """Built In rotated its ad copy; Felicis its skill tags with one tag swapped."""
        old = ["Cut your apply time in half.", "Design Patterns Git TensorFlow"]
        new = ["Track Smarter, Apply Better.", "Design Patterns Git PyTorch"]
        self.assertEqual(parse_page.page_verdict(old, new, True), parse_page.PAGE_NO_POSTING)
        self.assertEqual(parse_page.diff_pages("vc", old, new, JOB_LIST), [])
        # The same text on a programme page is all the signal there is, so it prints.
        self.assertEqual(len(parse_page.diff_pages("prog", old, new, PROGRAMME)), 1)

    def test_a_discovery_phrase_on_a_job_list_still_prints(self):
        old, new = ["Footer"], ["Freshman Discovery Day applications are live", "Footer"]
        self.assertEqual(parse_page.page_verdict(old, new, True), "")

    def test_what_counts_as_a_posting_title(self):
        """Banks say "Summer Analyst"; a card summary and a posting's own URL are not
        titles even when they contain the word."""
        url = "https://jefferies.tal.net/vx/candidate/so/pm/1/pl/2/opp/1930-2027-Equity-Sales-Internship"
        summary = "Tax interns support client engagements by preparing returns. " + "y" * 120
        self.assertEqual(
            parse_page.posting_titles([url, "2027 Equity Sales Internship", summary,
                                       "2027 Summer Analyst Program"]),
            ["2027 Equity Sales Internship", "2027 Summer Analyst Program"])

    def test_a_card_whose_title_never_says_intern_is_still_found(self):
        """a16z's board is filtered to internships server-side, and on 2026-10-07 it
        listed "Founders Associate - CEO Office": found by its card's location line."""
        old = ["45 jobs", "Heron Power", "Intern, Power Electronics", "Scotts Valley · Posted <age>"]
        new = old[:1] + ["Lio", "Founders Associate - CEO Office", "New York · Posted <age>"] + old[1:]
        (change,) = parse_page.diff_pages("vc", old, new, JOB_LIST)
        self.assertEqual(change.key, "VC portfolio internships: Lio · Founders Associate - CEO Office")


if __name__ == "__main__":
    unittest.main()
