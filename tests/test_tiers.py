"""The tier grid (process.tiers) and how classify applies it.

One test per cell of the grid, then the pieces around it: the freshman block's field
check, page changes kept out of the table, the employer table making two postings at one
firm agree, and the one-time higher cap on the first run under a new prompt.
"""
from __future__ import annotations

import unittest
import unittest.mock

import classify
from core import clock, models
from deliver import digest, urgency
from process import tiers


def judgment(firm="", role="", band="", well_known=False, key="ACME / Intern @ NYC",
             source_id="simplify-2027", **kw) -> models.Judgment:
    change_kw = {k: kw.pop(k) for k in ("backers", "structural", "is_discovery_candidate")
                 if k in kw}
    return models.Judgment(
        change=models.Change(source_id=source_id, kind="added", key=key, detail="d",
                             change_id=key, **change_kw),
        outcome=models.MODEL, relevant=True, confidence="high", firm_field=firm,
        role_field=role, size_band=band, well_known=well_known, **kw)


SMALL = {"band": "<100"}
LARGE = {"band": ">10000", "well_known": True}


class GridTests(unittest.TestCase):
    def test_target_firm(self):
        self.assertEqual(tiers.place(judgment("quant_or_ai", "ml_ai", **SMALL)),
                         (tiers.SS, tiers.TARGET_SMALL))
        self.assertEqual(tiers.place(judgment("quant_or_ai", "quant", **LARGE)),
                         (tiers.S, tiers.TARGET_LARGE), "Citadel QT")
        self.assertEqual(tiers.place(judgment("quant_or_ai", "swe", **SMALL)),
                         (tiers.S, tiers.TARGET_FIRM_SWE))
        self.assertEqual(tiers.place(judgment("quant_or_ai", "swe", **LARGE)),
                         (tiers.S, tiers.TARGET_FIRM_SWE), "Optiver SWE")

    def test_adjacent_roles_count_only_at_a_target_firm(self):
        self.assertEqual(tiers.place(judgment("quant_or_ai", "adjacent", **SMALL)),
                         (tiers.SS, tiers.TARGET_SMALL), "Scale AI operations intern")
        self.assertEqual(tiers.place(judgment("tech_or_finance", "adjacent", **SMALL)),
                         (None, tiers.OUT_FIELD))

    def test_near_target_firm_is_s(self):
        for role in ("ml_ai", "swe", "data_science"):
            with self.subTest(role):
                self.assertEqual(tiers.place(judgment("big_tech", role, **LARGE)),
                                 (tiers.S, tiers.NEAR_TARGET))

    def test_other_tech_or_finance(self):
        self.assertEqual(tiers.place(judgment("tech_or_finance", "swe", **SMALL)),
                         (tiers.A, tiers.SMALL_FIRM), "Ramp SWE")
        self.assertEqual(tiers.place(judgment("tech_or_finance", "swe", **LARGE)),
                         (tiers.B, tiers.LARGE_FIRM), "Visa SWE")
        self.assertEqual(tiers.place(judgment("tech_or_finance", "ml_ai", **SMALL)),
                         (tiers.A, tiers.SMALL_FIRM))

    def test_non_tech_keeps_only_ai_and_data_roles_at_b(self):
        for role in ("ml_ai", "data_science", "quant"):
            with self.subTest(role):
                self.assertEqual(tiers.place(judgment("other_industry", role, **LARGE)),
                                 (tiers.B, tiers.NON_TECH_ROLE))
        for role in ("swe", "cybersecurity"):
            with self.subTest(role):
                self.assertEqual(tiers.place(judgment("other_industry", role, **LARGE)),
                                 (None, tiers.OUT_NON_TECH), "Little Caesars")

    def test_out_of_field_is_ruled_out_anywhere(self):
        for firm in tiers.FIRM_FIELDS:
            with self.subTest(firm):
                self.assertEqual(tiers.place(judgment(firm, "out_of_field", **SMALL)),
                                 (None, tiers.OUT_FIELD))

    def test_cybersecurity_and_data_science_sit_with_swe(self):
        for role in ("cybersecurity", "data_science"):
            with self.subTest(role):
                self.assertEqual(tiers.place(judgment("tech_or_finance", role, **LARGE)),
                                 tiers.place(judgment("tech_or_finance", "swe", **LARGE)))

    def test_missing_labels_lower_the_tier_but_never_rule_out(self):
        self.assertEqual(tiers.place(judgment()), (tiers.A, tiers.SMALL_FIRM))


class GuardTests(unittest.TestCase):
    def test_big_tech_is_decided_by_name(self):
        self.assertEqual(tiers.guard_firm("tech_or_finance", "Tesla"), "big_tech")
        self.assertEqual(tiers.guard_firm("tech_or_finance", "Samsung Research America"),
                         "big_tech", "a subsidiary name")
        self.assertEqual(tiers.guard_firm("tech_or_finance", "Amazon Web Services"), "big_tech")
        self.assertEqual(tiers.guard_firm("big_tech", "Uber"), "tech_or_finance")
        self.assertEqual(tiers.guard_firm("tech_or_finance", "Apple Bank"), "tech_or_finance",
                         "Apple Bank is not Apple")
        self.assertEqual(tiers.guard_firm("tech_or_finance", "Meta Financial Group"),
                         "tech_or_finance")
        self.assertEqual(tiers.guard_firm("quant_or_ai", "Jane Street"), "quant_or_ai")
        self.assertEqual(tiers.guard_firm("other_industry", "Apple Bank"), "tech_or_finance",
                         "a bank by name is finance")
        self.assertEqual(tiers.guard_firm("other_industry", "Fidelity Investments"),
                         "tech_or_finance")
        self.assertEqual(tiers.guard_firm("other_industry", "Little Caesars Enterprises"),
                         "other_industry")

    def test_an_analyst_title_is_not_data_science(self):
        self.assertEqual(tiers.guard_role("data_science", "Data Analyst, Intern"),
                         "out_of_field")
        for title in ("Data Scientist Intern - Product Analytics", "Data Science & Analytics",
                      "Quantitative Analyst Intern", "Security Analyst Intern",
                      "Analytics Engineer Intern"):
            with self.subTest(title):
                self.assertEqual(tiers.guard_role("data_science", title), "data_science")
        self.assertEqual(tiers.guard_role("swe", "Software Analyst"), "swe",
                         "only a data-science label is corrected")


class SizeTests(unittest.TestCase):
    def test_an_unfamiliar_firm_is_small(self):
        self.assertTrue(tiers.is_small(judgment(band="1000-10000", well_known=False)))
        self.assertTrue(tiers.is_small(judgment()))

    def test_an_unknown_headcount_is_small_even_for_a_known_firm(self):
        self.assertTrue(tiers.is_small(judgment(band="", well_known=True)))

    def test_only_a_well_known_firm_of_1000_plus_is_large(self):
        self.assertFalse(tiers.is_small(judgment(band=">10000", well_known=True)))
        self.assertTrue(tiers.is_small(judgment(band="100-1000", well_known=True)))

    def test_a_vc_portfolio_company_is_small(self):
        self.assertTrue(tiers.is_small(judgment(band=">10000", well_known=True,
                                                backers="Sequoia Capital")))

    def test_a_hand_set_size_wins(self):
        self.assertFalse(tiers.is_small(judgment(employer_size="large")))


class UrgencyTests(unittest.TestCase):
    def test_unclassified_rows_are_b_unverified_whatever_their_labels(self):
        row = judgment("quant_or_ai", "ml_ai", **SMALL)
        row.outcome = models.UNCLASSIFIED
        self.assertEqual(urgency.rank(row), (tiers.B, tiers.UNVERIFIED))

    def test_order_is_tier_then_reason(self):
        rows = [judgment("tech_or_finance", "swe", **LARGE, key="b"),
                judgment("quant_or_ai", "ml_ai", **SMALL, key="ss"),
                judgment("big_tech", "swe", **LARGE, key="s"),
                judgment("tech_or_finance", "swe", **SMALL, key="a")]
        self.assertEqual([j.change.key for j in sorted(rows, key=urgency.order)],
                         ["ss", "s", "a", "b"])


class SettleTests(unittest.TestCase):
    SOURCES = {"simplify-2027": {"source_id": "simplify-2027", "method": "github_readme"},
               "prog": {"source_id": "prog", "method": "page_text", "job_list": "false"}}

    def test_the_grid_rules_out_with_a_short_reason(self):
        row = judgment("other_industry", "swe", **LARGE, key="Little Caesars / Innovation Intern @ Detroit")
        classify.settle([row], self.SOURCES, None, "2026-11-20")
        self.assertEqual((row.relevant, row.ruled_out_by, row.why),
                         (False, "value", tiers.OUT_NON_TECH))

    def test_a_freshman_programme_passes_the_field_check_first(self):
        """An underclassman posting in an out-of-field role must not reach the block."""
        row = judgment("other_industry", "out_of_field", **LARGE,
                       key="AISC / Fast Start Scholarship for freshmen @ Chicago")
        row.class_year = "freshman/sophomore"
        self.assertTrue(urgency.targets_underclassmen(row))
        classify.settle([row], self.SOURCES, None, "2026-11-20")
        self.assertFalse(urgency.in_top_block(row))

    def test_page_changes_skip_the_grid_and_the_table(self):
        page = judgment("other_industry", "out_of_field", key="Some Programme (2 added, 0 removed)",
                        source_id="prog", program_name="Some Programme")
        section = judgment(key="new section: truveta", structural=False)
        classify.settle([page, section], self.SOURCES, None, "2026-11-20")
        self.assertTrue(page.relevant, "a page change is not judged by the grid")
        _, body = digest.render([page, section], [], self.SOURCES)
        self.assertIn("## ■ PAGE CHANGES (2)", body)
        self.assertNotIn("## ■ OPPORTUNITIES", body)

    def test_two_postings_at_one_employer_agree_and_a_hand_override_wins(self):
        first = judgment("quant_or_ai", "ml_ai", band="<100", employer_name="Avary.ai",
                         key="Avary.ai / ML Intern @ San Jose")
        employers: dict[str, dict[str, str]] = {}
        classify.settle([first], self.SOURCES, employers, "2026-11-20")
        self.assertIn("avaryai", employers)
        second = judgment("tech_or_finance", "swe", band=">10000", well_known=True,
                          employer_name="Avary.ai", key="Avary.ai / SWE Intern @ San Jose")
        classify.settle([second], self.SOURCES, employers, "2026-11-21")
        self.assertEqual((second.firm_field, second.employer_size), ("quant_or_ai", "small"),
                         "the stored verdict replaces a later disagreeing one")
        employers["avaryai"]["override_size"] = "large"
        employers["avaryai"]["override_firm_field"] = "tech_or_finance"
        third = judgment("quant_or_ai", "swe", band="<100", employer_name="Avary.ai",
                         key="Avary.ai / QA Intern @ San Jose")
        classify.settle([third], self.SOURCES, employers, "2026-11-22")
        self.assertEqual((third.firm_field, third.employer_size), ("tech_or_finance", "large"))
        self.assertEqual(employers["avaryai"]["last_seen"], "2026-11-22")

    def test_a_workday_account_name_is_shown_as_the_real_employer(self):
        row = judgment("other_industry", "ml_ai", **LARGE, key="Globalhr / AI Intern @ Cambridge",
                       employer_name="RTX")
        row.change.url = "https://globalhr.wd5.myworkdayjobs.com/x/job/1"
        company, _ = digest.company_and_position(row)
        self.assertEqual(company, "RTX")


class FirstRunCapTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0

        def fake(provider, client, deployment, change, posting=None, effort=None):
            self.calls += 1
            return models.Judgment.from_model(change, relevant=True, why="ok",
                                              confidence="high")
        for name, value in (("select_provider", lambda: "azure"),
                            ("build_client", lambda provider: (None, "dep")),
                            ("classify_one", fake)):
            self.enterContext(unittest.mock.patch.object(classify, name, value))
        self.enterContext(unittest.mock.patch.object(clock, "today_iso", lambda: "2026-11-20"))
        self.changes = [models.Change(source_id="s", kind="added", key=f"Firm{i} / Intern",
                                      detail="d", change_id=str(i)) for i in range(300)]

    def test_the_first_run_under_a_new_prompt_gets_the_higher_cap(self):
        old = [{"key": "x|y", "prompt_version": "1", "cycle": "2027",
                "judged_on": "2026-11-19", "last_used": "2026-11-19"}]
        classify.classify(self.changes, {}, {}, {}, prior=old)
        self.assertEqual(self.calls, 300, "cap is FIRST_RUN_CAP when nothing matches")

    def test_later_runs_are_back_to_the_normal_cap(self):
        current = [{"key": "x|y", "prompt_version": classify.PROMPT_VERSION,
                    "cycle": "2027", "judged_on": "2026-11-19", "last_used": "2026-11-19"}]
        judged = classify.classify(self.changes, {}, {}, {}, prior=current)
        self.assertEqual(self.calls, classify.MAX_CLASSIFICATIONS_PER_RUN)
        over = [j for j in judged.judgments if j.outcome == models.OVER_BUDGET]
        self.assertEqual(len(over), 300 - classify.MAX_CLASSIFICATIONS_PER_RUN)


if __name__ == "__main__":
    unittest.main()
