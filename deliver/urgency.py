"""Which rows are High rather than Medium, and why; and which are for underclassmen.

The tiers were ACT NOW and WORTH A LOOK until 2026-10-08. They are now High and Medium,
each row carries the reason it got its tier, and postings aimed at first-years have
their own block at the top of the digest instead of competing for the High tier.

A pure function rather than a property on `Judgment`, because three of its four inputs
live on the `Change` and were never properties of the model's answer -- and because
urgency is a presentation decision. The same judgment could reasonably be urgent in one
surface and not in another; the classifier has no opinion about it.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from core import clock, models
from screen import rules as screen_rules

# The word the model returns for "reviews on a rolling basis / closes when full". One
# constant because two modules compare against it and a typo in either would silently
# stop the digest saying so.
ROLLING = "rolling"

HIGH = "High"
MEDIUM = "Medium"

# A class-year phrase that names first-years at all, even inside a floor or a list that
# runs on to seniors ("freshman, sophomore, junior, or senior"). Not enough to pin --
# see `NOT_TARGETED` -- but it is the employer saying in writing that it takes them,
# which is the best evidence of the owner's odds short of a programme built for them.
# "Sophomore" is not here: the owner is a first-year.
FIRST_YEARS_NAMED = re.compile(
    r"first.?year|1st.?year|freshm[ae]n|underclass", re.IGNORECASE)

# Who the posting is *for*. Deliberately narrower than `snapshot.DISCOVERY_PATTERN`,
# which also matches insight/discovery/ignite/launch and exists to spot a candidate new
# *programme*; this one has to be true of the role itself, because a pin lasts until the
# posting comes down and a wrong one is a wrong row every morning rather than once.
UNDERCLASSMAN = re.compile(
    r"first.?year|1st.?year|freshman|freshmen|sophomore|second.?year|2nd.?year"
    r"|underclass(?:man|men)?|rising sophomore",
    re.IGNORECASE,
)


# A class-year phrase that names first- or second-years but is not *aimed* at them. Added
# 2026-09-30: the pins had filled with rows like GE Healthcare's "at
# least sophomore standing" and Gilead's "freshman, sophomore, junior, or senior", which
# mention the word while being a floor or an open door. A posting is told once when it
# appears, and pinned only when it is a real match -- a programme for
# first- and second-years. A floor ("at least", "minimum", "completed") and an
# enumeration that runs on to juniors or seniors are both general postings.
NOT_TARGETED = re.compile(
    r"at\s+least|minimum|\bmin\.?\s|or\s+(?:higher|above|beyond)|and\s+(?:up|above)"
    r"|complet\w*|\+|junior|senior|third|fourth|3rd|4th|all\s+(?:class\s+)?years"
    r"|any\s+(?:class\s+)?year|graduate\s+student",
    re.IGNORECASE,
)


def _aimed_at_underclassmen(class_year: str, title: str) -> bool:
    """The shared test for a live judgment and a carried pin.

    The title is decisive on its own ("First-Year Insight"): an employer that names the
    year in the job title has built the role for it. The class-year phrase has to both
    name the year and not be a floor or a wider list.

    Sophomores alone are not a match. The owner is a first-year, so "Sophomore Intern"
    is a class-year rule-out and not a pin; Thrivent's sat at the top of the digest on
    2026-10-08 until this check existed. Re-asked of carried pins, so that one retires.
    """
    if screen_rules.sophomore_only(title) or screen_rules.sophomore_only(class_year):
        return False
    if UNDERCLASSMAN.search(title):
        return True
    return bool(UNDERCLASSMAN.search(class_year)) and not NOT_TARGETED.search(class_year)


def _deadline_passed(deadline: str) -> bool:
    days = clock.days_until(deadline)
    return days is not None and days < 0


def targets_underclassmen(judgment: models.Judgment) -> bool:
    """Whether this posting is aimed at first- or second-years.

    Read off `Judgment.class_year` and the row's own title -- never off the section
    heading the row sits under. That distinction dates from 2026-09-18 and it is the
    difference between a usable block and an undeliverable one: three of
    the watched repos (`luisae`, `underclassmen-cruz`, `underclassmen-zapply`) are
    underclassman trackers end to end, so their section headings carry the word on
    every row. `Row.identity` is section-qualified, so matching the whole snapshot line
    put 114 rows in scope -- 110 of them from those three repos, and at 577 bytes a
    table row that is ~63KB against GitHub's 65,536-character issue limit. The digest
    would have failed to send rather than merely read badly.

    `Change.key` is the row's own key, not the identity, so it is safe to read here;
    `Change.detail` is not, because on a `changed` row it carries the whole before and
    after including the heading.
    """
    if judgment.change.structural:
        # A collapse notice or a new-section row. Its `key` is the source's own prose,
        # not a job title: "underclassmen-cruz: 78 of 107 rows changed at once" matched
        # this rule and pinned a board restructure into ACT NOW, where it would have sat
        # until someone noticed. Measured on a forced re-baseline, 2026-09-18.
        return False
    return _aimed_at_underclassmen(judgment.class_year, judgment.change.key)


def is_stanford(judgment: models.Judgment) -> bool:
    """Whether the change comes from a Stanford page (stanford.edu, SLAC included).

    Added 2026-10-08: Stanford's own research programmes -- CURIS, SURIM, SIEPR, the
    VPUE index, SOLO's first-year filter -- are open to its first-years almost without
    exception, so they go in the top block with the postings built for them. Read off
    the host rather than a sources.csv column, because the host is the fact and a column
    would be one more thing to remember to set. Unlike an underclassman posting this is
    never pinned: a programme page stays up all year, so a pin would never retire, and a
    page change is news on the morning it happens.
    """
    host = (urlsplit(judgment.change.url or "").hostname or "").lower()
    return host == "stanford.edu" or host.endswith(".stanford.edu")


def in_top_block(judgment: models.Judgment) -> bool:
    """Relevant, and either built for underclassmen or a Stanford programme."""
    return judgment.relevant and (targets_underclassmen(judgment) or is_stanford(judgment))


def is_rolling(judgment: models.Judgment) -> bool:
    """Whether the posting itself says it closes when full."""
    return judgment.deadline.strip().lower() == ROLLING


# The reasons, best first. A row's position in this list is its sort key within its
# tier, so the order is the ranking: trimming the table from the end drops the row a
# first-year is least likely to land.
DISCOVERY = "discovery programme"
OPEN_TO_FIRST_YEARS = "open to first-years"
SMALL = "startup / small firm"
SIZE_UNKNOWN = "size unknown"
LARGE = "large firm"
UNVERIFIED = "unverified"
_ORDER = (DISCOVERY, OPEN_TO_FIRST_YEARS, SMALL, SIZE_UNKNOWN, LARGE, UNVERIFIED)


def rank(judgment: models.Judgment) -> tuple[str, str]:
    """(tier, reason) for a relevant judgment. The reason is printed beside the tier.

    The tier is the owner's odds of landing the role, not its deadline. The digest is
    read the morning a posting appears and applied to that day, so a close date sorts
    nothing: until 2026-10-11 High meant "closes within 21 days or a named rolling
    firm", and 80-90% of every table read "Medium · no deadline stated". The deadline is
    still printed in the Notes cell as a fact.

    High, best first: a confidently classified discovery programme; a posting whose
    class-year line names first-years even in a wider list; a startup or small firm.
    Medium: size unknown, then large employers, which recruit through junior-year
    pipelines, then anything the classifier never saw. Postings built *for*
    underclassmen are not ranked here -- they have their own block; see
    `targets_underclassmen`.
    """
    change = judgment.change
    if not judgment.classified:
        return MEDIUM, UNVERIFIED
    if change.is_discovery_candidate and judgment.confidence in ("medium", "high"):
        return HIGH, DISCOVERY
    if FIRST_YEARS_NAMED.search(judgment.class_year) \
            and not screen_rules.sophomore_only(judgment.class_year):
        return HIGH, OPEN_TO_FIRST_YEARS
    if judgment.employer_size == "small":
        return HIGH, SMALL
    if judgment.employer_size == "large":
        return MEDIUM, LARGE
    return MEDIUM, SIZE_UNKNOWN


def order(judgment: models.Judgment) -> int:
    """Sort key within a tier: the reason's place in `_ORDER`."""
    return _ORDER.index(rank(judgment)[1])


def is_urgent(judgment: models.Judgment) -> bool:
    """High tier: relevant, not in the top block, and a High reason."""
    if not judgment.relevant or in_top_block(judgment):
        return False
    return rank(judgment)[0] == HIGH


def live_change_ids(
    previous: dict[str, set[str]], changes: list[models.Change], checked: set[str]
) -> dict[str, set[str]]:
    """Which rows are on their board *now*, per source.

    Derived from yesterday's snapshots plus today's diff rather than from the new
    snapshots directly, because at pin-refresh time the new ones have not been written
    yet -- and because the subtraction is exactly what the diff already computed. A
    source that was not checked this run keeps yesterday's set verbatim; see
    `refresh_pins` for why that matters.
    """
    live = {source_id: set(ids) for source_id, ids in previous.items()}
    for change in changes:
        if change.source_id not in checked:
            continue
        bucket = live.setdefault(change.source_id, set())
        if change.kind == "removed":
            bucket.discard(change.change_id)
        else:
            bucket.add(change.change_id)
    return live


def refresh_pins(
    judgments: list[models.Judgment],
    existing: list[models.PinnedRow],
    live: dict[str, set[str]],
    checked: set[str],
    today: str,
    describe,
) -> list[models.PinnedRow]:
    """Today's pinned set: yesterday's, minus the ones that came down, plus new ones.

    Two asymmetries are load-bearing, and both are the same instinct the rest of this
    codebase follows -- a wrong keep costs one line the owner skims, a wrong drop hides
    an opportunity.

    A pin is dropped **only** when the source that carries it was checked successfully
    this run and no longer lists the row. A failing source, a quarantined one, or a
    source skipped by `--only` keeps every pin it has: "we have stopped looking" must
    never be rendered as "it closed", which is the same rule the circuit breaker
    follows when it insists a quarantined source still appears in HEALTH.

    And only a *relevant* judgment earns a pin. A row the screen or the classifier
    ruled out is not pinned, so the block does not accumulate the non-US, junior-only
    and off-field postings that the underclassman trackers carry alongside the real
    ones. It still prints once, in RULED OUT, on the morning it moved.
    """
    by_id = {row.change_id: row for row in existing}

    for judgment in judgments:
        if not judgment.relevant or not targets_underclassmen(judgment):
            continue
        change = judgment.change
        if change.kind == "removed":
            by_id.pop(change.change_id, None)
            continue
        company, position = describe(judgment)
        was = by_id.get(change.change_id)
        by_id[change.change_id] = models.PinnedRow(
            change_id=change.change_id,
            source_id=change.source_id,
            company=company,
            position=position,
            url=change.posting_url or change.url,
            deadline=judgment.deadline,
            class_year=judgment.class_year,
            location=judgment.location,
            first_pinned=was.first_pinned if was else today,
            last_seen=today,
        )

    kept = []
    for row in by_id.values():
        # Re-asked of every carried pin, not just new ones, so a rule change retires the
        # pins the old rule made instead of leaving them until their postings close.
        if not _aimed_at_underclassmen(row.class_year, row.position):
            continue
        # A stated close date that has passed ends the pin even while the page is up:
        # Jane Street's Bridge sat pinned three days past its 2026-09-27 deadline.
        if _deadline_passed(row.deadline):
            continue
        if row.source_id in checked:
            if row.change_id not in live.get(row.source_id, set()):
                continue  # checked, and the board no longer lists it
            row.last_seen = today
        kept.append(row)
    kept.sort(key=lambda r: (r.first_pinned, r.company.lower(), r.position.lower()))
    return kept
