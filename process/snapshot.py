"""Snapshot shape shared by every source tier.

Extracted from `github_repos` when Tier 2 (ATS job boards) and Tier 3 (page text)
arrived: all three tiers answer the same question -- what does this source look like
today, and what moved since yesterday -- and they must answer it identically or the
digest stops being comparable across sources.

The canonical form is a sorted `# sections` / `# rows` TSV. Sorting is what makes a
newly posted role exactly one added line in `git diff` rather than a reflowed blob,
which is the whole reason state lives in this repo.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from core import clock, models


# Spec section 5: a new row whose title looks like an underclassman program is a
# candidate new program, not just a new posting.
DISCOVERY_PATTERN = re.compile(
    r"first.?year|freshman|sophomore|underclass|insight|discovery|invitational"
    r"|immersion|explore|ignite|launch|winternship|pre.?intern",
    re.IGNORECASE,
)

# Spec section 8 rule 4: these review on a rolling basis and close when full, so any
# change on a row mentioning them is high priority regardless of stated deadline.
ROLLING_PATTERN = re.compile(
    r"jane\s*street|nvidia|d\.?\s*e\.?\s*shaw|deshaw|point\s*72|cubist",
    re.IGNORECASE,
)




@dataclass
class Row:
    section: str
    key: str
    value: str
    url: str = ""
    # The fetchable posting page for this row, when the producer knew which link that
    # was. Deliberately NOT rendered into the snapshot: the committed TSV format is
    # frozen (74 files and every past diff depend on it), so a row recovered by
    # `parse_snapshot` gets a best-effort value instead -- which is only ever needed
    # for a `removed` row, whose posting has gone anyway.
    posting_url: str = ""

    @property
    def identity(self) -> str:
        """Diff key. Section-qualified: Cruz-Lopez and Simplify both list the same
        company in more than one table, and an unqualified key would silently merge
        them and hide one of the two."""
        return f"{self.section} :: {self.key}"


@dataclass
class Snapshot:
    """What we remember about a repo between runs.

    Sections are tracked separately from rows because an empty table under a firm
    heading is meaningful: it means "this firm has no open roles right now", which is
    different from "this firm is not in the list". Without it, a firm posting its first
    role would look like a brand-new section.
    """

    sections: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)


# --------------------------------------------------------------------------- text

_IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_IMG_TAG = re.compile(r"(?is)<img\b[^>]*>")
_ANCHOR = re.compile(r'(?is)<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>')
_TAG = re.compile(r"(?s)<[^>]+>")
_COMMENT = re.compile(r"(?s)<!--.*?-->")
_TRACKING_PARAM = re.compile(r"[?&](utm_[a-z]+|ref)=[^&\s)]*")


_URL = re.compile(r"https?://[^\s)\]]+")

# The one link shape in the watchlist that is fetchable as a posting. Measured: rows
# put the employer's ATS link and the Simplify link in the same cell, ATS first, and
# only the second returns readable text -- a Workday posting renders entirely in
# JavaScript and yields zero characters.
_FETCHABLE_POSTING = re.compile(r"https://simplify\.jobs/p/[0-9a-fA-F-]+")


# How two of the watched aggregators mark a posting that has closed: the row stays and
# its apply link becomes a padlock. Measured 2026-10-11 across every committed snapshot,
# 3,035 rows carry it (aprameyak 2,984, vanshb03 51) and none of them still has a link.
# The no-link half of the test is what keeps a row that merely mentions the emoji.
_CLOSED_MARKER = "\U0001f512"


def is_closed(value: str) -> bool:
    """True when a row's value says the posting is closed and offers no link to it."""
    return _CLOSED_MARKER in value and not _URL.search(value)


def _recover_posting_url(value: str) -> str:
    """Best effort, for a row read back out of a committed snapshot.

    The live path does not use this: the producer records `posting_url` while it still
    has the parsed cells. This exists because the snapshot format is frozen, so a row
    recovered from a TSV has to recover its typed roles from the rendered value.
    """
    match = _FETCHABLE_POSTING.search(value)
    return match.group(0) if match else ""

SECTIONS_HEADER = "# sections"
ROWS_HEADER = "# rows"


def render_snapshot(snapshot: Snapshot) -> str:
    """Canonical, sorted text. This is what gets committed.

    Sorting matters: it means a newly posted role is exactly one added line in
    `git diff`, rather than a reflowed blob.
    """
    lines = [SECTIONS_HEADER]
    lines += sorted(set(snapshot.sections))
    lines.append(ROWS_HEADER)
    lines += sorted({f"{row.section}\t{row.key}\t{row.value}" for row in snapshot.rows})
    return "\n".join(lines) + "\n"


def parse_snapshot(text: str) -> Snapshot:
    snapshot = Snapshot()
    block = ROWS_HEADER  # tolerate a legacy rows-only snapshot
    for line in text.splitlines():
        if not line.strip():
            continue
        if line in (SECTIONS_HEADER, ROWS_HEADER):
            block = line
            continue
        if block == SECTIONS_HEADER:
            snapshot.sections.append(line)
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        section, key, value = parts[0], parts[1], "\t".join(parts[2:])
        urls = _URL.findall(value)
        snapshot.rows.append(
            Row(section=section, key=key, value=value, url=urls[0] if urls else "",
                posting_url=_recover_posting_url(value))
        )
    return snapshot


def diff_snapshots(source_id: str, old: Snapshot, new: Snapshot) -> list[models.Change]:
    """Row-level diff keyed on company + role (spec section 5)."""
    old_by_key = {row.identity: row for row in old.rows}
    new_by_key = {row.identity: row for row in new.rows}
    changes: list[models.Change] = []

    # A brand-new firm section is worth calling out in its own right (spec section 5).
    for section in sorted(set(new.sections) - set(old.sections)):
        if section:
            changes.append(
                models.Change(
                    source_id=source_id,
                    kind="added",
                    change_id=clock.change_id(source_id, f"section :: {section}"),
                    key=f"new section: {section}",
                    structural=True,
                    detail=f'A section that was not in the previous snapshot: "{section}".',
                    is_discovery_candidate=True,
                    rolling=bool(ROLLING_PATTERN.search(section)),
                )
            )

    for key in sorted(new_by_key.keys() - old_by_key.keys()):
        row = new_by_key[key]
        changes.append(
            models.Change(
                source_id=source_id,
                kind="added",
                change_id=clock.change_id(source_id, key),
                key=row.key,
                detail=f"New row: {row.value}",
                url=row.url,
                posting_url=row.posting_url,
                closed=is_closed(row.value),
                is_discovery_candidate=bool(DISCOVERY_PATTERN.search(f"{row.key} {row.value}")),
                rolling=bool(ROLLING_PATTERN.search(f"{row.key} {row.value}")),
            )
        )

    for key in sorted(old_by_key.keys() - new_by_key.keys()):
        row = old_by_key[key]
        changes.append(
            models.Change(
                source_id=source_id,
                kind="removed",
                change_id=clock.change_id(source_id, key),
                key=row.key,
                detail=f"Row disappeared. It previously read: {row.value}",
                url=row.url,
                posting_url=row.posting_url,
                rolling=bool(ROLLING_PATTERN.search(f"{key} {row.value}")),
            )
        )

    for key in sorted(old_by_key.keys() & new_by_key.keys()):
        before, after = old_by_key[key].value, new_by_key[key].value
        if before != after:
            changes.append(
                models.Change(
                    source_id=source_id,
                    kind="changed",
                    change_id=clock.change_id(source_id, key),
                    key=new_by_key[key].key,
                    detail=f"Was: {before}\nNow: {after}",
                    url=new_by_key[key].url,
                    posting_url=new_by_key[key].posting_url,
                    closed=is_closed(after),
                    is_discovery_candidate=bool(DISCOVERY_PATTERN.search(f"{key} {after}")),
                    rolling=bool(ROLLING_PATTERN.search(f"{key} {after}")),
                )
            )
    return changes


# --------------------------------------------------------------------------- fetch


# Job boards plant homoglyph canaries to catch scrapers. Jane Street's board carries
# "\uA4DFachine \uA4E1earning \uA4E3esearcher" -- Lisu letters MA, LA and ZHA standing in for M, L
# and R. It renders identically to "Machine Learning Researcher" and is byte-different,
# so an unnormalised key makes it a permanent phantom row whose garbled title is what
# the classifier reads. NFKC plus a small lookalike map costs nothing and closes the
# whole class.
_CONFUSABLE = {
    "\uA4DF": "M", "\uA4E1": "L", "\uA4E3": "R", "\uA4D0": "B", "\uA4D1": "P", "\uA4D2": "D",
    "\uA4D3": "T", "\uA4D4": "G", "\uA4D6": "K", "\uA4D7": "J", "\uA4DA": "C", "\uA4DC": "Z",
    "\uA4DD": "F", "\uA4E0": "N", "\uA4E4": "H", "\uA4E6": "W", "\uA4E7": "S", "\uA4EE": "A",
    "\uA4F0": "E", "\uA4F2": "I", "\uA4F3": "O", "\uA4F4": "U", "\u0391": "A", "\u0392": "B",
    "\u0395": "E", "\u0396": "Z", "\u0397": "H", "\u0399": "I", "\u039A": "K", "\u039C": "M",
    "\u039D": "N", "\u039F": "O", "\u03A1": "P", "\u03A4": "T", "\u03A5": "Y", "\u03A7": "X",
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041A": "K", "\u041C": "M", "\u041D": "H",
    "\u041E": "O", "\u0420": "P", "\u0421": "C", "\u0422": "T", "\u0425": "X", "\u0430": "a",
    "\u0435": "e", "\u043E": "o", "\u0440": "p", "\u0441": "c", "\u0445": "x",
}


def fold(text: str) -> str:
    """Normalise a title so a lookalike codepoint cannot forge a new row."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(_CONFUSABLE.get(ch, ch) for ch in text)
