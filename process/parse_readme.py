"""Parse a repo README's tables into canonical snapshot rows, and judge the result.

No scraping. These are tables in public repos, so `gather.github_readme` asks the API
for the default branch and fetches the raw README, and this diffs at the row level keyed
on company + role -- not on raw text, which would fire on every badge and emoji change.

Each repo gets its own parser config because the five READMEs share almost nothing:
NUFT puts the company in the `##` heading above a two-column table, Cruz-Lopez has seven
different header layouts, Simplify uses HTML `<table>` rather than markdown, and zapply
and LuisaE fold company and role into a single linked `Name` column. A "generic markdown
table differ" would produce noise for four of the five.

The stored snapshot is a canonical one-line-per-row rendering rather than the README
itself. That is what makes `git log` readable: a newly posted role is exactly one added
line, instead of a 700KB blob reflowing.

Pure. No network, no disk -- `previous` arrives as an argument.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from core import clock, models
from process.snapshot import (  # shared with parse_ats and parse_page
    Row,
    Snapshot,
    diff_snapshots,
    parse_snapshot,
    _URL,
    _recover_posting_url,
    render_snapshot,
)

# Filter ids, so HEALTH can name which screen dropped what. These three reported
# through nothing at all until now: a section this config excludes, a section it does
# not include, and a table with no recognisable entity column were all simply skipped,
# so a `section_include` pattern that stopped matching would have silently emptied a
# source while the row count floor below cried wolf about a README restructure that had
# not happened.
SECTION_EXCLUDED = "readme-section-excluded"
SECTION_NOT_INCLUDED = "readme-section-not-included"
NO_ENTITY_COLUMN = "readme-no-entity-column"
IGNORED_COLUMNS = "readme-ignored-columns"

MAX_SAMPLES = 5

@dataclass(frozen=True)
class RepoConfig:
    """How to read one repo's tables."""

    table_format: str  # "markdown" | "html"
    entity_columns: tuple[str, ...] = ()  # candidate header names for the company
    role_columns: tuple[str, ...] = ()  # candidate header names for the role
    heading_as_entity: bool = False  # company lives in the `##` heading instead
    ignore_columns: tuple[str, ...] = ()  # columns too noisy to diff on
    # Extra columns folded into the row key. Simplify lists the same company and role
    # once per office, so company+role alone collides ~79 times and would hide roles.
    qualifier_columns: tuple[str, ...] = ()
    # Decorations that churn independently of the posting. Simplify's "🔥" means
    # "recently posted", so it silently falls off every row after a few days; left in
    # the key it makes a posting look removed and re-added.
    volatile_markers: tuple[str, ...] = ()
    section_include: re.Pattern[str] | None = None
    section_exclude: re.Pattern[str] | None = None
    # Which markdown heading depth names a section; deeper headings are ignored. 0 means
    # the nearest heading of any depth. mehek-2027 moved every table under a `###`
    # ("Summer 2027 stated in the role title") inside its `##` field headings in late
    # October 2026, so `section_include` matched nothing and the source parsed 0 rows for
    # three weeks. Per repo rather than global, because a row's section is part of its
    # diff key, and changing it for every repo at once would report every row as moved.
    section_level: int = 0
    # Spec section 10.1 plausibility floors. Pick whichever quantity is *stable* for
    # the repo: NUFT's row count legitimately drains to near zero out of season (58
    # firm tables, 39 of them empty right now), so flooring its rows would cry wolf
    # every summer. Its section count is the invariant instead.
    min_rows: int = 10
    min_sections: int = 0


# speedyapply's two lists, Quant section only (added 2026-09-26). Their FAANG+ and
# Other sections are mostly Simplify and zshah again -- 300 of 577 and 414 of 735 rows
# share a posting id with them -- but the Quant section is the one honest route to
# Citadel and Citadel Securities, whose own site refuses this client's User-Agent.
# The Company cell is an HTML anchor around <strong>, and Age is relative ("3d").
_SPEEDYAPPLY = dict(
    table_format="markdown",
    entity_columns=("Company",),
    role_columns=("Position",),
    qualifier_columns=("Location",),
    ignore_columns=("Age",),
    section_include=re.compile(r"^quant\b", re.I),
    # The Quant table drains through the autumn the way NUFT's firm tables do: the SWE
    # list's was empty and the AI list's down to 8 rows by November 2026, and a row
    # floor reported both as README restructures. The table's existence is the
    # invariant -- a renamed or removed Quant heading still fails.
    min_rows=0,
    min_sections=1,
)

REPO_CONFIGS: dict[str, RepoConfig] = {
    "speedyapply-ai-2027": RepoConfig(**_SPEEDYAPPLY),
    "speedyapply-swe-2027": RepoConfig(**_SPEEDYAPPLY),
    "nuft-2027": RepoConfig(
        table_format="markdown",
        role_columns=("Role",),
        heading_as_entity=True,
        section_exclude=re.compile(r"^(contributing|using this repository|contents)", re.I),
        min_rows=0,
        min_sections=40,
    ),
    "underclassmen-cruz": RepoConfig(
        table_format="markdown",
        entity_columns=("Company", "University/Organization", "Organization", "State"),
        role_columns=("Role", "Program", "Opportunity", "Scholarship"),
        qualifier_columns=("Location",),
        min_rows=60,
    ),
    "underclassmen-zapply": RepoConfig(
        table_format="markdown",
        entity_columns=("Name",),
        min_rows=20,
    ),
    "luisae": RepoConfig(
        table_format="markdown",
        entity_columns=("Name",),
        min_rows=60,
    ),
    "zshah-2027": RepoConfig(
        table_format="markdown",
        entity_columns=("Company",),
        role_columns=("Role",),
        qualifier_columns=("Location",),
        # Only the role sections. The prose sections above them ("What this is",
        # "Scope") are themselves two-column markdown tables, and the Drop Radar
        # table is a forecast of dates rather than a list of live openings.
        section_include=re.compile(r"^(summer 20\d\d|fall 20\d\d|recently posted)", re.I),
        # "Posted" is an absolute date, so it is stable per row -- unlike Simplify's
        # relative "Age" column. The markers are not: this list is regenerated every
        # 30 minutes and the "new this week" flag turns over constantly.
        volatile_markers=("\U0001f195", "\u2713", "\U0001f6c2"),
        min_rows=200,
    ),
    # Added 2026-09-30 from the discovery proposal of 2026-09-14.
    # One markdown table: Company | Role | Location | Application/Link | Date Posted.
    # "Date Posted" is absolute ("Aug 21"), so it is stable per row; the flags (🛂 🇺🇸
    # 🔒) come off the key generically and stay in the value, where 🔒 -- closed -- is
    # real news. Its value over Simplify is the early-stage tail (Exa, Modal, Etched,
    # Dedalus Labs, many workatastartup.com links). Last committed to on 2026-08-23, so
    # it may be dormant; a dormant repo costs one fetch a day and reports nothing.
    # Added 2026-10-01 for coverage beyond Simplify: each was measured against
    # Simplify's full list and the existing watchlist before being added. All four link
    # to the employer's own posting, which is what lets process.suppress dedupe them.
    "dreamworkhq-2027": RepoConfig(
        table_format="markdown",
        entity_columns=("Company",),
        role_columns=("Role",),
        qualifier_columns=("Location",),
        # "Added" is relative ("0d", "1mo") and "Pay" is re-estimated; both would churn.
        ignore_columns=("Added", "Pay"),
        section_include=re.compile(r"^(engineering|data science|security)\b", re.I),
        min_rows=300,
    ),
    # Read from SUMMER.md since 2026-10-08 (the url column carries the path). On
    # 2026-10-05 the README stopped carrying its "newest 75" table and became a page of
    # links, so the README parsed to zero rows. SUMMER.md is the full table: 5,452 rows
    # on 2026-10-08, 2,894 of them marked 🔒 closed, and it only grows, so the floor sits
    # well under that rather than at the old window's 40. Measured over the 24h to
    # 2026-10-08 08:24 UTC: 83 added, 30 removed, 27 changed.
    "aprameyak-2027": RepoConfig(
        table_format="markdown",
        entity_columns=("Company",),
        role_columns=("Role",),
        qualifier_columns=("Location",),
        ignore_columns=("Date Added",),
        section_include=re.compile(r"summer 2027 internships", re.I),
        min_rows=2000,
    ),
    "mehek-2027": RepoConfig(
        table_format="markdown",
        entity_columns=("Company",),
        role_columns=("Role",),
        qualifier_columns=("Location",),
        ignore_columns=("Posted", "Pay"),
        section_include=re.compile(r"^(quant|software engineering|data science)", re.I),
        section_level=2,
        min_rows=40,
    ),
    "vanshb03-2027": RepoConfig(
        table_format="markdown",
        entity_columns=("Company",),
        role_columns=("Role",),
        qualifier_columns=("Location",),
        min_rows=150,
    ),
    "simplify-2027": RepoConfig(
        table_format="html",
        entity_columns=("Company",),
        role_columns=("Role",),
        # Keep the sections in the target fields. Data Science/AI/ML was added
        # 2026-10-01: reading only SWE and Quant missed 446 in-field Simplify postings,
        # which made Simplify a larger list than the tracker rather than a subset of it.
        # Product and Hardware stay out.
        section_include=re.compile(
            r"software engineering|quantitative finance|data science", re.I),
        # "Age" is a relative timestamp ("1d", "2mo"). Diffing it would report every
        # row as changed every single day.
        ignore_columns=("Age",),
        qualifier_columns=("Location",),
        volatile_markers=("🔥",),
        min_rows=150,
    ),
}


@dataclass
class Table:
    heading: str
    headers: list[str]
    rows: list[dict[str, str]] = field(default_factory=list)


_IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")

_IMG_TAG = re.compile(r"(?is)<img\b[^>]*>")

_ANCHOR = re.compile(r'(?is)<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>')

_TAG = re.compile(r"(?s)<[^>]+>")

_COMMENT = re.compile(r"(?s)<!--.*?-->")

_TRACKING_PARAM = re.compile(r"[?&](utm_[a-z]+|ref)=[^&\s)]*")

def clean_cell(text: str) -> str:
    """Normalise one cell: drop badge images and tags, keep link targets and emoji.

    Link targets are kept deliberately -- a changed `gh_jid` means a genuinely new
    posting, which is signal. Badge images and `utm_*` parameters are pure churn.
    """
    text = _COMMENT.sub(" ", text)
    text = _IMG_MD.sub(" ", text)
    text = _IMG_TAG.sub(" ", text)
    text = _ANCHOR.sub(lambda m: f"[{m.group(2)}]({m.group(1)})", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _TRACKING_PARAM.sub("", text)
    text = text.replace(" ", " ").replace("**", "")
    return re.sub(r"\s+", " ", text).strip(" |")


def _split_markdown_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

# A trailing parenthetical that starts with a digit is a tally the README author
# regenerates, not part of the section's name: "Summer 2027 (300 employer-stated)",
# "Recently posted -- cycle not stated (179 roles)".
_HEADING_TALLY = re.compile(r"\s*\(\d[^)]*\)\s*$")


def stable_heading(text: str) -> str:
    """A section heading with its row tally removed.

    The heading is a row's `section`, so it is part of every row's snapshot text -- and
    a count inside it belongs to the whole section, which means one posting appearing
    anywhere below it rewrites *every* row and the diff reports the entire section as
    changed. That is what happened on 2026-09-14: two of zshah-2027's three counts
    ticked overnight and the run reported 469 changes over 480 rows whose own text had
    not moved, then died posting a digest past GitHub's 65,536-character issue body
    limit. It is the same failure as Simplify's relative "Age" column and its "recently
    posted" flame, and it is the third time a value that churns independently of the
    posting has been read as news.

    Anchored on a leading digit inside the parentheses so that a qualifier which is
    part of the name -- "Quantitative Finance (Advanced)" -- is left alone. Normalising
    here rather than in `clean_cell` keeps it off table cells, where a parenthetical is
    the posting's own text and is stable per row.
    """
    return _HEADING_TALLY.sub("", clean_cell(text))


def parse_markdown_tables(text: str, section_level: int = 0) -> list[Table]:
    lines = text.splitlines()
    tables: list[Table] = []
    heading = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _HEADING.match(line)
        if match:
            if not section_level or len(match.group(1)) <= section_level:
                heading = stable_heading(match.group(2))
            index += 1
            continue
        # A table is a `|` line followed by a `|---|` separator.
        if line.lstrip().startswith("|") and index + 1 < len(lines) and _SEPARATOR.match(lines[index + 1]):
            headers = [clean_cell(cell) for cell in _split_markdown_row(line)]
            table = Table(heading=heading, headers=headers)
            index += 2
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                cells = _split_markdown_row(lines[index])
                if not _SEPARATOR.match(lines[index]):
                    table.rows.append(
                        {
                            header: clean_cell(cell)
                            for header, cell in zip(headers, cells)
                        }
                    )
                index += 1
            tables.append(table)
            continue
        index += 1
    return tables


_TABLE_BLOCK = re.compile(r"(?is)<table\b.*?</table>")
_TH = re.compile(r"(?is)<th\b[^>]*>(.*?)</th>")
_TR = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr>")
_TD = re.compile(r"(?is)<td\b[^>]*>(.*?)</td>")


def parse_html_tables(text: str) -> list[Table]:
    """Parse `<table>` blocks, attributing each to the nearest preceding markdown heading.

    Simplify's README interleaves markdown headings with HTML tables, and the heading is
    the only thing that says which category a table belongs to.
    """
    headings: list[tuple[int, str]] = [
        (match.start(), stable_heading(match.group(2)))
        for match in re.finditer(r"(?m)^(#{1,6})\s+(.*)$", text)
    ]
    tables: list[Table] = []
    for block in _TABLE_BLOCK.finditer(text):
        heading = ""
        for position, title in headings:
            if position < block.start():
                heading = title
            else:
                break
        body = block.group(0)
        headers = [clean_cell(cell) for cell in _TH.findall(body)]
        table = Table(heading=heading, headers=headers)
        for row_html in _TR.findall(body):
            cells = [clean_cell(cell) for cell in _TD.findall(row_html)]
            if cells:
                table.rows.append(dict(zip(headers, cells)))
        tables.append(table)
    # Simplify emits one `<table>` per section but repeats `<tbody>` per row, so blocks
    # can nest oddly; merging same-heading tables keeps sections whole.
    merged: dict[str, Table] = {}
    for table in tables:
        existing = merged.get(table.heading)
        if existing and existing.headers == table.headers:
            existing.rows.extend(table.rows)
        else:
            merged.setdefault(table.heading, table)
    return list(merged.values())


# --------------------------------------------------------------------------- rows


def _first_present(headers: list[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in headers:
            return candidate
    return None


# Simplify writes a bare "↳" in the Company cell to mean "same company as the row
# above". Taken literally it produces hundreds of rows keyed on "↳".
_CARRY_FORWARD = ("↳", "->", "⤷")


def _strip_markers(text: str, markers: tuple[str, ...]) -> str:
    for marker in markers:
        text = text.replace(marker, "")
    return re.sub(r"\s+", " ", text).strip()


# Emoji and pictographs, for the *key* only. Enumerating a repo's decorations by hand
# is what failed on 2026-09-14: three `volatile_markers` knobs were already configured
# for zshah-2027 and a fourth flag still churned 39 postings, because the list can only
# ever hold the decorations somebody already noticed. A README author's cosmetics are
# essentially always emoji, so the generic rule carries every repo added from here on,
# including the ones whose quirks nobody has looked at yet.
#
# Arrows (U+2190-21FF) are deliberately absent: `↳` and `⤷` are the carry-forward
# markers, which mean "same company as the row above" and are resolved rather than
# dropped. Ranges, not a blanket "non-ASCII": accented company names and CJK are
# identity, and stripping them would merge real postings.
_EMOJI = re.compile(
    "[" 
    "\U0001F000-\U0001FAFF"  # pictographs, emoticons, transport, flags, supplements
    "\u2600-\u27BF"          # misc symbols and dingbats: ✅, ✓, ❗
    "\u2B00-\u2BFF"          # stars and arrows-in-boxes: ⭐
    "\uFE0F\u200D\u20E3"      # variation selector, ZWJ, combining keycap
    "]+"
)


def _key_text(text: str) -> str:
    """The identity-bearing part of a cell: emoji removed, whitespace collapsed.

    Applied to the key and never to the value. The value is the record of what the row
    actually said -- Cruz-Lopez's `Status=🔥 [CLOSING SOON]` is real signal and an
    OPEN -> CLOSING SOON transition is exactly what must be reported -- while the key is
    the diff identity, where the same character is pure churn.

    Falls back to the unstripped text when stripping would empty the cell, rather than
    dropping the row: an entity that is *only* a pictograph is still an entity, and
    losing it would report a removal and an addition every time the parser ran.
    """
    stripped = re.sub(r"\s+", " ", _EMOJI.sub(" ", text)).strip()
    return stripped or re.sub(r"\s+", " ", text).strip()


@dataclass
class _Tally:
    """One filter's running counts while a README is being flattened."""

    considered: int = 0
    removed: int = 0
    samples: list[str] = field(default_factory=list)

    def note(self, sample: str, rows: int = 1) -> None:
        self.removed += rows
        if len(self.samples) < MAX_SAMPLES and sample:
            self.samples.append(sample)


def extract(text: str, config: RepoConfig) -> Snapshot:
    """Flatten every table in the README into canonical `(section, key, value)` rows."""
    return extract_reported(text, config)[0]


def extract_reported(
    text: str, config: RepoConfig, source_id: str = ""
) -> tuple[Snapshot, list[models.FilterReport]]:
    """`extract`, plus a FilterReport for each screen that dropped something.

    Every filter reports what it removed. These three were the clearest remaining hole
    in that rule: a section excluded by config, a section not matched by
    `section_include`, and a table with no recognisable entity column were all a bare
    `continue`. So a `section_include` pattern that stopped matching -- Simplify's is
    "software engineering|quantitative finance", which a heading rename would break --
    would have emptied the source silently, and the only complaint would have been the
    row-count floor reporting a README restructure that had not happened.
    """
    if config.table_format == "markdown":
        def parser(body: str) -> list[Table]:
            return parse_markdown_tables(body, config.section_level)
    else:
        parser = parse_html_tables
    snapshot = Snapshot()
    seen_keys: dict[tuple[str, str], int] = {}
    tallies = {
        SECTION_EXCLUDED: _Tally(),
        SECTION_NOT_INCLUDED: _Tally(),
        NO_ENTITY_COLUMN: _Tally(),
        IGNORED_COLUMNS: _Tally(),
    }
    for table in parser(text):
        for tally in tallies.values():
            tally.considered += len(table.rows)
        if config.section_include and not config.section_include.search(table.heading):
            tallies[SECTION_NOT_INCLUDED].note(table.heading, len(table.rows))
            continue
        if config.section_exclude and config.section_exclude.search(table.heading):
            tallies[SECTION_EXCLUDED].note(table.heading, len(table.rows))
            continue
        entity_column = _first_present(table.headers, config.entity_columns)
        role_column = _first_present(table.headers, config.role_columns)
        if not config.heading_as_entity and not entity_column:
            # Not a listing table (legend, key, contents, ...).
            tallies[NO_ENTITY_COLUMN].note(table.heading, len(table.rows))
            continue
        for header in table.headers:
            if header and header in config.ignore_columns:
                tallies[IGNORED_COLUMNS].note(
                    f"{table.heading or '(no heading)'}: {header}",
                    sum(1 for cells in table.rows if cells.get(header)))
        if table.heading and table.heading not in snapshot.sections:
            snapshot.sections.append(table.heading)
        previous_entity = ""
        for cells in table.rows:
            entity = table.heading if config.heading_as_entity else cells.get(entity_column, "")
            entity = _strip_markers(entity, config.volatile_markers)
            if entity.strip() in _CARRY_FORWARD:
                entity = previous_entity
            elif entity.strip():
                previous_entity = entity
            if entity_column:
                # Write the resolved entity back so the value is position-independent.
                cells = {**cells, entity_column: entity}
            # Every part of the key gets the markers stripped, not just the entity.
            # The key is the diff identity, so a marker anywhere in it churns the row:
            # zshah-2027's "new this week" flag sits in the *Role* cell and turns over
            # constantly, and on 2026-09-14 it re-added 39 postings whose only change
            # was losing it. The row *value* was already stripped below, which is why
            # this showed up as an added/removed pair rather than as a `changed` row.
            role = cells.get(role_column, "") if role_column else ""
            role = _strip_markers(role, config.volatile_markers)
            # `_key_text` last, and on every part: the write-back into `cells` above is
            # what the value renders from, so the value keeps the decorations and only
            # the identity loses them.
            entity, role = _key_text(entity), _key_text(role)
            if not entity and not role:
                continue
            key = f"{entity} / {role}" if role else entity
            for qualifier in config.qualifier_columns:
                value_ = _key_text(_strip_markers(
                    cells.get(qualifier, ""), config.volatile_markers
                ))
                if value_:
                    key = f"{key} @ {value_}"
            value_parts = [
                f"{header}={_strip_markers(cells[header], config.volatile_markers)}"
                for header in table.headers
                if header and header not in config.ignore_columns and cells.get(header)
            ]
            value = "; ".join(value_parts)
            urls = _URL.findall(value)
            key = re.sub(r"\s+", " ", key)
            # Any collision that survives the qualifiers gets a stable ordinal rather
            # than being silently deduplicated away.
            seen_keys[(table.heading, key)] = seen_keys.get((table.heading, key), 0) + 1
            occurrence = seen_keys[(table.heading, key)]
            if occurrence > 1:
                key = f"{key} #{occurrence}"
            snapshot.rows.append(
                Row(
                    section=table.heading,
                    key=key,
                    value=value,
                    url=urls[0] if urls else "",
                    # Assigned here, where the parsed cells are in hand. `url` is
                    # whichever link came first, which on a Simplify row is the company
                    # page rather than the posting.
                    posting_url=_recover_posting_url(value),
                )
            )
    return snapshot, [
        models.FilterReport(
            stage="process",
            filter_id=filter_id,
            source_id=source_id,
            considered=tally.considered,
            removed=tally.removed,
            reason=_REASONS[filter_id],
            samples=tuple(tally.samples),
        )
        for filter_id, tally in tallies.items()
    ]


# A repo that moves this much of itself in one run has restructured, not restocked.
# `parse_ats` has had this guard since Phase 2 and the READMEs did not, which is the
# only reason 2026-09-14 could reach the digest at all: 480 of zshah-2027's 514 rows
# reported as changed and the pipeline treated every one as news.
#
# Proportional, unlike the ATS boards' flat 25. An aggregator legitimately posts dozens
# of real rows on a busy morning -- simplify-2027 moved 38 of 625 that same day, all of
# them genuine -- so a flat count would either collapse a real morning or never fire on
# a 600-row repo. Both conditions are required: the ratio is what catches a restructure,
# and the floor is what stops a 12-row repo tripping on three postings.
MAX_CHANGE_RATIO = 0.25
MIN_CHANGES_TO_COLLAPSE = 25

# How many of a collapsed repo's rows to name. The digest gets the count; these are for
# whoever goes and looks at why.
MAX_COLLAPSE_SAMPLES = 10

_REASONS = {
    SECTION_EXCLUDED: "section matched this repo's section_exclude pattern",
    SECTION_NOT_INCLUDED: "section did not match this repo's section_include pattern",
    NO_ENTITY_COLUMN: "table has no recognisable company column, so it is a legend or "
                      "a contents list rather than a listing",
    IGNORED_COLUMNS: "column values dropped as per-run churn rather than news, per "
                     "this repo's ignore_columns (Simplify's relative \"Age\" column "
                     "would otherwise report every row as changed every day)",
}


def assess(
    source: dict[str, str],
    config: RepoConfig | None,
    fetched,
    previous: str | None,
) -> models.SourceResult:
    """Decide what one README fetch means. Pure.

    `previous` is the last committed snapshot text, or None for "never seen".
    """
    source_id = source["source_id"]
    if config is None:
        return models.SourceResult(
            source_id=source_id, ok=False, error=f"no parser config for {source_id!r}")

    attempt = fetched.attempt
    if not attempt.ok:
        return models.SourceResult(
            source_id=source_id, ok=False, error=attempt.error,
            content_length=attempt.size)

    parsed, filters = extract_reported(fetched.text, config, source_id)
    size, branch = attempt.size, fetched.branch

    # Spec section 10.1: a redesign that yields nothing parseable must be an alert, not
    # a silent "nothing changed today". Pick whichever quantity is *stable* for the
    # repo -- NUFT's row count legitimately drains to near zero out of season, so
    # flooring its rows would cry wolf every summer and its section count is the
    # invariant instead.
    shortfall = ""
    if len(parsed.rows) < config.min_rows:
        shortfall = f"parsed only {len(parsed.rows)} rows, expected at least {config.min_rows}"
    elif len(parsed.sections) < config.min_sections:
        shortfall = (
            f"parsed only {len(parsed.sections)} sections, "
            f"expected at least {config.min_sections}"
        )
    if shortfall:
        return models.SourceResult(
            source_id=source_id,
            ok=False,
            content_length=size,
            # The filter reports ride along on the failure too. When a section pattern
            # stops matching, "excluded 514 of 514 rows" is the answer to why the floor
            # fired, and withholding it until the next successful run is withholding it
            # exactly when it is needed.
            filters=filters,
            error=(
                f"{shortfall} (from {size} bytes on branch {branch}). "
                "Likely a README restructure."
            ),
        )

    rendered = render_snapshot(parsed)
    extra = {"rows": len(parsed.rows), "sections": len(parsed.sections), "branch": branch}
    if previous is None:
        return models.SourceResult(
            source_id=source_id,
            ok=True,
            content_length=size,
            snapshot_text=rendered,
            baseline=True,
            extra=extra,
            filters=filters,
        )

    previous_snapshot = parse_snapshot(previous)
    changes = diff_snapshots(source_id, previous_snapshot, parsed)
    for change in changes:
        change.program_name = source.get("program_names", "")

    # Measured against whichever snapshot is larger, so a repo that empties is caught by
    # the same rule as one that doubles: 500 rows becoming 5 is 495 removals, which is a
    # format break and not a hiring freeze. Counting removals here is deliberate even
    # though `process.suppress` drops them from the digest downstream -- a mass removal
    # is the single clearest signal that a parser has stopped matching, and this guard
    # is the last place it is still visible.
    collapsed = None
    scale = max(len(parsed.rows), len(previous_snapshot.rows), 1)
    if len(changes) >= MIN_CHANGES_TO_COLLAPSE and len(changes) > MAX_CHANGE_RATIO * scale:
        samples = tuple(f"{c.kind}: {c.key}" for c in changes[:MAX_COLLAPSE_SAMPLES])
        collapsed = models.BoardCollapse(total=len(changes), samples=samples)
        extra["collapsed"] = len(changes)
        # The new snapshot is still returned and still committed, so the run re-baselines
        # and tomorrow diffs against today rather than reporting the same storm again.
        # Nothing is lost by withholding the rows: git is the database, and the state
        # commit records exactly which lines moved on which morning either way.
        changes = [
            models.Change(
                source_id=source_id,
                kind="changed",
                change_id=clock.change_id(source_id, "repo-restructure"),
                structural=True,
                key=(
                    f"{source_id}: {collapsed.total} of {scale} rows changed at once"
                ),
                detail=(
                    f"{collapsed.total} of {scale} rows moved in a single run "
                    f"({collapsed.total / scale:.0%}), which reads as a README "
                    "restructure rather than news. The snapshot has been re-baselined, "
                    f"so this will not repeat tomorrow. First {len(samples)}:\n"
                    + "\n".join(f"- {s}" for s in samples)
                ),
                url=source.get("url", ""),
                program_name=source.get("program_names", ""),
            )
        ]

    return models.SourceResult(
        source_id=source_id,
        ok=True,
        changes=changes,
        content_length=size,
        snapshot_text=rendered,
        extra=extra,
        filters=filters,
        collapsed=collapsed,
    )
