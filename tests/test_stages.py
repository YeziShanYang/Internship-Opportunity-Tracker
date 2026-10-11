"""The stage boundary: the runner's verbs, and "every filter reports what it removed".

The second half is the one that earns its place. That rule is older than the
`FilterReport` shape and has been broken twice, both times identically -- a filter was
added, its HEALTH line was not, and the digest stayed plausible while a source went
blind. With one shape for all of them, the rule stops being a habit and becomes an
assertion: a filter that removed rows and emitted no report is a test failure.
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from deliver import digest, health
import run
from core import models, paths
from persist import artifacts
from process import parse_ats, suppress
from process import snapshot as snap


def _change(key: str, program_name: str = "") -> models.Change:
    return models.Change(source_id="s", kind="added", key=key, detail="d",
                         program_name=program_name)


class FilterReportTests(unittest.TestCase):
    """Every filter emits a report, including when it removed nothing."""

    def test_a_filter_that_removed_nothing_still_reports(self):
        """The useful case. Reporting zero is what distinguishes "quiet today" from
        "not running", and the difference between those two is the entire subject of
        spec 10.1."""
        kept, reports = suppress.suppress([_change("a")], muted=set(), applied={})
        self.assertEqual(len(kept), 1)
        self.assertEqual(
            {r.filter_id for r in reports},
            {suppress.DISAPPEARED, suppress.CLOSED, suppress.DUPLICATE,
             suppress.MUTED, suppress.APPLIED},
        )
        self.assertTrue(all(r.removed == 0 for r in reports))

    def test_one_posting_on_two_sources_is_listed_once(self):
        """The same opportunity reported by several sources is one thing to go and do.

        Matched on the employer's own posting link, because the text does not match: one
        real PIMCO posting read "Software Engineering Intern - Technology Analyst" on
        Simplify and "2027 Summer Intern - Technology Analyst, Software Engineering" on
        zshah, and both carried requisition R106745.
        """
        apply_url = ("https://pimco.wd1.myworkdayjobs.com/pimco-careers/job/"
                     "Austin-TX-USA/XMLNAME-2027-Summer-Intern_R106745")
        on_simplify = models.Change(
            source_id="simplify-2027", kind="added",
            key="PIMCO / Software Engineering Intern - Technology Analyst @ Austin, TX",
            detail=f"New row: Application={apply_url}")
        # The employer's own board links the same requisition without the tenant path
        # segment, which is exactly why the identity is the requisition and not the URL.
        on_board = models.Change(
            source_id="pimco-workday", kind="added",
            key="2027 Summer Intern - Technology Analyst, Software Engineering",
            detail=("New row: URL=https://pimco.wd1.myworkdayjobs.com/job/"
                    "Austin-TX-USA/XMLNAME-2027-Summer-Intern_R106745"))

        kept, reports = suppress.suppress(
            [on_simplify, on_board], muted=set(), applied={},
            aggregators=frozenset({"simplify-2027"}))

        self.assertEqual([c.source_id for c in kept], ["pimco-workday"],
                         "the employer's board outranks an aggregator's copy")
        self.assertEqual(suppress.removed_by(reports, suppress.DUPLICATE), 1)

    def test_two_postings_at_one_employer_are_not_merged(self):
        """Only a posting link dedupes, never a company page.

        A Simplify row carries `simplify.jobs/c/PIMCO` alongside its posting link, and
        keying on the company page would collapse every PIMCO posting into one row.
        """
        first = models.Change(
            source_id="simplify-2027", kind="added", key="PIMCO / SWE Intern",
            detail=("New row: Company=[PIMCO](https://simplify.jobs/c/PIMCO); "
                    "Application=[ ](https://simplify.jobs/p/"
                    "5370153e-8623-4e1d-b4f6-986a7ab410a1)"))
        second = models.Change(
            source_id="simplify-2027", kind="added", key="PIMCO / Quant Intern",
            detail=("New row: Company=[PIMCO](https://simplify.jobs/c/PIMCO); "
                    "Application=[ ](https://simplify.jobs/p/"
                    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee)"))
        kept, reports = suppress.suppress(
            [first, second], muted=set(), applied={},
            aggregators=frozenset({"simplify-2027"}))
        self.assertEqual(len(kept), 2, "two different postings at one employer")
        self.assertEqual(suppress.removed_by(reports, suppress.DUPLICATE), 0)

    def test_a_change_with_no_posting_link_is_never_deduplicated(self):
        """Absence of an identity is not evidence of sameness.

        Page-text sources carry no posting id at all, and two of them reporting on the
        same morning must both survive -- the conservative direction, since a wrong
        merge hides an opportunity outright.
        """
        a = models.Change(source_id="hrt-inside", kind="changed", key="page updated",
                          detail="text changed")
        b = models.Change(source_id="janestreet-fttp", kind="changed",
                          key="page updated", detail="text changed")
        kept, reports = suppress.suppress(
            [a, b], muted=set(), applied={}, aggregators=frozenset())
        self.assertEqual(len(kept), 2)
        self.assertEqual(suppress.removed_by(reports, suppress.DUPLICATE), 0)

    def test_a_removed_posting_is_dropped_and_counted(self):
        """Nobody can apply to a posting that has left the board.

        Dropped in `process` rather than in the renderer so it never reaches `enrich`
        or `classify`: on 2026-09-14 removals were 40 of 67 changes, and each one would
        otherwise have cost a posting fetch and a model call to describe something that
        is no longer there. The count still has to reach HEALTH -- a board shedding an
        implausible number of rows is a format problem, not a hiring freeze.
        """
        gone = models.Change(source_id="s", kind="removed", key="gone", detail="d")
        kept, reports = suppress.suppress(
            [_change("here"), gone], muted=set(), applied={})
        self.assertEqual([c.key for c in kept], ["here"])
        self.assertEqual(suppress.removed_by(reports, suppress.DISAPPEARED), 1)
        self.assertEqual(
            sum(r.removed for r in reports), 1, "the drop must be counted exactly once"
        )

    def test_a_posting_its_list_marks_closed_is_dropped_and_counted(self):
        """aprameyak and vanshb03 keep a closed posting's row and swap its link for 🔒.

        On 2026-10-11 eighteen of those reached the digest as rows with no link: ten
        closures that the differ read as edited rows, and eight closed duplicates added
        under an en-dash spelling. A closure is a removal in a different shape.
        """
        old = snap.parse_snapshot(
            "# rows\nS\tAcme / Intern @ NY\tCompany=Acme; Application/Link=[Apply](https://x.io/j/1)\n")
        new = snap.parse_snapshot(
            "# rows\nS\tAcme / Intern @ NY\tCompany=Acme; Application/Link=🔒\n"
            "S\tAcme / Intern – Data @ NY\tCompany=Acme; Application/Link=🔒\n"
            "S\tBeta / Intern @ SF\tCompany=Beta; Application/Link=[Apply](https://y.io/j/2)\n")
        changes = snap.diff_snapshots("s", old, new)
        self.assertEqual(len(changes), 3)
        kept, reports = suppress.suppress(changes, muted=set(), applied={})
        self.assertEqual([c.key for c in kept], ["Beta / Intern @ SF"])
        self.assertEqual(suppress.removed_by(reports, suppress.CLOSED), 2)

    def test_a_padlock_beside_a_live_link_is_not_a_closure(self):
        """The link is the test, not the emoji: a row that still links somewhere has
        something to apply to."""
        self.assertFalse(snap.is_closed("Company=Acme; Status=🔒 soon; [Apply](https://x.io/j/1)"))
        self.assertTrue(snap.is_closed("Company=Acme; Application/Link=🔒"))

    def test_every_report_accounts_for_exactly_what_it_dropped(self):
        changes = [_change("a", "Muted One"), _change("b", "Live One"), _change("c")]
        kept, reports = suppress.suppress(
            changes, muted={"Muted One"}, applied={})
        self.assertEqual([c.key for c in kept], ["b", "c"])
        self.assertEqual(suppress.removed_by(reports, suppress.MUTED), 1)
        self.assertEqual(sum(r.removed for r in reports), len(changes) - len(kept))

    def test_a_report_carries_samples_so_an_over_matching_filter_can_be_audited(self):
        changes = [_change(f"row-{n}", "Muted One") for n in range(9)]
        _, reports = suppress.suppress(changes, muted={"Muted One"}, applied={})
        muted = next(r for r in reports if r.filter_id == suppress.MUTED)
        self.assertEqual(muted.removed, 9)
        self.assertEqual(len(muted.samples), suppress.MAX_SAMPLES)
        self.assertEqual(muted.samples[0], "row-0")

    def test_a_reason_is_present_on_every_report(self):
        """HEALTH prints it verbatim. A report with no sentence is a count with no
        explanation, which is what the old `before - after` subtractions were."""
        _, reports = suppress.suppress([_change("a")], muted=set(), applied={})
        for report in reports:
            self.assertTrue(report.reason.strip(), report)
            self.assertEqual(report.stage, "process")

    def test_the_applied_filter_keys_on_the_cycle_hash_not_the_row_text(self):
        from core import clock
        change = _change("SWE Intern")
        key = clock.change_key(change.source_id, change.key)
        kept, reports = suppress.suppress([change], muted=set(), applied={key: "2026-01-01"})
        self.assertEqual(kept, [])
        self.assertEqual(suppress.removed_by(reports, suppress.APPLIED), 1)

    def test_a_source_with_no_programme_is_never_muted(self):
        """An empty program_names is a whole job board, not a muted programme. Muting
        one of the 187 programmes must not silence a board that names none of them."""
        kept, _ = suppress.suppress([_change("a")], muted={"Anything"}, applied={})
        self.assertEqual(len(kept), 1)


class LocationScreenTests(unittest.TestCase):
    """An unrecognised location is kept; only a clearly foreign one drops a row.

    Measured 2026-09-30: the code had been keeping only recognised US locations, and
    silently dropped Kleiner Perkins' 2027 Engineering Fellow at "Bay Area Offices".
    """

    def _us(self, location):
        return parse_ats.is_us(models.Posting(
            title="Intern", location=location, department="", employment_type="",
            url="", text=""))

    def test_unrecognised_us_places_are_kept(self):
        for place in ("Bay Area Offices", "Des Moines", "Berkeley", "London, NYC",
                      "New London, CT", ""):
            self.assertTrue(self._us(place), place)

    def test_clearly_foreign_places_are_dropped(self):
        for place in ("London", "Beijing OR Shanghai", "Europe", "APAC", "Toronto, Ontario"):
            self.assertFalse(self._us(place), place)


class FilterLineTests(unittest.TestCase):
    """How filter reports read in HEALTH."""

    def _reports(self, n: int) -> list[models.FilterReport]:
        return [
            models.FilterReport(
                stage="process", filter_id="ats-not-a-student-role",
                source_id=f"board-{i}", considered=40, removed=30,
                reason="title gives no sign of a student role")
            for i in range(n)
        ]

    def test_a_filter_firing_on_many_sources_reports_a_count_not_a_list(self):
        """Measured on the live watchlist: the student-role screen legitimately fires
        on 62 boards, and naming all 62 is 700 characters nobody reads to the end of.
        A truncated list is worse than a count -- an arbitrary first four reads as if
        the filter only touched those."""
        line = health.filter_lines(self._reports(62))[0]
        self.assertIn("across 62 sources", line)
        self.assertNotIn("board-0", line)
        self.assertIn("1860 of 2480 rows removed", line)

    def test_a_filter_firing_on_a_few_sources_names_them(self):
        line = health.filter_lines(self._reports(2))[0]
        self.assertIn("board-0, board-1", line)

    def test_a_run_wide_filter_needs_no_source_list(self):
        line = health.filter_lines([models.FilterReport(
            stage="process", filter_id="muted-programme", considered=9, removed=3,
            reason="muted in programs.csv")])[0]
        self.assertIn("3 of 9 rows removed —", line)


class ChangeSetArtifactTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(setattr, paths, "RUN_DIR", paths.RUN_DIR)
        paths.RUN_DIR = pathlib.Path(tmp.name) / ".run"

    def test_the_snapshot_text_never_reaches_the_artifact(self):
        """22KB of TSV per source would make changes.json unreadable, and the snapshot
        already has a home in data/snapshots/ where git tracks it. An artifact that
        cannot be read in one look is not doing its job."""
        result = models.SourceResult(
            source_id="s", ok=True, snapshot_text="# rows\n" + "x" * 20_000)
        metrics = models.SourceMetrics.of(result)
        self.assertNotIn("snapshot_text", [f for f in vars(metrics)])
        artifacts.write(artifacts.CHANGES, "changes",
                        models.ChangeSet(metrics=[metrics]))
        self.assertNotIn("xxxx", artifacts.read_text(artifacts.CHANGES))

    def test_metrics_keep_failure_and_quiet_distinguishable(self):
        """SourceMetrics is a projection, and the one thing it may never flatten is
        the difference between a source that failed and one that had nothing to say."""
        failed = models.SourceMetrics.of(
            models.SourceResult(source_id="a", ok=False, error="HTTP 500"))
        quiet = models.SourceMetrics.of(models.SourceResult(source_id="b", ok=True))
        self.assertNotEqual((failed.ok, failed.error), (quiet.ok, quiet.error))
        self.assertEqual(quiet.change_count, 0)
        self.assertTrue(quiet.ok)

    def test_the_change_set_round_trips_through_the_artifact(self):
        original = models.ChangeSet(
            changes=[_change("a")],
            metrics=[models.SourceMetrics(source_id="s", ok=True, change_count=1)],
            filters=[models.FilterReport("process", "muted-programme", 3, 2,
                                         samples=("x", "y"))],
        )
        artifacts.write(artifacts.CHANGES, "changes", original)
        self.assertEqual(
            artifacts.read(artifacts.CHANGES, "changes", models.ChangeSet), original)


class RunnerTests(unittest.TestCase):
    """The verbs exist, and a verb that cannot run yet says so loudly."""

    def _run(self, argv: list[str]) -> tuple[int, str]:
        """Run the CLI with stderr captured. The message is the behaviour under test,
        and a suite that prints eight copies of it is a suite people stop reading."""
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            code = run.main(argv)
        return code, captured.getvalue()

    def test_every_stage_has_a_verb(self):
        self.assertEqual(
            run.STAGES,
            ("gather", "process", "enrich", "screen", "classify", "render", "deliver"))

    def test_every_verb_has_a_handler(self):
        """No verb may fall through. A stage that exits 0 having done nothing is the
        same failure shape as a source that goes quiet instead of failing, so the
        fall-through case is an AssertionError rather than a silent success."""
        import inspect

        from jobs import daily
        source = inspect.getsource(run.main)
        for verb in run.STAGES:
            with self.subTest(verb):
                self.assertIn(f'args.verb == "{verb}"', source)
                self.assertTrue(hasattr(daily, f"{verb}_only"), verb)
        self.assertIn("unreachable", source)

    def test_a_verb_reading_a_missing_artifact_says_which_stage_to_run(self):
        """Running `classify` before `enrich` has to name the stage, not raise a
        KeyError about a file nobody mentioned."""
        from core import codec
        from persist import artifacts as arts

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(setattr, paths, "RUN_DIR", paths.RUN_DIR)
        paths.RUN_DIR = pathlib.Path(tmp.name) / ".run"
        with self.assertRaises(codec.ArtifactError) as caught:
            arts.read(arts.ENRICHED, "enriched", dict[str, str])
        self.assertIn("run.py", str(caught.exception))

    def test_an_unknown_verb_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run.main(["gathr"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class StageRegistryTests(unittest.TestCase):
    """The two method tables must agree, and must cover the real sources.csv.

    A method in `gather.collect.CLIENT_FOR` and not in `process.build.ASSESSORS` is a
    source that fetches every morning and is never read; the reverse is a source that
    is read and never fetched. Either one is silently unwatched, which is the exact bug
    the registry replaced -- a typo'd method used to be a bare `continue`.
    """

    def test_the_gather_and_process_tables_cover_the_same_methods(self):
        from gather import collect
        from process import build as process_build

        self.assertEqual(set(collect.CLIENT_FOR), set(process_build.ASSESSORS))

    def test_the_github_credential_reaches_only_github(self):
        """Sharing one client would send GH_PAT to boards-api.greenhouse.io,
        api.lever.co, api.ashbyhq.com and every firm's marketing site."""
        from gather import collect

        needs_github = {m for m, c in collect.CLIENT_FOR.items()
                        if c == collect.GITHUB_CLIENT}
        self.assertEqual(needs_github, {"github_readme"})


class ProcessStageTests(unittest.TestCase):
    """The three non-failures that must stay distinguishable from a failure."""

    SOURCE = {"source_id": "s", "method": "page_text", "url": "https://x.test/a",
              "program_names": "P"}

    def _assess(self, attempt, body=b""):
        from process import build as process_build
        return process_build.assess_one(self.SOURCE, attempt, body)

    def test_a_quarantined_attempt_is_not_a_quiet_day(self):
        result = self._assess(models.FetchAttempt(
            source_id="s", ok=False, quarantined=True, error="quarantined for 6h"))
        self.assertFalse(result.ok)
        self.assertTrue(result.quarantined)
        self.assertIn("quarantined", result.error)
        self.assertEqual(result.changes, [])
        self.assertIsNone(result.snapshot_text,
                          "nothing was fetched, so nothing may be written back")

    def test_an_unhandled_method_is_a_failure_not_a_skip(self):
        from process import build as process_build
        result = process_build.assess_one(
            {"source_id": "typo", "method": "githb_readme", "url": "x"},
            models.FetchAttempt(
                source_id="typo", ok=False,
                error="sources.csv sets method='githb_readme', which no module handles"),
            None)
        self.assertFalse(result.ok)
        self.assertIn("no module handles", result.error)

    def test_a_fetched_source_missing_from_sources_csv_is_reported(self):
        from process import build as process_build
        results = process_build.build(
            [], [models.FetchAttempt(source_id="ghost", ok=True)])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("no longer present in sources.csv", results[0].error)

    def test_a_failed_fetch_carries_its_error_through_to_the_result(self):
        result = self._assess(models.FetchAttempt(
            source_id="s", ok=False, error="ConnectError: nope", size=0))
        self.assertFalse(result.ok)
        self.assertIn("ConnectError", result.error)


class RawBodyReplayTests(unittest.TestCase):
    """A stored body plus its index row must rebuild the fetch the parser expects."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(setattr, paths, "RUN_DIR", paths.RUN_DIR)
        paths.RUN_DIR = pathlib.Path(tmp.name) / ".run"

    def test_a_page_body_rebuilds_as_html(self):
        from process import build as process_build
        attempt = models.FetchAttempt(source_id="s", ok=True, size=9)
        fetched = process_build._rebuild("page_text", attempt, b"<p>caf\xc3\xa9</p>")
        self.assertEqual(fetched.html, "<p>café</p>")

    def test_a_readme_body_recovers_the_branch_from_meta(self):
        """The branch is not in the README bytes and it is in the failure message when
        a parse floor fires ("from N bytes on branch dev")."""
        from process import build as process_build
        attempt = models.FetchAttempt(
            source_id="s", ok=True, meta={"branch": "master"})
        fetched = process_build._rebuild("github_readme", attempt, b"# hi")
        self.assertEqual((fetched.text, fetched.branch), ("# hi", "master"))

    def test_an_ats_body_recovers_the_pages_details_and_board_total(self):
        """`listed` is the count a paged board reported, and it cannot be recovered
        from the payload once parsing has screened it -- losing it is how the Phenom
        category filter briefly went silent."""
        import json as _json

        from process import build as process_build
        body = _json.dumps({"pages": [{"jobs": [{"title": "x"}]}],
                            "details": {"/p/1": {"jobPostingInfo": {}}}}).encode()
        attempt = models.FetchAttempt(
            source_id="s", ok=True, meta={"listed": "261", "planned": "100"})
        fetched = process_build._rebuild("phenom", attempt, body)
        self.assertEqual(fetched.listed, 261)
        self.assertEqual(fetched.planned, 100)
        self.assertEqual(len(fetched.pages), 1)
        self.assertIn("/p/1", fetched.details)

    def test_a_body_written_then_read_is_unchanged(self):
        raw = b"\xff\xfe<html>not utf-8</html>"
        artifacts.write_body("s", raw)
        self.assertEqual(artifacts.read_body("s"), raw)


class EnrichStageTests(unittest.TestCase):
    """The second network stage, and the one thing it must never do: turn a fetch
    failure into a relevance decision."""

    def _change(self, **kwargs) -> models.Change:
        base = dict(source_id="s", kind="added", key="SWE Intern", detail="d",
                    change_id="abc123")
        base.update(kwargs)
        return models.Change(**base)

    def test_a_body_table_is_keyed_on_change_id_not_url(self):
        """An inline ATS body has no URL to key on, and two changed rows can share one
        posting link -- a URL-keyed table can hold neither case."""
        from enrich import bodies
        table = bodies.collect([
            self._change(change_id="one", posting_text="A"),
            self._change(change_id="two", posting_text="B"),
        ])
        self.assertEqual(sorted(table), ["one", "two"])
        self.assertEqual(table["one"].text, "A")

    def test_every_change_gets_an_entry_even_with_nothing_to_fetch(self):
        """So "missing from the table" stops being ambiguous between "nothing to get"
        and "enrich never ran"."""
        from enrich import bodies
        table = bodies.collect([self._change()])
        self.assertEqual(table["abc123"].origin, bodies.ABSENT)

    def test_a_failed_fetch_stays_an_error_and_never_becomes_empty_text(self):
        """The whole reason PostingBody carries `origin` and `error`. Returning a
        failed fetch as empty text would let the classifier read "no stated
        requirements" and rule the posting out on a network problem."""
        from enrich import bodies
        body = bodies.PostingBody(
            change_id="abc123", error="ConnectError: nope", origin=bodies.FETCHED)
        self.assertEqual(
            bodies.for_change({"abc123": body}, self._change()),
            ("", "ConnectError: nope"))
        self.assertFalse(body.usable)

    def test_an_inline_body_survives_a_caller_that_skipped_enrich(self):
        """The text belongs to the Change -- an ATS returns it in the same response
        that lists the job. A classify call with no bodies table must not lose it, or a
        screen rule-out silently becomes a "no opinion" plus a wasted model call. This
        regressed once while enrich was being extracted, and the suite caught it."""
        from enrich import bodies
        change = self._change(posting_text="Rising junior required.")
        self.assertEqual(
            bodies.for_change({}, change), ("Rising junior required.", ""))

    def test_the_typed_posting_url_is_used_without_reparsing_the_detail(self):
        from enrich import bodies
        url = "https://simplify.jobs/p/00000000-0000-0000-0000-000000000001"
        self.assertEqual(bodies.needs_fetch(self._change(posting_url=url)), url)

    def test_the_stage_reports_what_it_managed(self):
        from enrich import bodies
        table = {
            "a": bodies.PostingBody("a", text="x", origin=bodies.FETCHED),
            "b": bodies.PostingBody("b", error="boom", origin=bodies.FETCHED),
            "c": bodies.PostingBody("c", text="y", origin=bodies.INLINE),
        }
        line = bodies.health_line(table)
        self.assertIn("1 of 2 bodies fetched for 3 changed row(s)", line)
        self.assertIn("could not be read", line)

    def test_nothing_fetched_means_no_health_line(self):
        """An inline-only run has nothing to report and must not print a zero."""
        from enrich import bodies
        self.assertIsNone(bodies.health_line(
            {"a": bodies.PostingBody("a", text="x", origin=bodies.INLINE)}))
