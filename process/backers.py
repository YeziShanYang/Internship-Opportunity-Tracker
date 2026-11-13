"""Which investors back the startup behind a change. Pure.

The VC portfolio boards are the vetted route to startups: a company is on Khosla's board
because Khosla invested. The weekly discovery pass records, in data/backers.csv, which
investors' boards link to which employer ATS board. This stage puts that on every change
whose own link names one of those boards -- whichever source found it, so a Simplify row
for a Khosla company is marked as surely as a row from Khosla's board -- and on every
change that came from a VC board directly.

It is a fact about the posting, not a judgment: the digest prints it as "Backed by", and
the classifier is told it. An empty value means the backing is not known, which for a
startup outside the watched portfolios is the normal case, and is never read as
"unbacked".
"""
from __future__ import annotations

import dataclasses

from core import models
from core import text as coretext

# The suffix the watched VC portfolio boards share in sources.csv.
VC_BOARD_SUFFIX = "-portfolio-interns"
SINGLE_BOARD_METHODS = ("greenhouse", "ashby", "lever")


def investor_name(source: dict[str, str]) -> str:
    """'Khosla Ventures portfolio internships (early-stage US)' -> 'Khosla Ventures'."""
    name = (source.get("program_names") or "").split(" portfolio")[0].strip()
    return name or (source.get("source_id") or "").removesuffix(VC_BOARD_SUFFIX)


def annotate(
    changes: list[models.Change],
    backer_rows: list[dict[str, str]],
    sources: list[dict[str, str]],
) -> list[models.Change]:
    by_board = {
        row["board"]: {b.strip() for b in (row.get("backers") or "").split(";") if b.strip()}
        for row in backer_rows
    }
    by_source = {source["source_id"]: source for source in sources}
    annotated = []
    for change in changes:
        investors: set[str] = set()
        source = by_source.get(change.source_id, {})
        method = (source.get("method") or "").strip()
        if change.source_id.endswith(VC_BOARD_SUFFIX) and method == "page_text":
            investors.add(investor_name(source))
        boards = [f"{v}:{s.lower()}" for v, s in coretext.ats_boards(
            change.posting_url, change.url)]
        if method in SINGLE_BOARD_METHODS:
            boards.append(f"{method}:{(source.get('url') or '').strip().lower()}")
        for board in boards:
            investors |= by_board.get(board, set())
        annotated.append(
            dataclasses.replace(change, backers="; ".join(sorted(investors)))
            if investors else change
        )
    return annotated
