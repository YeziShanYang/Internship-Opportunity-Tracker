"""Where everything lives, and the constants that describe the files' shape.

Split out of `state.py`, which had grown into four unrelated jobs in one module: these
paths and columns, the clock helpers, the dataclasses every stage passes around, and the
CSV reader/writer. Only the last of those does any I/O, and mixing it with the rest is
what made "which layer may touch the disk" unanswerable.

**Every path here is read as a module attribute at call time, never imported by value.**
`persist.store` says `paths.PROGRAMS_CSV`, not `from core.paths import PROGRAMS_CSV`.
That is not a style preference -- it is the only reason the test suite can redirect the
whole database at a temp directory, and `from ... import` would silently defeat it while
leaving every test green. See `_IsolatedState` and the fingerprint guard in
tests/test_acceptance.py, which exist because that has already gone wrong twice.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROGRAMS_CSV = DATA / "programs.csv"
SOURCES_CSV = DATA / "sources.csv"
# The one method that is deliberately not fetched: these sources block automation or
# have nothing to diff, and they reach the owner as calendar reminders instead. It lives
# here rather than with the checker registry because build_xlsx.py needs it too -- a
# manual row must not count as coverage when deciding what stays on the owner's
# hand-check list.
UNWATCHED_METHOD = "manual"
SNAPSHOTS = DATA / "snapshots"
PROPOSALS_LOG = DATA / "proposals.log"
# Items the owner has ticked off in a delivered digest -- applied to, or not interested.
# Suppressed from every later digest. Keyed on a hash of source_id + row key, so next
# cycle's repost ("Summer 2028" rather than "Summer 2027") is a different key and
# resurfaces on its own, which is the intended behaviour rather than an accident.
APPLIED_TSV = DATA / "applied.tsv"
# Sources the weekly discovery pass has proposed. Committed, because without it the
# same candidates reappear every Monday and the section becomes noise. `status` is
# hand-edited: `rejected` is a permanent tombstone, never re-proposed.
DISCOVERED_CSV = DATA / "discovered.csv"
DISCOVERED_COLUMNS = [
    "first_proposed", "last_proposed", "kind", "key", "title", "url", "evidence",
    # One sentence from the priority triage: what the source is and why it does or does
    # not matter. Written for a discarded row as well as a kept one -- a row whose only
    # record was "discarded" would say nothing about what was thrown away.
    "description", "status",
]
# Underclassman-targeted postings pinned into ACT NOW until they leave their board.
# Committed, because the pin has to outlive the run that created it -- that is the
# whole feature. Rows are dropped only when the source that carries them was checked
# successfully and no longer lists them; a failing or quarantined source keeps its
# pins, because "we stopped looking" must never read as "it closed".
STANDING_CSV = DATA / "standing.csv"
STANDING_COLUMNS = [
    "change_id", "source_id", "company", "position", "url",
    "deadline", "class_year", "location", "first_pinned", "last_seen",
]
LAST_DISCOVERY = DATA / "last_discovery.txt"
# Slug lists for the bulk board sources (`greenhouse_bulk`, `ashby_bulk`, `lever_bulk`):
# one employer board per line, each watched without a sources.csv row of its own.
BOARD_LISTS = DATA / "boards"
# One row per weekly coverage measurement (process.coverage): how many postings in the
# target fields the tracker sees against Simplify's whole list. Committed, because the
# trend is the claim worth making and a single week is an anecdote.
COVERAGE_CSV = DATA / "coverage.csv"
COVERAGE_COLUMNS = [
    "date", "scope", "tracker", "simplify", "tracker_only", "employer_only", "lead_pct",
]
# Simplify's own data file, of which the README is a rendering. The reference for
# coverage: every active posting, not just the README sections we parse.
SIMPLIFY_LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/"
    ".github/scripts/listings.json"
)
# The date of the last digest actually delivered. The schedule fires several times
# each morning so that a dropped cron tick is not a missed day (see daily.yml), and the
# cadence is exactly one digest a day, so something has to stop ticks two and three from
# mailing again. This marker is the *fallback* half of that cap -- it is read from
# whatever commit the run checked out, so it can be stale. deliver.issue.already_sent
# holds the authoritative answer.
LAST_DELIVERED = DATA / "last_delivered.txt"
# Fetched job-posting text. Derived data, not state: re-fetchable, and committing ~35
# pages a day would bury the CSV history that git log exists to answer. It lived as an
# import-bound `postings.CACHE_DIR` for a long time, which is why the test suite used to
# write into the real one; resolving it here means it is redirected like every other
# path rather than needing its own special case.
POSTINGS_CACHE = DATA / "postings_cache"

# Derived stage artifacts. Gitignored, following the precedent set by
# data/postings_cache/: re-fetchable data that would otherwise bury the CSV history that
# `git log` exists to answer.
RUN_DIR = ROOT / ".run"
# Two workbooks, with a deliberate split of audience (2026-09-11). programs.xlsx is the
# owner's file: only the opportunities that have to be chased by hand. Anything the
# owner cannot apply to, and anything the tracker already watches, is
# bloat there and belongs in the other file. tracked.xlsx is the tool's own bookkeeping
# -- the full programme list plus the source, applied and discovery tables.
OUT_XLSX = ROOT / "out" / "programs.xlsx"
OUT_TRACKED_XLSX = ROOT / "out" / "tracked.xlsx"

# Honest about *what* is asking -- an automated tracker, never a browser string -- and
# silent about *who*. This used to carry the owner's email as a contact address, which
# put the owner's identity in front of ~144 employer sites every morning. Nothing personal goes
# here: no email, no name, and no repo URL either, because the repo is public and leads
# straight back to the owner. Changed 2026-09-26.
USER_AGENT = "opportunity-tracker/1.0"
REQUEST_DELAY_SECONDS = 1.0
HTTP_TIMEOUT_SECONDS = 30.0

# GitHub rejects an issue body over this with a 422 and opens nothing at all. Lives here
# rather than in deliver.issue so that deliver.digest can respect it without importing
# the module that does the POST -- the separation that lets a digest be rendered offline.
#
# It has cost two mornings. 2026-09-14 hit it on 469 changes that were a churn bug, and
# 2026-09-15 hit it again on 146 that were real. The first was a defect to fix; the
# second is the shape of a busy day, which is why `digest.render` now trims to fit and
# says what it withheld instead of letting the whole digest be lost.
MAX_ISSUE_BODY_CHARS = 65_536

# Spec section 10.1: a source this broken is an emergency, not a footnote.
FAILURE_ESCALATION_THRESHOLD = 3

# Seeded from quant_math_cs_programs_freshman.xlsx, plus the tracking columns in
# spec section 4. Order is fixed; do not reorder without rewriting the whole file.
PROGRAM_COLUMNS = [
    "category",
    "name",
    "website",
    "applications_open",
    "target_years",
    "eligible",
    "notes",
    "source_id",
    "last_checked",
    "last_changed",
    "snapshot_hash",
    "status",
    "first_seen",
    "muted",
]

SOURCE_COLUMNS = [
    "source_id",
    "tier",
    "method",
    "url",
    "program_names",
    "selector",
    "render_js",
    # true for a page_text row that is a list of postings (a VC portfolio board, a Built
    # In search, an ATS feed) rather than a programme page. A change to it is reported
    # only when an added line is a posting, and is named by that posting; see
    # process.parse_page.page_verdict.
    "job_list",
    "last_success",
    "consecutive_failures",
    # When we last actually tried. Distinct from last_success because the circuit
    # breaker needs to know when the backoff window started, and a quarantined run
    # is not an attempt.
    "last_attempt",
    # high | low. Low-signal sources are only classified when a row matches the
    # underclassman or rolling-firm filters; see classify.triage.
    "signal",
    "notes",
]

PROGRAM_STATUSES = ("dormant", "open", "closed", "unknown")
