"""Underclassman postings stay in the top block for a week, or until they leave their board.

Added 2026-09-18, bounded to `urgency.PIN_DAYS` on 2026-11-13. Every other ACT NOW test
is a *dated* reason that fires on the morning a row moves; this one is a standing fact
about who the posting is for, so the interesting cases are all about time and about
silence: a pin survives the day nothing happens to it, a pin is never retired early by a
source that failed, and every pin ages out after a week whatever its source is doing.

The size cases are here too, because the feature is only deliverable at all because of
them. Matching the whole snapshot line put 114 rows in scope against GitHub's
65,536-character issue limit; matching the row's own title puts a handful in scope.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import models
from deliver import digest, urgency

TODAY = "2026-09-18"
YESTERDAY = "2026-09-17"


def _judgment(key: str, *, relevant=True, class_year="", source_id="s",
              kind="added", change_id="", deadline="") -> models.Judgment:
    change = models.Change(source_id=source_id, kind=kind, key=key, detail="d",
                           change_id=change_id or f"{source_id}:{key}")
    return models.Judgment(change=change, outcome=models.MODEL, relevant=relevant,
                           class_year=class_year, deadline=deadline, confidence="high")


def _pin(change_id="s:Acme / First-Year Analyst", source_id="s",
         first_pinned=YESTERDAY) -> models.PinnedRow:
    return models.PinnedRow(change_id=change_id, source_id=source_id, company="Acme",
                            position="First-Year Analyst", first_pinned=first_pinned,
                            last_seen=first_pinned)


class UnderclassmanDetectionTests(unittest.TestCase):
    def test_class_year_from_the_posting_body_promotes(self):
        """The field the classifier reads off the body, which is where it usually is."""
        self.assertTrue(urgency.targets_underclassmen(
            _judgment("Acme / Analyst", class_year="freshmen and sophomores")))

    def test_row_title_promotes(self):
        self.assertTrue(urgency.targets_underclassmen(
            _judgment("Acme / First-Year Insight Program")))

    def test_section_heading_does_not_promote(self):
        """The whole reason the block is deliverable.

        `Row.identity` is section-qualified and three watched repos are underclassman
        trackers end to end, so their headings carry the word on every row. `Change.key`
        is the row's own key and excludes the heading, which is why this is the field
        the rule reads. Measured 2026-09-18: matching the full line caught 114 rows,
        110 of them from those three repos.
        """
        judgment = _judgment("Acme / Backend Engineer Intern")
        judgment.change.detail = "CS underclassmen internships\tAcme\tBackend Engineer"
        self.assertFalse(urgency.targets_underclassmen(judgment))

    def test_a_collapse_notice_is_never_pinned(self):
        """Caught on a forced re-baseline of underclassmen-cruz, 2026-09-18.

        The collapse guard reports "underclassmen-cruz: 78 of 107 rows changed at once"
        as a single `changed` row whose key is the source's own prose. It matched the
        underclassman rule on the word in the source id and pinned a board restructure
        into ACT NOW as an opportunity, where nothing would ever have retired it.
        """
        judgment = _judgment("underclassmen-cruz: 78 of 107 rows changed at once")
        judgment.change.structural = True
        self.assertFalse(urgency.targets_underclassmen(judgment))
        self.assertEqual(
            urgency.refresh_pins([judgment], [], {"s": {judgment.change.change_id}},
                                 {"s"}, TODAY, digest.company_and_position), [])

    def test_a_new_section_row_is_never_pinned(self):
        judgment = _judgment("new section: CS underclassmen internships")
        judgment.change.structural = True
        self.assertFalse(urgency.targets_underclassmen(judgment))

    def test_junior_only_row_is_not_promoted(self):
        self.assertFalse(urgency.targets_underclassmen(
            _judgment("Acme / Junior Summer Analyst", class_year="rising seniors")))

    def test_a_floor_or_an_open_door_is_not_a_target(self):
        """Narrowed 2026-09-30. The pins had filled with postings that
        mention the word without being aimed at the year: a floor, or a list that runs
        on to seniors. Those are told once, on the morning they appear, and not pinned."""
        for phrase in ('"At least sophomore standing"; "Sophomore year minimum"',
                       "current academic standing of at least Sophomore level",
                       "undergraduate (freshman, sophomore, junior, or senior)",
                       "Sophomore+"):
            self.assertFalse(urgency.targets_underclassmen(
                _judgment("Acme / Software Engineer Intern", class_year=phrase)), phrase)
        self.assertTrue(urgency.targets_underclassmen(
            _judgment("Acme / Bridge", class_year="First- & Second-Year Undergrads")))

    def test_a_carried_pin_the_rule_no_longer_accepts_is_retired(self):
        stale = models.PinnedRow(change_id="s:x", source_id="s", company="Acme",
                                 position="Software Engineer Intern",
                                 class_year="At least sophomore standing",
                                 first_pinned=YESTERDAY, last_seen=YESTERDAY)
        pins = urgency.refresh_pins([], [stale], {"s": {"s:x"}}, {"s"}, TODAY,
                                    digest.company_and_position)
        self.assertEqual(pins, [])

    def test_a_pin_past_its_deadline_is_retired_while_the_page_is_up(self):
        """Jane Street Bridge sat pinned three days past its 2026-09-27 deadline."""
        pin = _pin()
        pin.deadline = "2000-01-01"
        pins = urgency.refresh_pins([], [pin], {"s": {pin.change_id}}, {"s"}, TODAY,
                                    digest.company_and_position)
        self.assertEqual(pins, [])

    def test_an_underclassman_row_goes_in_its_own_block_not_the_high_tier(self):
        judgment = _judgment("Acme / First-Year Analyst")
        self.assertTrue(urgency.targets_underclassmen(judgment))
        self.assertFalse(urgency.is_urgent(judgment))

    def test_an_irrelevant_underclassman_row_is_not_urgent(self):
        """Relevance still gates everything. A ruled-out row prints once in RULED OUT."""
        self.assertFalse(urgency.is_urgent(
            _judgment("Acme / First-Year Analyst", relevant=False)))

    def test_a_sophomore_only_posting_is_not_a_target(self):
        """The owner is a first-year. Thrivent's "Sophomore Intern" was pinned on
        2026-10-08; "rising sophomore" is the owner's own standing and still is one."""
        self.assertFalse(urgency.targets_underclassmen(_judgment(
            "Thrivent / Associate Software Engineer - Sophomore Intern Summer 2027")))
        self.assertFalse(urgency.targets_underclassmen(
            _judgment("Acme / Analyst", class_year="current sophomores")))
        self.assertTrue(urgency.targets_underclassmen(
            _judgment("Acme / Rising Sophomore Analyst")))
        self.assertTrue(urgency.targets_underclassmen(
            _judgment("Acme / Freshman & Sophomore Program")))

    def test_a_carried_sophomore_pin_is_retired(self):
        stale = models.PinnedRow(change_id="s:x", source_id="s", company="Thrivent",
                                 position="Associate Software Engineer - Sophomore Intern",
                                 first_pinned=YESTERDAY, last_seen=YESTERDAY)
        self.assertEqual(urgency.refresh_pins([], [stale], {"s": {"s:x"}}, {"s"}, TODAY,
                                              digest.company_and_position), [])


class StanfordBlockTests(unittest.TestCase):
    """Stanford's own programme pages go in the top block, on the morning they change."""

    def _page(self, url, relevant=True):
        change = models.Change(source_id="surim-apply", kind="changed",
                               key="Stanford SURIM (2 added, 1 removed)", detail="d",
                               url=url, change_id="surim-apply:page")
        return models.Judgment(change=change, outcome=models.MODEL, relevant=relevant,
                               confidence="high", program_name="Stanford SURIM")

    def test_a_stanford_page_is_in_the_top_block_and_not_ranked(self):
        judgment = self._page("https://surim.stanford.edu/apply-surim")
        self.assertTrue(urgency.in_top_block(judgment))
        self.assertFalse(urgency.is_urgent(judgment))
        _, body = digest.render([judgment], [], {})
        self.assertIn("## ■ FOR FRESHMEN & UNDERCLASSMEN (1)", body)
        self.assertNotIn("## ■ OPPORTUNITIES", body)

    def test_the_host_must_be_stanford_not_merely_mention_it(self):
        self.assertTrue(urgency.is_stanford(self._page("https://careers.slac.stanford.edu/x")))
        self.assertFalse(urgency.is_stanford(self._page("https://stanford.edu.example.com/")))
        self.assertFalse(urgency.is_stanford(self._page("https://example.com/?u=stanford.edu")))

    def test_a_ruled_out_stanford_page_stays_in_ruled_out(self):
        self.assertFalse(urgency.in_top_block(
            self._page("https://surim.stanford.edu/", relevant=False)))

    def test_a_stanford_page_is_never_pinned(self):
        """A programme page stays up all year, so a pin would never retire."""
        judgment = self._page("https://surim.stanford.edu/apply-surim")
        self.assertEqual(urgency.refresh_pins(
            [judgment], [], {"surim-apply": {"surim-apply:page"}}, {"surim-apply"}, TODAY,
            digest.company_and_position), [])


class PinRefreshTests(unittest.TestCase):
    def test_a_new_underclassman_row_is_pinned(self):
        judgments = [_judgment("Acme / First-Year Analyst")]
        pins = urgency.refresh_pins(
            judgments, [], {"s": {"s:Acme / First-Year Analyst"}}, {"s"}, TODAY,
            digest.company_and_position)
        self.assertEqual([p.change_id for p in pins], ["s:Acme / First-Year Analyst"])
        self.assertEqual(pins[0].first_pinned, TODAY)

    def test_a_ruled_out_row_is_not_pinned(self):
        pins = urgency.refresh_pins(
            [_judgment("Acme / First-Year Analyst", relevant=False)], [],
            {"s": {"s:Acme / First-Year Analyst"}}, {"s"}, TODAY,
            digest.company_and_position)
        self.assertEqual(pins, [])

    def test_a_pin_survives_a_day_with_no_changes(self):
        """The feature. Nothing moved, the row is still on the board, it stays."""
        pins = urgency.refresh_pins(
            [], [_pin()], {"s": {"s:Acme / First-Year Analyst"}}, {"s"}, TODAY,
            digest.company_and_position)
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0].first_pinned, YESTERDAY)  # not re-dated
        self.assertEqual(pins[0].last_seen, TODAY)

    def test_a_pin_is_dropped_once_the_posting_comes_down(self):
        pins = urgency.refresh_pins(
            [], [_pin()], {"s": set()}, {"s"}, TODAY, digest.company_and_position)
        self.assertEqual(pins, [])

    def test_a_failing_source_never_retires_a_pin(self):
        """"We have stopped looking" must never render as "it closed".

        The same rule the circuit breaker follows when it insists a quarantined source
        still appears in HEALTH. `checked` is empty here, so the empty live set must
        not be read as evidence.
        """
        pins = urgency.refresh_pins(
            [], [_pin()], {"s": set()}, set(), TODAY, digest.company_and_position)
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0].last_seen, YESTERDAY)  # honest about when we last saw it

    def test_a_pin_ages_out_after_a_week(self):
        """2026-11-13: rolling postings have no close date, so a pin used to last until
        the page came down. The block reached 15 rows, the oldest 40 mornings old."""
        live = {"s": {"s:Acme / First-Year Analyst"}}
        six_days = urgency.refresh_pins(
            [], [_pin(first_pinned="2026-09-12")], live, {"s"}, TODAY,
            digest.company_and_position)
        self.assertEqual(len(six_days), 1, "the seventh morning still shows it")
        seven_days = urgency.refresh_pins(
            [], [_pin(first_pinned="2026-09-11")], live, {"s"}, TODAY,
            digest.company_and_position)
        self.assertEqual(seven_days, [])

    def test_ageing_out_applies_to_a_failing_source_too(self):
        """A failing source must not retire a pin early, but it cannot keep one forever:
        mehek-2027 failed for three weeks and held its Cognex pin the whole time."""
        pins = urgency.refresh_pins(
            [], [_pin(first_pinned="2026-09-01")], {"s": set()}, set(), TODAY,
            digest.company_and_position)
        self.assertEqual(pins, [])

    def test_a_pinned_row_that_changes_keeps_its_original_date(self):
        """A row edit is not a new posting, so it must not restart the week."""
        old = _pin(first_pinned="2026-09-11")
        moved = _judgment("Acme / First-Year Analyst", kind="changed",
                          change_id=old.change_id)
        pins = urgency.refresh_pins(
            [moved], [old], {"s": {old.change_id}}, {"s"}, TODAY,
            digest.company_and_position)
        self.assertEqual(pins, [])

    def test_a_removed_row_retires_its_pin(self):
        pins = urgency.refresh_pins(
            [_judgment("Acme / First-Year Analyst", kind="removed")], [_pin()],
            {"s": set()}, {"s"}, TODAY, digest.company_and_position)
        self.assertEqual(pins, [])

    def test_live_ids_are_yesterdays_rows_plus_todays_diff(self):
        previous = {"s": {"old", "gone"}}
        changes = [
            models.Change(source_id="s", kind="added", key="k", detail="d",
                          change_id="new"),
            models.Change(source_id="s", kind="removed", key="k", detail="d",
                          change_id="gone"),
        ]
        self.assertEqual(
            urgency.live_change_ids(previous, changes, {"s"}), {"s": {"old", "new"}})

    def test_an_unchecked_source_keeps_yesterdays_rows_verbatim(self):
        previous = {"s": {"old"}}
        changes = [models.Change(source_id="s", kind="removed", key="k", detail="d",
                                 change_id="old")]
        self.assertEqual(urgency.live_change_ids(previous, changes, set()), {"s": {"old"}})


class PinnedRenderingTests(unittest.TestCase):
    def _body(self, judgments, pinned):
        _, body = digest.render(judgments, [], {}, pinned=pinned)
        return body

    def test_a_carried_pin_renders_compactly_in_the_top_block(self):
        body = self._body([], [_pin()])
        lines = body.splitlines()
        row = next(line for line in lines if "Acme" in line)
        heading = max(i for i, line in enumerate(lines[:lines.index(row)])
                      if line.startswith("## "))
        self.assertTrue(lines[heading].startswith("## ■ FOR FRESHMEN & UNDERCLASSMEN"))
        self.assertEqual(row.count("|"), 4, "three columns: no priority in this block")
        self.assertIn(f"pinned {YESTERDAY}", row)
        self.assertNotIn("<br>", row)  # the full bullet cell is what this replaces

    def test_a_pin_is_not_printed_twice_on_the_day_it_moves(self):
        judgment = _judgment("Acme / First-Year Analyst")
        body = self._body([judgment], [_pin(change_id=judgment.change.change_id)])
        self.assertEqual(sum("Acme" in line for line in body.splitlines()), 1)

    def test_a_carried_pin_is_far_cheaper_than_a_full_row(self):
        """577 bytes a full row is what made the naive version undeliverable."""
        compact = digest._pinned_row(_pin(), TODAY)
        self.assertLess(len(compact), 200)
