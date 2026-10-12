"""Render the digest body. Delivery itself is `deliver.issue`.

Issues rather than email: GitHub emails the owner when an issue is opened in their own
repo, so there is no SMTP, no API key that expires silently, and no deliverability
problem.

The body is one table -- urgency, company, position, notes -- and nothing else but the
calendar and health footers. It used to be two prose sections of checkbox list items,
which read as an explanation of the project rather than a list of things to go and do.
The checkboxes went with it: a GitHub task list only renders as a tickable box in a list
item, never inside a table cell, so the table and tick-to-dismiss were mutually
exclusive. `data/applied.tsv` survives as a hand-editable mute list.

Cadence is exactly one digest a day, every day -- no more and no less. A reminder has
to be reliable, and a fixed daily arrival is what makes silence diagnostic: no issue on a given morning means the job is broken, with no "maybe nothing
changed" ambiguity to explain it away. The cost is that quiet days mail too, so a quiet
day says so in the title ("(no changes)") and stays short enough to archive in a glance.

The "no more" half is load-bearing in the other direction. The schedule fires one tick
now, but a manual `workflow_dispatch` can still run alongside it -- that happened on
2026-09-14, when a late tick and a hand-dispatched run overlapped by twenty-one seconds
-- and either would otherwise be free to open its own issue. Two digests for the same
date is the same notification-fatigue failure as a daily "0 changes" email, so delivery
is capped -- see should_send and delivered_issue_exists.
"""
from __future__ import annotations

import datetime
import re

import calendar_reminders
from core import clock, models, paths, profile
from deliver import health, urgency
from enrich import bodies as enrich_bodies
from process import parse_readme

def _rollover_line(sources: dict[str, dict]) -> str | None:
    """Warn that the class-standing rules and season-named sources are for last season.

    Nothing else would: a ruled-out sophomore programme still prints in RULED OUT
    looking deliberate, and a 2027 list that has gone quiet passes every health check.
    """
    soon = (datetime.date.fromisoformat(clock.today_iso())
            + datetime.timedelta(days=profile.ROLLOVER_WARN_DAYS)).isoformat()
    if clock.recruiting_cycle(soon) <= profile.RULES_CYCLE:
        return None
    season = str(profile.RULES_CYCLE)
    named = sorted(sid for sid, row in sources.items()
                   if row.get("method") != "manual"
                   and (season in sid or season in row.get("url", "")))
    return (
        f"⚠ The Summer {profile.RULES_CYCLE + 1} season is starting, and this tracker "
        f"is still set up for Summer {profile.RULES_CYCLE}. The class-standing rules "
        "assume a first-year (OWNER_PROFILE, classify.SYSTEM_PROMPT rules 1 and 6, "
        "screen/rules.py sophomore and advanced-standing), so sophomore programmes are "
        f"being ruled out. Season-named sources to replace with next year's lists: "
        f"{', '.join(named) or 'none'}; also paths.SIMPLIFY_LISTINGS_URL. Then bump "
        "RULES_CYCLE in core/profile.py."
    )


def _stale_profile_line() -> str | None:
    """Nag when the owner profile has not been reviewed in a while.

    Every relevance call in the digest is made against `core.profile.OWNER_PROFILE`. When
    it drifts out of date nothing breaks visibly -- the digest still renders, the
    judgments still look confident, they are just answering last year's question. The
    CALENDAR block is the right home because it already carries recurring
    human-action reminders, and this line costs nothing on the days it does not fire.
    """
    try:
        reviewed = datetime.date.fromisoformat(profile.PROFILE_LAST_REVIEWED)
    except ValueError:
        return None
    days = (datetime.date.fromisoformat(clock.today_iso()) - reviewed).days
    if days < profile.PROFILE_REVIEW_AFTER_DAYS:
        return None
    return (
        f"Your interest profile was last reviewed {reviewed.isoformat()} "
        f"({days // 30} months ago) - are quant and maths still the priority? Every "
        "relevance call in this digest assumes so. Edit OWNER_PROFILE in core/profile.py "
        "and bump PROFILE_LAST_REVIEWED."
    )


# Nothing in the table is truncated. It used to be: a 96-character budget on Notes and
# 70 on Position, on the theory that GitHub wraps a long cell rather than scrolling it
# and an unbounded `why` turns four tidy rows into a wall of text.
#
# That traded the wrong thing away. On the 2026-09-13 digest every Notes cell ended in
# an ellipsis two lines in -- and because the model
# writes `why` as one sentence beginning with the posting's requirement, the clause
# that got cut was reliably the decisive one ("...and is a Summer 2027 role, so a
# first-year" — then nothing). A clipped sentence is worse than a tall cell: it ends
# mid-clause and the only way to recover the rest is to open the posting, which is the
# work the digest exists to save. Height is now managed by splitting the cell into
# labelled bullets instead, so a long note is scannable rather than a paragraph.
#
# Bullets inside a table cell are `<br>`-joined: GitHub renders a line break there but
# does not render a markdown list, so "- " per line would print literal hyphens.
BULLET_JOIN = "<br>"
BULLET = "• "

ROLLING_NOTE = "rolling — closes when full"

# Past this many sources, a filter line reports the count instead of the names.
MAX_FILTER_SOURCES = 6

# The first column is the tier and the reason for it, since 2026-10-08: "High · startup /
# small firm", "Medium · large firm". The tiers were ACT NOW and WORTH A
# LOOK before that, and nothing on the row said which test had put it there.
PRIORITY_HIGH = f"**{urgency.HIGH}**"
PRIORITY_MEDIUM = urgency.MEDIUM


def _priority(tier: str, reason: str) -> str:
    label = PRIORITY_HIGH if tier == urgency.HIGH else PRIORITY_MEDIUM
    return f"{label} · {reason}" if reason else label


def _cell(text: str) -> str:
    """Flatten arbitrary text into something safe inside a markdown table cell.

    Two hazards, both of which have to be handled here rather than at the call sites: a
    literal pipe ends the cell, and a newline ends the whole row. Length is deliberately
    not one of them -- see the note on the bullet constants above.
    """
    return re.sub(r"\s+", " ", (text or "").replace("|", "\\|")).strip()


def _bullets(items: list[tuple[str, str]]) -> str:
    """(label, value) pairs as a bulleted list inside one table cell.

    An empty value is dropped rather than printed as "Year: not specified": a bullet
    that costs a line and says only that the posting was silent is noise, and its
    absence already carries the same information. An empty *label* is allowed, for the
    caveats, which read as sentences rather than as fields.
    """
    lines = []
    for label, value in items:
        value = _cell(value).rstrip(" .;,")
        if not value:
            continue
        lines.append(f"{BULLET}**{label}:** {value}" if label else f"{BULLET}{value}")
    return BULLET_JOIN.join(lines)


# An aggregator row's key carries the real employer, as a markdown link:
# "[InfiniteQuant](https://simplify.jobs/c/InfiniteQuant) / Quantitative Trader @ NYC".
# Matching the link shape rather than splitting every key on " / " is deliberate: a
# job-board key is "Title @ Location", and a title containing a slash ("Software
# Engineer / Backend") would otherwise be read as a company called "Software Engineer".
#
# The link is not the only shape, though: zshah-2027's Company cell is plain text, so its
# keys are "ABB / AI Robotics Intern @ Milpitas" and every one of them printed the list's
# own name as the company (2026-09-26, 14 rows). `parse_readme` builds every aggregator
# key as f"{entity} / {role}", so for those sources -- and only those -- the first " / "
# is the employer boundary by construction, linked or not.
_LINKED_ENTITY = re.compile(r"^\[([^\]]+)\]\([^)]*\)\s*/\s*(.+)$")
_PLAIN_ENTITY = re.compile(r"^(.+?)\s+/\s+(.+)$")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _unlink(text: str) -> str:
    """Markdown link syntax down to its label.

    Escaping the brackets instead, which is what the first version did, rendered
    "[InfiniteQuant](url) / Quantitative Trader" as "(InfiniteQuant)(url) /
    Quantitative Trader" inside the cell -- the URL printed twice and the employer in
    parentheses.
    """
    return _MD_LINK.sub(r"\1", text)


def _company_and_position(judgment: models.Judgment) -> tuple[str, str]:
    """Split one change into the table's company and position columns.

    Job-board rows are already keyed "Title @ Location", which is exactly the position
    column. Page-text rows are keyed on the programme name instead -- "Optiver
    FutureFocus (3 added, 1 removed)" -- so the company name would otherwise be printed
    twice and waste the only two columns that carry the identity of the thing.
    """
    change = judgment.change
    company = judgment.program_name or change.program_name or change.source_id
    position = change.key

    # Prefer the employer named in the row over the aggregator that carried it. For a
    # Simplify or zshah row, `program_names` is the list's own name, which is the same
    # useless string on every one of its 500+ rows.
    # This also has to win over `judgment.program_name`: the prompt names the
    # aggregator as the "tracked program", and the model hands that name straight back.
    linked = _LINKED_ENTITY.match(position)
    if not linked and change.source_id in parse_readme.REPO_CONFIGS and not change.structural:
        linked = _PLAIN_ENTITY.match(position)
    if linked:
        # .strip(): speedyapply's anchor wraps <strong>, which leaves "[ HRT ](url)".
        return _unlink(linked.group(1)).strip(), _unlink(linked.group(2)).strip()

    position = _unlink(position)
    if company and position.lower().startswith(company.lower()):
        position = position[len(company):].lstrip(" -–—:·,").strip()
        # What is left of a page-text key is the bare change count, "(2 added, 0
        # removed)", which is not a position and reads as a typo as a link label.
        if position.startswith("("):
            position = f"page updated {position}"
    return company, position or "page updated"


def _deadline_note(judgment: models.Judgment) -> str:
    """The deadline bullet's text: the rolling sentinel spelled out, or the date.

    A value the model returned that is neither `rolling` nor an ISO date is still
    shown, verbatim. It just never counts as urgent -- see `deliver.urgency`. Showing
    it is the point: a deadline the code could not parse is exactly the one a
    human needs to read.
    """
    if judgment.change.rolling or urgency.is_rolling(judgment):
        return ROLLING_NOTE
    return judgment.deadline


def _notes(judgment: models.Judgment) -> str:
    """The Notes cell: three labelled facts, then the caveats.

    Bullets rather than one run-on clause, because this column answers separate
    questions, read in this order: how long is left (Deadline), is the posting open to
    this applicant at all (Year), and where is it (Location). They used to be crushed into a
    single semicolon-joined sentence, which is what made the old truncation so costly --
    the class year and the location were inside the model's one prose sentence, so they
    were both only ever present by luck.

    **`why` is deliberately not here.** It was a fourth bullet justifying the row's
    presence, and it was removed on 2026-09-17 because the justification is redundant: a
    row in the table has already been judged relevant, so explaining that it is worth
    looking at repeats what its appearance in the digest already says.
    It was also the longest bullet by a wide margin. `why` is still requested, still
    stored on the Judgment, still in the artifacts and the state commit, and still
    printed in RULED OUT, as a few words -- where the row's presence proves the opposite.

    Ordered most-decision-relevant first. A rolling deadline changes what the owner
    does today; a confidence caveat only changes how far to trust a row already being
    read, so it goes last.
    """
    items: list[tuple[str, str]] = [
        ("Deadline", _deadline_note(judgment)),
        ("Year", judgment.class_year),
        ("Location", judgment.location),
    ]
    if not judgment.classified:
        items.append(("", "⚠ unverified — open the page"))
    elif judgment.confidence == "low":
        items.append(("", "low confidence, kept deliberately"))
    cell = _bullets(items)
    if cell:
        return cell
    # Nothing the posting stated and no caveat, which a page diff can produce: the
    # change is that a watched page moved, not that a posting appeared, so there is no
    # deadline or class year to report. An empty table cell reads as a bug, so fall back
    # to naming the kind of change. `why` is not used even here -- it is the one thing
    # this cell is no longer for.
    return {
        "removed": f"{BULLET}row disappeared from the source",
        "changed": f"{BULLET}page changed on the source",
    }.get(judgment.change.kind, f"{BULLET}no details stated on the posting")


VALUE = "value"
ELIGIBILITY = "eligibility"

# The deterministic screen's own `why` quotes the posting at length, which is what
# `.run/screened.json` is for. The digest prints only the rule, in a few words: RULED OUT
# had grown to 145 rows and 39,494 characters at a median of 271 a row, the largest block
# in the body, and a reason the reader can take in at a glance is all it needs to carry.
_SCREEN_LABELS = {
    "graduation-window": "graduates before 2030",
    "already-graduated": "degree already required",
    "advanced-standing": "juniors and above",
    "graduate-only": "graduate students only",
    "sophomore-title": "sophomores only",
}

# The field a title-only rule-out matched, e.g. ("Civil Engineering").
_QUOTED = re.compile(r"[\"\u201c]([^\"\u201c\u201d]{3,60})[\"\u201d]")


def _ruled_out_line(judgment: models.Judgment) -> str:
    """One RULED OUT entry: the row and its reason in a few words.

    The model is asked for a two-to-five-word phrase when it rules a row out, so its
    `why` is printed as it stands. A screen rule-out is printed as its rule's label.
    """
    why = judgment.why or "not relevant"
    if judgment.screen_rule == "out-of-field-title":
        quoted = _QUOTED.search(why)
        why = f"{quoted.group(1)} role" if quoted else "out-of-field role"
    elif judgment.screen_rule in _SCREEN_LABELS:
        why = _SCREEN_LABELS[judgment.screen_rule]
    return f"- **{judgment.change.key}** — {why}"


def _table_row(priority: str | None, company: str, position: str, notes: str,
               url: str = "") -> str:
    """One table row. `priority` None is the underclassman block, which has no tier."""
    company = _cell(company)
    position = _cell(position)
    if url:
        # Any remaining brackets would terminate the link text early. By here the
        # markdown links are already reduced to their labels by `_unlink`, so this is
        # a backstop for a literal bracket in a job title.
        label = position.replace("[", "(").replace("]", ")")
        position = f"[{label}]({url})"
    if priority is None:
        return f"| {company} | {position} | {notes} |"
    return f"| {priority} | {company} | {position} | {notes} |"


def company_and_position(judgment: models.Judgment) -> tuple[str, str]:
    """Public alias. `jobs.daily` needs the same employer/position split the table uses
    when it records a pin, so the pinned row reads identically to the row it came from
    -- and so the two cannot drift into disagreeing about who the employer is."""
    return _company_and_position(judgment)


def _pinned_row(pin: models.PinnedRow, today: str) -> str:
    """A carried-over pin, deliberately compact.

    A pin is in the table because it is still open, not because anything happened to
    it, so it costs one line of facts rather than the full bullet cell: the owner has
    already read the cell on the morning the row arrived, and repeating it every day
    until the posting comes down is what would make a standing block unreadable -- and,
    at 577 bytes a full row, undeliverable. "pinned <date>" is there so a row that has
    sat open for three weeks says so instead of reading as today's news.
    """
    facts = [bit for bit in (pin.deadline, pin.class_year, pin.location) if bit.strip()]
    facts.append(f"pinned {pin.first_pinned}" if pin.first_pinned != today else "pinned today")
    return _table_row(None, pin.company, pin.position, _cell(" · ".join(facts)), pin.url)


def _judgment_row(judgment: models.Judgment, ranked: bool = True) -> str:
    company, position = _company_and_position(judgment)
    return _table_row(
        _priority(*urgency.rank(judgment)) if ranked else None,
        company,
        position,
        _notes(judgment),
        judgment.change.url,
    )


def render(
    judgments: list[models.Judgment],
    results: list[models.SourceMetrics],
    sources: dict[str, dict[str, str]],
    suppressed_applied: int = 0,
    suppressed_muted: int = 0,
    discovery_lines: list[str] | None = None,
    status_only: bool = False,
    enriched: dict[str, enrich_bodies.PostingBody] | None = None,
    filters: list[models.FilterReport] | None = None,
    usage: models.Usage | None = None,
    pinned: list[models.PinnedRow] | None = None,
    coverage_line: str | None = None,
) -> tuple[str, str]:
    """Return (issue title, issue body).

    `status_only` says there was no news -- it only picks the title. The body is the
    same shape every day: on a quiet day that is the calendar block and the health
    block, which is exactly what a reminder with nothing to report should look like.
    """
    today = clock.today_iso()
    health_lines, escalated = health.source_lines(results, sources)

    # Each tier is sorted by its reason, best first (`urgency.order`). A stable sort, so
    # the order within a reason is unchanged and trimming from the end still drops the
    # least interesting row.
    underclass = [j for j in judgments if urgency.in_top_block(j)]
    high = sorted([j for j in judgments if urgency.is_urgent(j)], key=urgency.order)
    medium = sorted([j for j in judgments if j.relevant and not urgency.is_urgent(j)
                     and not urgency.in_top_block(j)], key=urgency.order)
    ruled_out = [j for j in judgments if not j.relevant]

    if escalated:
        title = f"Opportunity digest — {today} (source failing)"
    elif status_only:
        # Quiet days mail now, so the title has to carry the whole message for an owner
        # triaging a notification list without opening anything.
        title = f"Opportunity digest — {today} (no changes)"
    else:
        title = f"Opportunity digest — {today}"

    body: list[str] = [f"Opportunity digest — {today}", ""]

    # Degraded mode announces itself once, at the top. When every unclassified item
    # shares one reason -- almost always "no classifier credentials are configured" --
    # that reason is a fact about the whole run rather than about any row, and the rows
    # carry "unverified - open the page" individually anyway. This used to also suppress
    # a per-row `why` bullet; the bullet is gone, so all that remains is the notice.
    reasons = {j.why for j in judgments if not j.classified and j.why}
    if len(reasons) == 1:
        body.append(f"> Note: {reasons.pop()}")
        body.append("")

    # Postings built for first- or second-years come first, in their own block, since
    # 2026-10-08. They are the rows this owner is most likely to land, and they used to
    # compete for the top tier with every posting that had a deadline. A pin keeps a row
    # here every morning until its posting comes down; one that moved today prints its
    # full cell, and a carried one prints compactly -- see `_pinned_row`. Stanford's own
    # programme pages join them on the morning they change; see `urgency.is_stanford`.
    moved = {j.change.change_id for j in judgments}
    first_years = [_judgment_row(j, ranked=False) for j in underclass]
    first_years += [_pinned_row(pin, today) for pin in (pinned or [])
                    if pin.change_id not in moved]
    if first_years:
        body.append(f"## ■ FOR FRESHMEN & UNDERCLASSMEN ({len(first_years)})")
        body.append("")
        body.append("_Built for first- or second-years, plus Stanford's own programmes. A "
                    "posting stays here until it comes down; a Stanford page shows on the "
                    "morning it changes._")
        body.append("")
        body.append("| Company | Position | Notes |")
        body.append("|---|---|---|")
        body += first_years
        body.append("")

    # Everything else is one table sorted so every High row sits above every Medium
    # row, and the first column says which test put it there.
    #
    # Collected as a list rather than appended straight onto the body, because the table
    # is the one block that can be trimmed if the whole digest will not fit. Built in
    # priority order, so trimming from the end always drops the least urgent row.
    table: list[str] = [
        _table_row(_priority(urgency.HIGH, "source blind"), source_id, position,
                   _cell(notes))
        for source_id, position, notes in escalated
    ]
    table += [_judgment_row(j) for j in high]
    table += [_judgment_row(j) for j in medium]
    table_index = None
    if table:
        body.append(f"## ■ OPPORTUNITIES ({len(table)})")
        body.append("")
        body.append("| Priority | Company | Position | Notes |")
        body.append("|---|---|---|---|")
        table_index = len(body)
        body += table
        body.append("")

    if ruled_out:
        body.append(f"<details><summary>■ RULED OUT ({len(ruled_out)})</summary>")
        body.append("")
        body.append("_Read and judged out of scope, with the reason in a few words._")
        body.append("")
        # Value first: they are the arguable ones, and the reader who opens this block
        # is usually opening it to disagree with one.
        for judgment in sorted(ruled_out, key=lambda j: j.ruled_out_by != VALUE):
            body.append(_ruled_out_line(judgment))
        body.append("")
        body.append("</details>")
        body.append("")

    if discovery_lines:
        body.append("## ■ DISCOVERED")
        body += discovery_lines
        body.append("")

    month, reminders = calendar_reminders.for_month()
    body.append(f"## ■ CALENDAR ({month})")
    for reminder in reminders or ["Nothing scheduled for this month."]:
        body.append(f"- {reminder}")
    for reminder in calendar_reminders.ALWAYS:
        body.append(f"- {reminder}")
    for line in (_rollover_line(sources), _stale_profile_line()):
        if line:
            body.append(f"- {line}")
    body.append("")

    body.append("## ■ HEALTH")
    for line in health_lines:
        body.append(f"- {line}")
    # Weekly, with discovery. Off the artifacts for the same reason DISCOVERED is.
    if coverage_line:
        body.append(f"- {coverage_line}")
    # What the run cost, in the run's own report. A cost that only appears on a billing
    # page a day later is a cost nobody notices drifting upward.
    # Before the spend line, because it explains part of it: a change the screen
    # settled is a model call that did not happen.
    # Before the run-wide filter lines, because it comes first in the pipeline and
    # because a posting that could not be read is the reason a screen had no opinion
    # about it.
    enrich_line = enrich_bodies.health_line(enriched or {})
    if enrich_line:
        body.append(f"- {enrich_line}")
    # Every filter in the pipeline reports through one shape and is rendered by one
    # function, here: the per-source screens each checker applied, the two run-wide
    # mute filters, and the deterministic screen. The screen's tally used to be a
    # bespoke sentence read off a module global, which is why a stage could not be run
    # on its own -- `render` on a fresh process printed an empty line against
    # freshly-initialised counters.
    for line in health.filter_lines(filters or []):
        body.append(f"- {line}")
    spend = health.usage_line(usage or models.Usage())
    if spend:
        body.append(f"- {spend}")
    if pinned:
        # A standing block is the one part of the digest that can grow without anyone
        # deciding to grow it, so its size is stated rather than left to be counted.
        # Carried rows print no reasoning, so without this line a reader could not tell
        # a block that is working from one that has quietly filled up with stale pins.
        carried = sum(1 for pin in pinned if pin.change_id not in moved)
        body.append(
            f"- {len(pinned)} underclassman posting(s) pinned to the top block while they "
            f"stay on their boards; {carried} carried over from an earlier morning. "
            "A pin is retired only when its source is checked and no longer lists it."
        )
    if suppressed_applied:
        body.append(
            f"- {suppressed_applied} item(s) are muted in data/applied.tsv and were "
            "hidden. Delete the line to bring one back."
        )
    if suppressed_muted:
        # This filter reported through nothing at all until 2026-09-12, which is how it
        # went unnoticed that it was not filtering either.
        body.append(
            f"- {suppressed_muted} item(s) were hidden because every programme their "
            "source informs is muted=true in data/programs.csv."
        )
    if ruled_out:
        # Surfaced here as well as in the collapsed block: the filter silently eating
        # real opportunities is the failure mode worth noticing, and an implausible
        # count is the cheapest signal that it is happening.
        body.append(
            f"- {len(ruled_out)} of {len(judgments)} changes were filtered out by the "
            "classifier (see RULED OUT above)."
        )

    return title, _fit(body, table, table_index, len(judgments))


def _fit(
    body: list[str], table: list[str], table_index: int | None, total: int
) -> str:
    """Join the body, trimming the table if GitHub would refuse the whole thing.

    A 422 for an over-long body opens no issue at all, so without this a busy morning
    loses the entire digest rather than its overflow -- the calendar, the health block
    and every urgent row with it. That is the failure this project cares about most:
    silence is supposed to mean broken, and it did, but the reader has no way to know
    which morning it was or what it would have said.

    Trimming is the last resort and not a strategy. The digest is short because the
    screen and the classifier are selective; when it is still too long, the least urgent
    rows go, the count says exactly how many, and the state commit has all of them. It
    does nothing at all to a digest that already fits, so the byte-identical re-render
    property is untouched on every normal day.
    """
    rendered = "\n".join(body)
    if len(rendered) <= paths.MAX_ISSUE_BODY_CHARS or table_index is None:
        return rendered

    head, tail = body[:table_index], body[table_index + len(table):]
    # Recomputed inside the loop: the note's own length is part of what has to fit, and
    # its digits change as rows come off.
    for keep in range(len(table) - 1, -1, -1):
        note = (
            f"- _{len(table) - keep} of {len(table)} rows did not fit GitHub's "
            f"{paths.MAX_ISSUE_BODY_CHARS:,}-character issue body limit and were "
            "withheld, least urgent first. All of them are in this morning's state "
            "commit._"
        )
        candidate = "\n".join(head + table[:keep] + [""] + [note] + tail)
        if len(candidate) <= paths.MAX_ISSUE_BODY_CHARS:
            return candidate
    # Every row removed and it still does not fit, so the overflow is not the table.
    # Return the un-trimmed body rather than a silently mangled one: the delivery will
    # fail loudly, which is the correct outcome for a bug this is not equipped to fix.
    return rendered
