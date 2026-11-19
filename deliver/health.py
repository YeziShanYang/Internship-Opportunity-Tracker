"""The HEALTH block: what the run did, what it removed, and what it spent.

Functions over data, with no module globals read anywhere. That is the whole point of
this module existing: `usage_line` used to live in `classify` and read `classify.USAGE`,
so rendering a digest in a fresh process printed a line about a run that had not
happened -- and the stage forbidden to emit markdown was emitting markdown.
"""
from __future__ import annotations

import re

from core import models, paths

# Past this many sources, a filter line reports the count instead of the names.
MAX_FILTER_SOURCES = 6


def usage_line(usage: models.Usage) -> str | None:
    """What the run spent, or None if the model was never called.

    Reports tokens and says the model is unpriced rather than inventing a figure, for
    the same reason PRICES_PER_MTOK only lists models whose pricing has been checked.
    """
    if not usage.calls:
        return None
    bits = [
        f"classifier: {usage.calls} call{'s' if usage.calls != 1 else ''}",
        f"{usage.input_tokens:,} in",
        f"{usage.output_tokens:,} out",
    ]
    if usage.cached_input_tokens:
        bits.insert(2, f"{usage.cached_input_tokens:,} cached")
    if usage.reasoning_tokens:
        # Named separately because it is where the money goes: 81% of the 2026-09-12
        # bill was reasoning tokens, and nothing in the digest said so.
        bits.append(f"{usage.reasoning_tokens:,} of it reasoning")
    cost = usage.estimated_usd()
    bits.append(
        f"~${cost:.4f} est." if cost is not None
        else f"{usage.model} is unpriced here")
    line = " · ".join(bits) + f" ({usage.model}, effort={usage.effort})"
    if usage.unreported:
        # Missing usage is counted, not assumed to be zero: a provider that stopped
        # reporting would otherwise make the run look free, which is the same failure
        # shape as a source that goes quiet instead of failing.
        line += (
            f" — ⚠ {usage.unreported} call(s) reported no usage, so this is an "
            "undercount"
        )
    return line


# Most filters count rows; a bulk source's unreadable-board report counts boards, and
# nothing was removed -- the rows were carried forward.
FILTER_UNITS = {
    "bulk-board-unreadable": "boards unreadable",
    "page-churn": "changed pages dropped",
    "page-no-new-posting": "changed pages dropped",
}
NAME_SAMPLES = {"bulk-board-unreadable"}


def filter_lines(reports: list[models.FilterReport]) -> list[str]:
    """One line per filter that actually removed something.

    A filter that removed nothing is not printed -- that would be a dozen lines of
    "0 removed" every morning, and a digest nobody reads is as good as no digest. The
    *report* is still emitted by the filter and still lands in `.run/changes.json`, so
    "this filter is not running" stays answerable; it just is not the reader's problem
    until it removes something.

    Reports are aggregated by filter across sources, because per-source lines would put
    five identical sentences in HEALTH on a morning when five READMEs each excluded a
    contents table. The source list is capped for the same reason: the student-role
    screen legitimately fires on 62 of the watched boards, and naming all 62 is 700
    characters of HEALTH that nobody will read to the end of.
    """
    by_filter: dict[str, list[models.FilterReport]] = {}
    for report in reports:
        if report.removed:
            by_filter.setdefault(report.filter_id, []).append(report)

    lines: list[str] = []
    for filter_id in sorted(by_filter):
        group = by_filter[filter_id]
        removed = sum(r.removed for r in group)
        considered = sum(r.considered for r in group)
        sources = sorted({r.source_id for r in group if r.source_id})
        if not sources:
            where = ""
        elif len(sources) <= MAX_FILTER_SOURCES:
            where = f" ({', '.join(sources)})"
        else:
            # The count, not a truncated list: an arbitrary first six reads as if the
            # filter only touched those, which is worse than saying how many.
            where = f" (across {len(sources)} sources)"
        what = FILTER_UNITS.get(filter_id, "rows removed")
        named = ""
        if filter_id in NAME_SAMPLES:
            # Which boards, not just how many: a board that stays unreadable for weeks
            # is carrying stale rows, and only its name says which list to fix.
            named = " Boards: " + "; ".join(s for r in group for s in r.samples) + "."
        lines.append(
            f"· {filter_id}: {removed} of {considered} {what}{where}"
            f" — {group[0].reason}.{named}"
        )
    return lines


# Failures that share one cause print as one line once this many sources have it. On
# 2026-11-16 one HTTP 429 took out 26 Workday boards and printed 26 near-identical
# lines. Blind sources are never grouped: each of those needs its own look.
GROUP_FAILURES_AT = 3


def _cause(error: str) -> str:
    """The part of an error that does not name the source, for grouping failures."""
    status = re.search(r"(?:Client|Server) error '(\d{3} [^']*)'", error)
    if status:
        return f"HTTP {status.group(1)}"
    return re.sub(r"https?://\S+", "<url>", error.splitlines()[0] if error else "").strip()


def _shared_causes(
    failing: list[models.SourceMetrics], sources: dict[str, dict[str, str]]
) -> dict[str, list[models.SourceMetrics]]:
    """Causes shared by enough not-yet-blind failures to print as one line."""
    by_cause: dict[str, list[models.SourceMetrics]] = {}
    for result in failing:
        count = int(sources.get(result.source_id, {}).get("consecutive_failures") or 0)
        if result.quarantined or count >= paths.FAILURE_ESCALATION_THRESHOLD:
            continue
        cause = _cause(result.error)
        if cause:
            by_cause.setdefault(cause, []).append(result)
    return {c: g for c, g in by_cause.items() if len(g) >= GROUP_FAILURES_AT}


def _retry_note(result: models.SourceMetrics) -> str:
    if not result.extra.get("retried"):
        return ""
    return " Asked again at the end of the run and refused again."


def _group_line(
    cause: str, group: list[models.SourceMetrics], sources: dict[str, dict[str, str]]
) -> str:
    counts = sorted({int(sources.get(r.source_id, {}).get("consecutive_failures") or 0)
                     for r in group})
    if len(counts) == 1:
        streak = f"{counts[0]} consecutive failure{'s' if counts[0] != 1 else ''} each"
    else:
        streak = f"{counts[0]}-{counts[-1]} consecutive failures"
    dates = sorted({(sources.get(r.source_id, {}).get("last_success") or "never")[:10]
                    for r in group})
    last = dates[0] if len(dates) == 1 else f"{dates[0]} to {dates[-1]}"
    retried = all(r.extra.get("retried") for r in group)
    return (
        f"⚠ {len(group)} sources failed with the same error, {cause} ({streak}, "
        f"last success {last}): " + ", ".join(sorted(r.source_id for r in group)) + "."
        + (" All were asked again at the end of the run and refused again." if retried else "")
    )


def source_lines(
    results: list[models.SourceMetrics], sources: dict[str, dict[str, str]]
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Return (health lines, blind sources as (source_id, status, detail)).

    Takes the `SourceMetrics` projection rather than live `SourceResult`s, which is
    what lets `run.py render` reproduce a digest from `.run/changes.json` alone. Every
    fact HEALTH is obliged to print is on the projection for exactly that reason.

    A blind source (3+ failures running) used to be a High row at the top of the
    opportunities table. Since 2026-11-19 it is reported here only, as a SOURCE BLIND
    line plus a count in the summary, because the table is for things to apply to.
    The blind list is still returned: the caller uses it to keep a blind morning from
    being titled "(no changes)".
    """
    healthy = [r for r in results if r.ok]

    def watched_gone(result: models.SourceMetrics) -> bool:
        # A programme page that has answered "not here" for long enough to be a fact
        # rather than a blip. Still printed every morning, with the date it went, but
        # it is not blind -- the server answered -- so it is neither counted as failing
        # nor escalated. Before the threshold it reads like any other failure.
        source = sources.get(result.source_id, {})
        return (
            bool(result.extra.get("gone"))
            and bool((source.get("gone_since") or "").strip())
            and int(source.get("consecutive_failures") or 0)
            >= paths.FAILURE_ESCALATION_THRESHOLD
        )

    gone = [r for r in results if not r.ok and watched_gone(r)]
    failing = [r for r in results if not r.ok and not watched_gone(r)]

    def failures(result: models.SourceMetrics) -> int:
        return int(sources.get(result.source_id, {}).get("consecutive_failures") or 0)

    blind = sum(1 for r in failing if failures(r) >= paths.FAILURE_ESCALATION_THRESHOLD)
    summary = f"{len(results)} sources checked · {len(healthy)} healthy"
    if failing:
        summary += f" · {len(failing)} FAILING"
        if blind:
            summary += f" ({blind} blind for {paths.FAILURE_ESCALATION_THRESHOLD}+ runs)"
    if gone:
        summary += f" · {len(gone)} programme page(s) taken down"
    lines = [summary]
    for result in gone:
        source = sources.get(result.source_id, {})
        last_success = (source.get("last_success") or "never")[:10]
        lines.append(
            f"· {result.source_id}: page taken down ({result.extra['gone']}) since "
            f"{source.get('gone_since')}, last seen {last_success}. Still checked every "
            "morning; its return will be a row in the digest."
        )
    escalated: list[tuple[str, str, str]] = []
    quarantined = [r for r in failing if r.quarantined]
    if quarantined:
        # Reported as its own fact. "We have stopped looking at this source" is a
        # different and more serious statement than "this fetch failed", and it is the
        # one a reader is most likely to assume did not happen.
        lines.append(
            f"⚠ {len(quarantined)} source(s) were not fetched at all — the circuit "
            f"breaker has them quarantined: "
            + ", ".join(sorted(r.source_id for r in quarantined))
        )
    groups = _shared_causes(failing, sources)
    printed_groups: set[str] = set()
    for result in failing:
        source = sources.get(result.source_id, {})
        count = int(source.get("consecutive_failures") or 0)
        last_success = source.get("last_success") or "never"
        cause = _cause(result.error)
        if cause in groups:
            if cause not in printed_groups:
                printed_groups.add(cause)
                lines.append(_group_line(cause, groups[cause], sources))
            continue
        is_blind = count >= paths.FAILURE_ESCALATION_THRESHOLD
        lines.append(
            f"⚠ {'SOURCE BLIND — ' if is_blind else ''}{result.source_id}: {count} "
            f"consecutive failure{'s' if count != 1 else ''}, last success "
            f"{last_success}. {result.error}" + _retry_note(result)
        )
        if is_blind:
            escalated.append((
                result.source_id,
                f"SOURCE BLIND — {count} failures running",
                f"not quiet, blind. Last success {last_success}. {result.error}",
            ))
    recovered: dict[str, list[str]] = {}
    for result in results:
        if result.ok and result.extra.get("retried"):
            recovered.setdefault(result.extra["retried"], []).append(result.source_id)
    for reason, ids in recovered.items():
        lines.append(
            f"· {len(ids)} source(s) were refused with {reason} on the first pass and read "
            "on a second try at the end of the run: " + ", ".join(sorted(ids)) + "."
        )
    for result in results:
        if result.baseline:
            lines.append(
                f"· {result.source_id}: first run, recorded "
                f"{result.extra.get('rows', 0)} rows as the baseline."
            )
    # The two Tier 2 counters that predate FilterReport and are still read by the
    # console summary. The typed reports for the same screens are rendered once, by the
    # caller, over the complete list -- rendering them here as well is what made a
    # digest rebuilt from the artifact print each of them twice.
    not_student = sum(r.extra.get("suppressed_not_student", 0) for r in results)
    not_us = sum(r.extra.get("suppressed_not_us", 0) for r in results)
    if not_student or not_us:
        lines.append(
            f"· job boards: {not_student} postings were not student roles and "
            f"{not_us} were outside the US."
        )
    for result in results:
        # Not a failure -- the fetch worked -- but the row is watching a page it was
        # not configured for, which is a coverage hole rather than an outage. Said out
        # loud every morning until the url is corrected, because the failure mode it
        # replaces was silence.
        redirected = result.extra.get("redirected")
        if redirected:
            lines.append(f"⚠ {result.source_id}: {redirected}")
    for result in results:
        if result.collapsed:
            lines.append(
                f"⚠ {result.source_id}: {result.collapsed.total} rows changed at once "
                "and were collapsed into one item — that reads as a board restructure."
            )
    return lines, escalated
