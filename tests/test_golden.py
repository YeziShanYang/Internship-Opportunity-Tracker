"""Golden digest: the rendered bytes may not change unless someone meant them to.

This is the real safety net for a refactor this size. The unit tests assert that each
piece behaves; this asserts that the composition of all of them produces the same issue
body it produced before the code moved. Steps 4 through 9 relocate the renderer, the
urgency rule, the HEALTH strings and the filter counters, and every one of those moves
is the kind that can leave a plausible-looking digest with one line quietly missing --
which is the failure this project has been bitten by twice and cannot detect from the
inside.

Everything non-deterministic is pinned: the date, the calendar reminders, the profile
review date, and the classifier's usage and screen tallies. The calendar *content* is
replaced with fixtures on purpose. The golden is here to notice a change in the
renderer, not to make editing a reminder in calendar_reminders.py a test failure.

When a digest change is deliberate, regenerate with:

    .venv/bin/python tests/test_golden.py --update

and read the diff before committing it. That diff is the review.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import calendar_reminders
import classify
from deliver import digest, health, issue, urgency
import screen
from core import clock, models, profile

GOLDEN_DIR = pathlib.Path(__file__).resolve().parent / "golden"

TODAY = "2026-09-12"

# Fixtures, not the real reminders. See the module docstring.
FAKE_MONTH = ("September", ["A dated reminder for this month."])
FAKE_ALWAYS = ["A standing reminder that fires every month."]

SOURCES = {
    "janestreet-greenhouse": {
        "source_id": "janestreet-greenhouse", "method": "greenhouse",
        "url": "janestreet", "program_names": "Jane Street FTTP",
        "consecutive_failures": "0", "last_success": f"{TODAY}T07:00:00+00:00",
    },
    "simplify-2027": {
        "source_id": "simplify-2027", "method": "github_readme",
        "url": "SimplifyJobs/Summer2027-Internships", "signal": "low",
        "program_names": "SimplifyJobs Summer 2027 Internships",
        "consecutive_failures": "0", "last_success": f"{TODAY}T07:00:00+00:00",
    },
    "aqr-internship-program": {
        "source_id": "aqr-internship-program", "method": "page_text",
        "url": "https://www.aqr.com/About-Us/Our-Internship-Program",
        "program_names": "AQR Internship", "consecutive_failures": "4",
        "last_success": "",
    },
    "crabel-careers": {
        "source_id": "crabel-careers", "method": "page_text",
        "url": "https://www.crabel.com/careers/", "program_names": "Crabel Careers",
        "consecutive_failures": "5", "last_success": "",
    },
    "optiver-students": {
        "source_id": "optiver-students", "method": "page_text",
        "url": "https://optiver.com/working-at-optiver/career-opportunities/?level=student",
        "program_names": "Optiver FutureFocus", "consecutive_failures": "0",
        "last_success": f"{TODAY}T07:00:00+00:00",
    },
    "millennium-eightfold": {
        "source_id": "millennium-eightfold", "method": "eightfold",
        "url": "https://campusjobs.mlp.com", "program_names": "Millennium Campus",
        "consecutive_failures": "0", "last_success": "",
    },
}


def _change(**kwargs) -> models.Change:
    base = dict(source_id="janestreet-greenhouse", kind="added", key="k", detail="d")
    base.update(kwargs)
    return models.Change(**base)


def _judgment(change: models.Change, **kwargs) -> models.Judgment:
    base = dict(change=change, program_name=change.program_name, outcome=models.MODEL,
                confidence="high", relevant=True)
    base.update(kwargs)
    return models.Judgment(**base)


def scenario_full() -> dict:
    """One of everything the body can contain.

    Deliberately includes the awkward cases rather than tidy ones: an aggregator row
    whose employer arrives as a markdown link, a page-text row keyed on its programme
    name, a pipe and an over-long note, and a row whose posting the screen ruled out.
    """
    rolling = _change(
        key="Quantitative Trader @ New York", rolling=True,
        program_name="Jane Street FTTP", url="https://janestreet.test/1")
    first_year = _change(
        source_id="simplify-2027", key="[InfiniteQuant](https://simplify.jobs/c/IQ) "
                                      "/ Quantitative Trader @ NYC",
        is_discovery_candidate=True, program_name="SimplifyJobs Summer 2027 Internships",
        url="https://simplify.jobs/p/abc")
    page = _change(
        source_id="optiver-students", kind="changed",
        key="Optiver FutureFocus (3 added, 1 removed)",
        program_name="Optiver FutureFocus",
        url="https://optiver.com/working-at-optiver/career-opportunities/?level=student")
    plain = _change(
        source_id="millennium-eightfold", key="2027 Quantitative Researcher Intern | Austin",
        program_name="Millennium Campus", url="https://campusjobs.mlp.com/1")
    gone = _change(
        source_id="millennium-eightfold", kind="removed",
        key="2027 Applied AI Engineer Intern, New York",
        program_name="Millennium Campus")
    screened = _change(
        source_id="simplify-2027", key="Senior Platform Engineer @ Remote",
        program_name="SimplifyJobs Summer 2027 Internships")
    unclassified = _change(
        source_id="millennium-eightfold", key="2027 Data Engineer Intern, Miami",
        program_name="Millennium Campus")

    judgments = [
        _judgment(rolling, why="Jane Street reviews on a rolling basis and this is a "
                               "trading seat open to all undergraduate years.",
                  class_year="all undergraduate years", location="New York, NY",
                  deadline="rolling"),
        _judgment(first_year, why="Explicitly open to first- and second-year "
                                  "undergraduates, which is an unusually good match.",
                  class_year="first- and second-year undergraduates",
                  location="New York, NY", deadline="2026-09-20"),
        _judgment(page, why="The page added a 2027 FutureFocus date.",
                  location="Chicago, IL or Amsterdam, NL"),
        _judgment(plain, why="Campus board row with no class-year gate stated, so a "
                             "first-year is eligible on the face of it and the note is "
                             "long enough that it would have been clipped mid-clause "
                             "under the old 96-character budget.",
                  class_year="Bachelor's, no year stated", location="Austin, TX",
                  # High, so the table still has a High row now that blind sources
                  # are reported in HEALTH instead (2026-11-19).
                  deadline="2027-03-31", employer_size="small"),
        _judgment(gone, why="Row disappeared from the board.", confidence="low"),
        _judgment(screened, relevant=False, why='Posting requires junior standing or '
                                                'above; this owner is a rising sophomore '
                                                'for Summer 2027: "rising junior"',
                  screen_rule="advanced-standing", screen_version=1),
        models.Judgment(
            change=unclassified, program_name=unclassified.program_name,
            why="Not classified: no classifier credentials are configured (neither "
                "ANTHROPIC_API_KEY nor AZURE_OPENAI_API_KEY), so this is surfaced "
                "unjudged rather than dropped."),
    ]

    results = [
        models.SourceResult(
            source_id="janestreet-greenhouse", ok=True, changes=[rolling],
            snapshot_text="x", content_length=1,
            extra={"rows": 230, "postings": 4154,
                   "suppressed_not_student": 3900, "suppressed_not_us": 397}),
        models.SourceResult(
            source_id="simplify-2027", ok=True, changes=[first_year, screened],
            snapshot_text="x", content_length=1,
            extra={"rows": 514, "sections": 2, "collapsed": 41},
            # The typed field is what HEALTH reads now. Keeping the `extra` key too,
            # because `update_program_state` and the console summary still read it and
            # a fixture that only sets one of the two would hide a divergence.
            collapsed=models.BoardCollapse(
                total=41,
                samples=("added: Quantitative Trader @ NYC",
                         "removed: SWE Intern @ Austin")),
            # A per-source screen that reports. These three used to be a bare
            # `continue` in github_repos, so a section_include pattern that stopped
            # matching would have emptied the source in silence.
            filters=[
                models.FilterReport(
                    stage="process", filter_id="readme-section-not-included",
                    source_id="simplify-2027", considered=1402, removed=888,
                    reason="section did not match this repo's section_include pattern",
                    samples=("Data Science, AI & Machine Learning", "Hardware")),
                models.FilterReport(
                    stage="process", filter_id="readme-ignored-columns",
                    source_id="simplify-2027", considered=1402, removed=514,
                    reason="column values dropped as per-run churn rather than news",
                    samples=("Software Engineering: Age",)),
                # Reports zero, so it must NOT appear in HEALTH -- the report still
                # reaches the artifact, which is what keeps "is this filter running"
                # answerable without a dozen "0 removed" lines every morning.
                models.FilterReport(
                    stage="process", filter_id="readme-no-entity-column",
                    source_id="simplify-2027", considered=1402, removed=0,
                    reason="table has no recognisable company column"),
            ]),
        models.SourceResult(
            source_id="optiver-students", ok=True, changes=[page],
            snapshot_text="x", snapshot_ext="txt", content_length=1,
            extra={"rows": 88, "chars": 4200, "ratio": 0.0129,
                   "redirected": "redirected to a different path: "
                                 "https://optiver.com/a -> https://www.optiver.com/b. "
                                 "The fetch succeeded, but this row is no longer "
                                 "watching the page it was configured for."}),
        models.SourceResult(
            source_id="millennium-eightfold", ok=True, baseline=True,
            changes=[], snapshot_text="x", content_length=1, extra={"rows": 17}),
        # Two failing sources past the escalation threshold, so HEALTH has to carry
        # SOURCE BLIND lines and the blind count.
        models.SourceResult(
            source_id="aqr-internship-program", ok=False,
            error="redirected to what looks like an error page: "
                  "https://www.aqr.com/About-Us/Our-Internship-Program -> "
                  "https://www.aqr.com/404."),
        models.SourceResult(
            source_id="crabel-careers", ok=False, quarantined=True,
            error="quarantined for 24h after 5 consecutive failures, retry after "
                  "2026-09-13T07:00+00:00 — it has never succeeded, so check the URL "
                  "rather than waiting"),
    ]
    metrics = [models.SourceMetrics.of(r) for r in results]
    return dict(
        judgments=judgments,
        # Projected, because that is what the renderer is handed in the
        # pipeline -- a digest has to be reproducible from the artifact.
        results=metrics,
        sources=SOURCES,
        suppressed_applied=2, suppressed_muted=3,
        # Passed in rather than pinned on a module global, which is the whole point of
        # step 8: `render` composes strings from the data it was given.
        usage=models.Usage(
            provider="azure", model="gpt-5-mini", effort="minimal", calls=6,
            input_tokens=42_000, cached_input_tokens=12_000, output_tokens=9_000,
            reasoning_tokens=7_300),
        # Every report from every filter site in one list, per-source and run-wide,
        # exactly as the pipeline builds it. Passing only the run-wide half is what
        # made the per-source lines vanish from a digest rebuilt with `run.py render`.
        filters=[report for m in metrics for report in m.filters] + [
            # The screen's tally, arriving as the same FilterReport shape every other
            # filter uses instead of as a bespoke sentence off a module global.
            models.FilterReport(
                stage="screen", filter_id="screen-v1", considered=7, removed=1,
                reason="ruled out on a quoted phrase, with no model call "
                       "(advanced-standing 1). They are listed in RULED OUT with the "
                       "phrase that did it",
                samples=("Senior Platform Engineer @ Remote",)),
        ],
        discovery_lines=[
            "_Proposals only — nothing has been added._", "",
            "- **greenhouse** `drweng` — linked from simplify-2027.tsv → "
            "https://boards-api.greenhouse.io/v1/boards/drweng/jobs?content=true",
        ],
        status_only=False,
    )


def scenario_quiet() -> dict:
    """The quiet day. Title plus calendar plus health, and nothing else.

    Worth a golden of its own: this is the shape that mails most mornings, and the
    cadence rule that makes silence diagnostic depends on it staying short enough to
    archive at a glance.
    """
    return dict(
        judgments=[], results=[
            models.SourceMetrics(source_id=source_id, ok=True)
            for source_id in ("janestreet-greenhouse", "simplify-2027")
        ], sources=SOURCES, status_only=True)


SCENARIOS = {"full": scenario_full, "quiet": scenario_quiet}


class _Pinned:
    """Pin everything the digest reads that is not one of its arguments."""

    #: Well past PROFILE_REVIEW_AFTER_DAYS from TODAY, so the stale-profile nag fires
    #: and the CALENDAR block's conditional line is exercised rather than skipped.
    STALE_REVIEW = "2025-01-01"

    def pin(self):
        self.addCleanup(setattr, clock, "today_iso", clock.today_iso)
        self.addCleanup(setattr, calendar_reminders, "for_month",
                        calendar_reminders.for_month)
        self.addCleanup(setattr, calendar_reminders, "ALWAYS", calendar_reminders.ALWAYS)
        self.addCleanup(setattr, classify, "PROFILE_LAST_REVIEWED",
                        profile.PROFILE_LAST_REVIEWED)

        clock.today_iso = lambda: TODAY
        calendar_reminders.for_month = lambda when=None: (FAKE_MONTH[0], list(FAKE_MONTH[1]))
        calendar_reminders.ALWAYS = list(FAKE_ALWAYS)
        profile.PROFILE_LAST_REVIEWED = self.STALE_REVIEW





def render(name: str) -> str:
    title, body = digest.render(**SCENARIOS[name]())
    return f"{title}\n\n{body}\n"


class GoldenDigestTests(_Pinned, unittest.TestCase):
    def setUp(self):
        self.pin()

    def test_the_full_digest_is_byte_identical_to_the_golden(self):
        self._check("full")

    def test_the_quiet_digest_is_byte_identical_to_the_golden(self):
        self._check("quiet")

    def _check(self, name: str):
        path = GOLDEN_DIR / f"digest-{name}.md"
        self.assertTrue(
            path.exists(),
            f"{path} is missing. Generate it with "
            f"`.venv/bin/python tests/test_golden.py --update` and read the diff.")
        self.assertEqual(
            render(name), path.read_text(encoding="utf-8"),
            f"the rendered digest changed. If that was deliberate, regenerate "
            f"{path.name} with `python tests/test_golden.py --update` and commit the "
            f"diff as part of the change that caused it.")

    def test_the_full_scenario_actually_exercises_every_block(self):
        """A golden over a digest missing half its sections would pass forever while
        protecting nothing -- the same vacuous-gate failure as an empty glob."""
        body = render("full")
        for block in ("## ■ OPPORTUNITIES", "■ RULED OUT", "## ■ DISCOVERED",
                      "## ■ CALENDAR", "## ■ HEALTH"):
            self.assertIn(block, body, block)
        for line in ("**High** · ", "Medium · ", "SOURCE BLIND",
                     "circuit breaker has them quarantined", "were not student roles",
                     "no longer watching the page", "collapsed into one item",
                     "first run, recorded", "are muted in data/applied.tsv",
                     "readme-section-not-included: 888 of 1402 rows removed",
                     "muted=true in data/programs.csv", "screen-v1",
                     "classifier: 6 calls", "are quant and maths still the priority",
                     # The Notes bullets, so the golden cannot go on protecting a
                     # digest that quietly stopped rendering them.
                     "• **Deadline:** rolling — closes when full",
                     "• **Year:** first- and second-year undergraduates",
                     "• **Location:** Austin, TX"):
            self.assertIn(line, body, line)
        self.assertNotIn("\u2026", body, "nothing in the digest is truncated any more")

    def test_the_quiet_digest_says_no_changes_in_its_title_and_stays_short(self):
        """Quiet days mail, so the title has to carry the whole message for someone
        triaging a notification list without opening anything."""
        body = render("quiet")
        self.assertIn("(no changes)", body.splitlines()[0])
        self.assertNotIn("OPPORTUNITIES", body)
        self.assertLess(len(body.splitlines()), 20, body)

    def test_a_filter_that_removed_nothing_is_not_printed_in_health(self):
        """It is still emitted onto the artifact, which is where "is this filter
        running at all" is answered. HEALTH is for what the reader has to act on, and a
        dozen "0 removed" lines a morning is how a digest stops being read."""
        self.assertNotIn("readme-no-entity-column", render("full"))


def _update() -> int:
    """Regenerate the goldens. Kept in-file so there is no separate script to rot."""
    class _Harness(_Pinned, unittest.TestCase):
        def runTest(self):  # noqa: N802 - unittest's own naming
            pass

    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for name in SCENARIOS:
        harness = _Harness()
        harness.pin()
        try:
            (GOLDEN_DIR / f"digest-{name}.md").write_text(render(name), encoding="utf-8")
            print(f"wrote {GOLDEN_DIR.name}/digest-{name}.md")
        finally:
            harness.doCleanups()
    return 0


if __name__ == "__main__":
    if "--update" in sys.argv:
        sys.exit(_update())
    unittest.main(verbosity=2)
