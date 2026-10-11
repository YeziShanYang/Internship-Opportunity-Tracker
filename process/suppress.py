"""The two run-wide mute filters, and the reports they owe HEALTH.

Both of these sat loose in `check.main` as a pair of inline comprehensions with a
`before - after` subtraction each. That is how one of them managed to be broken for
weeks: `program_names` in sources.csv is "|"-separated, the filter compared the whole
field against a single programme name, and so muting had no effect at all on any source
covering more than one programme -- while reporting through nothing, which is why
nobody noticed it was not filtering. The two halves of the same failure.

Every filter reports what it removed, so each one here returns a `FilterReport` even
when it removed nothing. Reporting zero is the useful case: it is the difference between
"this filter is quiet today" and "this filter is not running".
"""
from __future__ import annotations

from core import clock, models, text

MUTED = "muted-programme"
APPLIED = "applied-tsv"
DISAPPEARED = "removed-posting"
CLOSED = "closed-posting"
DUPLICATE = "duplicate-posting"

# Enough removed keys to audit a filter that has started over-matching, few enough that
# the artifact stays readable.
MAX_SAMPLES = 5


def muted_programmes(programs: list[dict[str, str]]) -> set[str]:
    return {
        program["name"]
        for program in programs
        if (program.get("muted") or "").strip().lower() == "true"
    }


def _all_muted(change: models.Change, muted: set[str]) -> bool:
    """True only when EVERY programme this change's source informs is muted.

    Muting "Jane Street INSIGHT" must not also silence FTTP news arriving on the same
    row, and an empty `program_names` is a whole job board rather than a muted
    programme, so it is never muted.
    """
    names = [n.strip() for n in (change.program_name or "").split("|") if n.strip()]
    return bool(names) and all(name in muted for name in names)


def _group_by_posting(changes: list[models.Change]) -> list[list[models.Change]]:
    """Group changes that are demonstrably the same posting. Order is preserved.

    Union-find over posting identities rather than a dict keyed on one of them, because
    a single change can carry several -- a Simplify row holds the employer's apply link
    *and* its own -- and two rows may agree on one identity while each also knows an
    identity the other does not. Keying on "the first id" would split those.

    A change with no identity is its own group. That is the conservative direction and
    it is the point: two rows are only ever called the same posting on the evidence of a
    shared link, never on a resemblance between their titles.
    """
    identities = [
        text.posting_identities(change.detail, change.url, change.posting_url, change.key)
        for change in changes
    ]
    parent = list(range(len(changes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen: dict[str, int] = {}
    for index, ids in enumerate(identities):
        for identity in ids:
            if identity in seen:
                a, b = find(seen[identity]), find(index)
                if a != b:
                    parent[b] = a
            else:
                seen[identity] = index

    grouped: dict[int, list[models.Change]] = {}
    for index, change in enumerate(changes):
        grouped.setdefault(find(index), []).append(change)
    # Keyed on first appearance, so the surviving order matches the input order and a
    # re-run over the same artifact produces the same bytes.
    return [grouped[key] for key in dict.fromkeys(find(i) for i in range(len(changes)))]


def _report(filter_id: str, considered: int, removed: list[models.Change], reason: str):
    return models.FilterReport(
        stage="process",
        filter_id=filter_id,
        considered=considered,
        removed=len(removed),
        reason=reason,
        samples=tuple(change.key for change in removed[:MAX_SAMPLES]),
    )


def suppress(
    changes: list[models.Change],
    *,
    muted: set[str],
    applied: dict[str, str],
    aggregators: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[models.Change], list[models.FilterReport]]:
    """Drop muted and already-actioned changes. Returns (kept, reports).

    Order matters only for the counts: a change that is both muted and in applied.tsv
    is attributed to the muted filter, because that is the more specific statement about
    why the owner is not seeing it.
    """
    reports: list[models.FilterReport] = []

    # A posting that has left the board is not an opportunity: the owner cannot apply to
    # it, so it costs a line and offers nothing to do with it. Dropped first because
    # it is the most fundamental of the statements -- the mute filters say "you are
    # not being shown this", this one says "there is nothing there".
    #
    # Dropped here rather than in the renderer so the rows never reach `enrich` or
    # `classify`: on 2026-09-14 removals were 40 of 67 changes, which is 40 posting
    # fetches and 40 model calls spent on postings that no longer exist. The count still
    # goes to HEALTH, because a board shedding an implausible number of rows at once is
    # usually a parser or format problem rather than a hiring freeze, and this count is
    # the cheapest place that shows up.
    considered = len(changes)
    dropped = [change for change in changes if change.kind == "removed"]
    changes = [change for change in changes if change.kind != "removed"]
    reports.append(_report(
        DISAPPEARED, considered, dropped,
        "no longer on the source. A posting that has gone cannot be applied to",
    ))

    # The same fact in a different shape: the row is still on the list, but the list
    # has replaced its apply link with a padlock. On 2026-10-11 eighteen of these
    # reached the digest -- ten in the table and eight in RULED OUT -- as rows with no
    # link, because a closure reads to the differ as an edited row and a closed
    # duplicate added under an en-dash reads as a new one. Either way there is
    # nothing to apply to. Before `enrich` and `classify`, for the reason above.
    considered = len(changes)
    dropped = [change for change in changes if change.closed]
    changes = [change for change in changes if not change.closed]
    reports.append(_report(
        CLOSED, considered, dropped,
        "still listed, but the list marks the posting closed (🔒) and gives no link "
        "to it",
    ))

    # One row per opportunity, not one row per source. The watchlist overlaps on
    # purpose -- several aggregators plus ~70 employer boards -- so a new posting at a
    # well-covered firm arrives as three or four changes on one morning, and to the
    # reader that is one thing to go and do. Measured across the committed snapshots:
    # 127 postings sit on more than one source, 152 rows.
    #
    # Before the mute filters so their counts mean "postings muted" rather than "rows",
    # and before `enrich` and `classify` entirely, so a duplicate costs neither a
    # posting fetch nor a model call.
    considered = len(changes)
    kept: list[models.Change] = []
    dropped = []
    for group in _group_by_posting(changes):
        if len(group) == 1:
            kept.append(group[0])
            continue
        # The employer's own board wins over an aggregator's copy of the same posting.
        # The project's own framing of the two intake paths is the reason: an aggregator
        # is "wide, late, and thin on detail" while the firm's board is the record. On a
        # tie, first-seen, so the choice is deterministic and a re-render is byte-stable.
        winner = next(
            (c for c in group if c.source_id not in aggregators), group[0]
        )
        kept.append(winner)
        dropped += [c for c in group if c is not winner]
    changes = kept
    reports.append(_report(
        DUPLICATE, considered, dropped,
        "the same posting already appears in this digest from another source, matched "
        "on the employer's own posting link",
    ))

    considered = len(changes)
    dropped = [change for change in changes if _all_muted(change, muted)]
    changes = [change for change in changes if not _all_muted(change, muted)]
    reports.append(_report(
        MUTED, considered, dropped,
        "every programme their source informs is muted=true in data/programs.csv",
    ))

    # Dropped before classification, which also saves the model call. This used to be
    # populated by ticking a checkbox in a delivered digest; the digest is a table now
    # and a table cell cannot hold a working checkbox, so the file is hand-edited.
    considered = len(changes)
    keys = {change.key: clock.change_key(change.source_id, change.key) for change in changes}
    dropped = [change for change in changes if keys[change.key] in applied]
    changes = [change for change in changes if keys[change.key] not in applied]
    reports.append(_report(
        APPLIED, considered, dropped,
        "muted in data/applied.tsv. Delete the line to bring one back",
    ))

    return changes, reports


def removed_by(reports: list[models.FilterReport], filter_id: str) -> int:
    """How many rows one filter removed, across every report it emitted."""
    return sum(r.removed for r in reports if r.filter_id == filter_id)
