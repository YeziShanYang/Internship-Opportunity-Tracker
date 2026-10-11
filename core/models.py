"""The shapes every stage passes to the next one.

These carry no behaviour that touches the outside world, which is the point: a `Change`
can be built by `process`, read by `screen`, judged by `classify` and rendered by
`deliver` without any of those four needing to agree on anything else.

`SourceResult` is the load-bearing one. Spec section 10.1 says a failure must never be
indistinguishable from a quiet day, and this is where that is made structural rather
than remembered: `ok=False` and `ok=True, changes=[]` are different facts, and every
checker returns one of these whether it worked or not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Change:
    """One observed upstream change, before classification."""

    source_id: str
    kind: str  # added | removed | changed
    key: str  # human-readable row identity, e.g. "Akuna Capital / QD"
    detail: str  # what changed, as text the classifier and the reader both see
    # Opaque join key, set once by `process` and used by every stage after it. See
    # core.clock.change_id for why this is not the same thing as change_key.
    change_id: str = ""
    url: str = ""
    # The *fetchable posting page*, as distinct from `url`. On an aggregator row those
    # are different links and `url` is the wrong one: a Simplify row carries both the
    # employer's ATS (a JavaScript shell that returns zero characters) and
    # simplify.jobs/p/<uuid> (13K-34K characters of real requirements), and `url` is
    # whichever came first in the cell -- usually the company page. Recorded by the
    # producer that had the parsed cells in hand rather than recovered downstream with
    # a second regex, which is what it was until now.
    posting_url: str = ""
    program_name: str = ""  # filled from sources.csv program_names
    is_discovery_candidate: bool = False
    rolling: bool = False
    # Not a posting: a collapse notice, or "a section appeared". These carry the
    # source's own prose in `key`, so anything that reads the key as a job title will
    # be wrong about them -- `underclassmen-cruz: 78 of 107 rows changed at once`
    # matched the underclassman rule and pinned a board restructure into ACT NOW as
    # though it were an opportunity. Flagged rather than pattern-matched downstream,
    # because only the producer knows.
    structural: bool = False
    # The row is still listed but its source marks it closed: aprameyak and vanshb03
    # replace the apply link with 🔒 rather than deleting the row. Such a row has no
    # link to print and nothing to apply to, so it is dropped with the removals. Set by
    # the producer, which is the only place that still has the row's own value.
    closed: bool = False
    # Some sources hand back the full posting body in the same response that lists it
    # -- Greenhouse `content=true`, Lever and Ashby `descriptionPlain`. Carrying it
    # here lets the classifier judge real requirements without a second HTTP request,
    # and without `enrich` having to guess a URL it cannot always construct.
    posting_text: str = ""


@dataclass(frozen=True)
class FilterReport:
    """What one filter removed, in the one shape all of them use.

    Every filter reports what it removed. That rule is older than this dataclass and
    has been broken twice, both times the same way: the filter was added, the HEALTH
    line was not, and the digest stayed plausible while a source went blind. Two of the
    six filter sites reported through *nothing at all* until this shape existed -- the
    `muted` filter, and github_repos' section_include/section_exclude/ignore_columns.

    With one shape, "this filter removed rows and emitted no FilterReport" becomes a
    testable assertion instead of an invisible hole.
    """

    stage: str
    filter_id: str
    considered: int = 0
    removed: int = 0
    source_id: str = ""  # "" means run-wide rather than per-source
    reason: str = ""  # one human sentence, for HEALTH. NEVER parsed
    samples: tuple[str, ...] = ()  # up to 5 removed keys, so a bad filter can be audited


@dataclass(frozen=True)
class BoardCollapse:
    """A board that restructured rather than restocked.

    Carried typed rather than as an `extra` key so HEALTH cannot forget it: collapsing
    is what stands between a Greenhouse schema tweak and a 1,400-item digest, and a
    collapse nobody is told about is a board silently reporting one item on the morning
    it changed everything.
    """

    total: int
    samples: tuple[str, ...] = ()


@dataclass
class SourceResult:
    """Outcome of checking one source.

    Spec section 10.1: a failure must never be indistinguishable from a quiet day, so
    every check returns one of these whether it worked or not. `ok=False` and
    `ok=True, changes=[]` are different facts and are reported differently.
    """

    source_id: str
    ok: bool
    changes: list[Change] = field(default_factory=list)
    error: str = ""
    content_length: int = 0
    snapshot_text: str | None = None  # new snapshot to persist, if the check succeeded
    baseline: bool = False  # first ever sight of this source: record, do not report
    # Tier 1 and 2 store a canonical TSV; Tier 3 stores normalised page text.
    snapshot_ext: str = "tsv"
    extra: dict[str, Any] = field(default_factory=dict)
    # What each of this source's own filters removed. Separate from `extra` because a
    # filter report is a fact the digest is obliged to print, while `extra` is a loose
    # bag of counters -- and "the filter was added, the HEALTH line was not" is exactly
    # how this rule has been broken twice.
    filters: list[FilterReport] = field(default_factory=list)
    # Set when this board moved more rows at once than reads as news. The changes list
    # holds the single collapsed item; this is the fact HEALTH reports.
    collapsed: BoardCollapse | None = None
    # True when the circuit breaker skipped the fetch. Neither a success nor a new
    # failure: the counters must not move, or a quarantine would inflate itself.
    quarantined: bool = False


@dataclass
class SourceMetrics:
    """Everything one source check reported, except the payload.

    The snapshot text is deliberately absent. It is 22KB for one repo, its home is
    data/snapshots/ where git already tracks it, and carrying it through the stage
    handoff as well would make .run/changes.json unreadable in exactly the way these
    artifacts exist to prevent.

    `ok=False` and `ok=True, change_count=0` stay different facts here, the same way
    they are on SourceResult (spec 10.1).
    """

    source_id: str
    ok: bool
    error: str = ""
    quarantined: bool = False
    baseline: bool = False
    content_length: int = 0
    snapshot_ext: str = "tsv"
    change_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    # The reporting facts, carried rather than dropped: `deliver` renders the HEALTH
    # block from this projection, so anything HEALTH is obliged to print has to survive
    # the stage boundary. Omitting either of these is how a filter or a board
    # restructure would go unmentioned in a digest rendered from the artifact.
    filters: list[FilterReport] = field(default_factory=list)
    collapsed: BoardCollapse | None = None

    @classmethod
    def of(cls, result: SourceResult) -> SourceMetrics:
        return cls(
            source_id=result.source_id,
            ok=result.ok,
            error=result.error,
            quarantined=result.quarantined,
            baseline=result.baseline,
            content_length=result.content_length,
            snapshot_ext=result.snapshot_ext,
            change_count=len(result.changes),
            extra=dict(result.extra),
            filters=list(result.filters),
            collapsed=result.collapsed,
        )


@dataclass
class ChangeSet:
    """The `.run/changes.json` payload: what moved, and what every filter removed.

    Written after suppression rather than before, because a change that was muted is
    not a change this run is acting on -- but the count of them is, which is what the
    FilterReports carry.
    """

    changes: list[Change] = field(default_factory=list)
    metrics: list[SourceMetrics] = field(default_factory=list)
    filters: list[FilterReport] = field(default_factory=list)


@dataclass
class FetchAttempt:
    """What one network fetch did, without its payload.

    The payload goes to `.run/raw/{source_id}.body` as bytes; this is the row in
    `.run/raw/index.json`. Separating them is what lets `run.py process --only X` read
    one source instead of parsing every body, and it keeps the index small enough to
    scan in one look for all ~157 sources.

    `requested_url` and `final_url` are both here because the difference between them
    is part of whether the fetch succeeded -- see `process.redirect`. `requests` is
    greater than one for the methods whose single logical fetch is several round trips:
    Workday pages a board and then fetches a description per survivor.
    """

    source_id: str
    ok: bool
    error: str = ""
    status: int = 0
    requested_url: str = ""
    final_url: str = ""
    size: int = 0
    sha256: str = ""
    elapsed_ms: int = 0
    requests: int = 1
    # True when the circuit breaker skipped the fetch. A distinct fact from a failure:
    # nothing was learned, so no counter may move, or a quarantine would extend itself
    # for not having been looked at.
    quarantined: bool = False
    # The few per-method facts that are not the body and are not guessable from it: the
    # README's default branch, and how many rows a paged board reported. Strings, so the
    # artifact stays readable and the codec stays trivial; parsed back by the one
    # assessor that wrote them.
    meta: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Posting:
    """One job as an ATS describes it, before any screen has run.

    Lives in `core` rather than next to the parsers because both `gather` and `process`
    need the shape: the fetcher decides which Workday descriptions are worth a second
    request, and the parser decides which postings survive the student and location
    screens. A stage may only import at or below its own level, so the shape they share
    has to sit under both.
    """

    title: str
    location: str
    department: str
    employment_type: str
    url: str
    text: str  # description; goes to the classifier, never into the snapshot


# List prices per million tokens, (input, output), as published 2026-09-12. Only models
# whose pricing has actually been checked appear here: an unpriced model reports its
# token counts and says so, rather than inventing a dollar figure.
PRICES_PER_MTOK = {
    "gpt-5-mini": (0.25, 2.00),
}


@dataclass
class Usage:
    """What the run actually spent, accumulated across the thread pool.

    This exists because the only way to answer "why did yesterday cost 34 cents" used
    to be to reconstruct the prompts from git and solve backwards from the Azure
    portal. That reconstruction established that 81% of the spend was reasoning tokens
    -- a fact nothing in the digest would have surfaced on its own, which is exactly why
    the cost drifted unnoticed. A cost that only appears on a billing page a day later
    is a cost nobody notices rising.
    """

    provider: str = ""
    model: str = ""
    effort: str = ""
    calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    unreported: int = 0  # calls whose response carried no usage block

    def estimated_usd(self) -> float | None:
        """None means this model's pricing has not been checked, not that it is free."""
        rates = PRICES_PER_MTOK.get(self.model)
        if rates is None:
            return None
        rate_in, rate_out = rates
        # Cached input bills at a tenth of list on both providers.
        billed_in = (
            (self.input_tokens - self.cached_input_tokens)
            + self.cached_input_tokens * 0.1
        )
        return (billed_in * rate_in + self.output_tokens * rate_out) / 1_000_000


# How a Judgment came to exist. Five call sites used to produce five slightly different
# shapes of the same dataclass, with `classified` set by hand at each one -- so "was
# this judged?" was a field someone had to remember to set rather than a consequence of
# how the judgment was made.
MODEL = "model"
SCREENED = "screened"
UNCLASSIFIED = "unclassified"
ERROR = "error"
OVER_BUDGET = "over_budget"

#: The outcomes that represent a real verdict on the item.
DECIDED = (MODEL, SCREENED)


@dataclass
class Judgment:
    """A classified change, and how it came to be classified."""

    change: Change
    outcome: str = UNCLASSIFIED
    relevant: bool = True
    program_name: str = ""
    new_status: str = "unknown"
    why: str = ""
    confidence: str = "low"
    # Which kind of rule ruled this row out: "eligibility" (checkable against the
    # posting, and reliable) or "value" (rule 8's judgement of whether a role the user can
    # apply to is worth their morning, which has been wrong and is printed in full so it
    # can be argued with). Empty when the row is relevant.
    ruled_out_by: str = ""
    eligible_proposal: str = ""
    error: str = ""
    # The three facts the owner actually decides on, read off the posting body rather
    # than inferred from the row. They are separate fields and not folded into `why`
    # because the digest renders them as their own bullets, and because `deadline` is
    # the input to the urgency rule -- see deliver.urgency. A row's own text cannot
    # supply any of them: measured on simplify-2027, 7 of 592 rows mention a class year
    # at all, which is why ACT NOW was structurally empty of opportunities until these
    # existed.
    #
    # `deadline` is "rolling", an ISO date, or "". Anything else is displayed verbatim
    # and never treated as urgent -- an unparseable value must not silently promote or
    # demote a row.
    class_year: str = ""
    location: str = ""
    deadline: str = ""
    # "small", "large" or "". Added 2026-09-30, when startups and small firms were
    # weighted up because large employers hire juniors and seniors.
    # The digest sorts "small" first within each urgency tier; it never filters.
    employer_size: str = ""
    # Set when the deterministic screen settled this instead of the model. Carried so
    # RULED OUT can say which rule fired, and so a digest can be read back later to
    # tell which rule set produced it.
    screen_rule: str = ""
    screen_version: int = 0

    @property
    def classified(self) -> bool:
        """Derived, not stored. A judgment is classified when something actually
        decided it -- the model, or a quoted phrase. Everything else reached the digest
        unjudged, which the reader is told about rather than left to infer."""
        return self.outcome in DECIDED

    @classmethod
    def from_model(cls, change: Change, **payload) -> Judgment:
        return cls(change=change, outcome=MODEL, **payload)

    @classmethod
    def from_screen(
        cls, change: Change, *, why: str, rule: str, version: int
    ) -> Judgment:
        """A quoted phrase is a real judgment, with high confidence: the employer wrote
        the sentence that rules the item out."""
        return cls(
            change=change, outcome=SCREENED, program_name=change.program_name,
            relevant=False, confidence="high", why=why,
            screen_rule=rule, screen_version=version)

    @classmethod
    def unjudged(cls, change: Change, why: str) -> Judgment:
        """Surfaced without a verdict. `relevant` stays True on purpose -- spec 8 rule
        3: a false negative costs a real opportunity, a false positive costs a glance."""
        return cls(change=change, outcome=UNCLASSIFIED,
                   program_name=change.program_name, why=why)

    @classmethod
    def failed(cls, change: Change, error: str) -> Judgment:
        """A provider failure is never silently swallowed. The digest reports it rather
        than showing the change as benignly unclassified (spec 10.1)."""
        return cls(change=change, outcome=ERROR,
                   program_name=change.program_name, error=error)

    @classmethod
    def over_budget(cls, change: Change, cap: int) -> Judgment:
        return cls(
            change=change, outcome=OVER_BUDGET, program_name=change.program_name,
            why=f"Not classified: this run exceeded its cap of {cap} classifications, "
                "so this row is surfaced unjudged rather than dropped.")


@dataclass
class PinnedRow:
    """An underclassman-targeted posting that stays in ACT NOW while it is still up.

    Added 2026-09-18: a posting aimed at freshmen, sophomores or underclassmen in
    general goes in ACT NOW and stays there for as long as the posting is still up.
    Every other ACT NOW test is a *dated* reason
    that fires on the morning a row moves; this one is a standing fact about who the
    posting is for, so it has to survive the day its change scrolls past.

    A flattened row rather than a `Judgment`, and that is the point: what is pinned is
    the handful of facts the owner decides on, carried forward without re-asking the
    model. Re-classifying a pinned row every morning would bill a token a day per row
    to re-derive an answer that cannot have changed.
    """

    change_id: str
    source_id: str
    company: str
    position: str
    url: str = ""
    deadline: str = ""
    class_year: str = ""
    location: str = ""
    # The morning it first earned the pin, printed so a row that has sat there for
    # three weeks says so rather than reading as today's news.
    first_pinned: str = ""
    last_seen: str = ""


@dataclass
class JudgedSet:
    """The `.run/judged.json` payload: the verdicts, and what they cost."""

    judgments: list[Judgment] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    # Resolved here rather than at render time so that `run.py render` reproduces the
    # digest byte-for-byte without re-reading the snapshots -- the same reason the
    # screen tally and the spend are values on the artifacts.
    pinned: list[PinnedRow] = field(default_factory=list)
