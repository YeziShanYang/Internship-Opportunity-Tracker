"""The process stage: read `.run/raw/`, parse it, and say what moved.

Pure with respect to the network. It reads two things off disk -- the bodies `gather`
just wrote and the snapshots yesterday's run committed -- which is why `persist` sits at
level 1 below `gather` rather than at the end of the pipeline. A write-only persist
stage would have forbidden both reads.

This is the boundary that makes `run.py gather` then `run.py process` twice a meaningful
test: the second process makes zero requests and must produce the identical answer.
"""
from __future__ import annotations

import json

from core import models
from gather import ats, github_readme, page
from persist import artifacts, store
from process import parse_ats, parse_page, parse_readme

# method -> which assessor reads it. Companion to `gather.collect.CLIENT_FOR`; a method
# in one table and not the other is a source that fetches and is never read, or is read
# and never fetched, and `tests/test_stages.py` asserts they agree.
ASSESSORS = (
    "github_readme", "greenhouse", "lever", "ashby", "workday", "phenom",
    "eightfold", "nsf_awards", "page_text",
    "greenhouse_bulk", "ashby_bulk", "lever_bulk",
)

# Which snapshot extension each method stores. Tier 1 and 2 keep canonical TSV rows;
# Tier 3 keeps normalised page text.
SNAPSHOT_EXT = {"page_text": "txt"}


def _rebuild(method: str, attempt: models.FetchAttempt, body: bytes):
    """Reconstruct the method's fetch object from the stored body plus its index row.

    The decode happens here rather than at the fetch boundary on purpose: the parser is
    what knows the encoding, and forcing a lossy `response.text` in `gather` would make
    a mis-decoded page indistinguishable from a page that changed.
    """
    if method == "page_text":
        return page.PageFetch(attempt=attempt, html=body.decode("utf-8", "replace"))
    if method == "github_readme":
        return github_readme.ReadmeFetch(
            attempt=attempt,
            text=body.decode("utf-8", "replace"),
            branch=attempt.meta.get("branch", ""),
        )
    payload = json.loads(body.decode("utf-8", "replace")) if body else {}
    return ats.AtsFetch(
        attempt=attempt,
        pages=payload.get("pages") or [],
        details=payload.get("details") or {},
        listed=int(attempt.meta.get("listed") or 0),
        planned=int(attempt.meta.get("planned") or 0),
    )


def assess_one(
    source: dict[str, str], attempt: models.FetchAttempt, body: bytes | None
) -> models.SourceResult:
    """Turn one fetch into a SourceResult. Never raises."""
    source_id = source["source_id"]
    method = (source.get("method") or "").strip()

    # The breaker skipped this one. Reported, and reported as its own fact rather than
    # as an ordinary failure, because the counters must not move.
    if attempt.quarantined:
        return models.SourceResult(
            source_id=source_id, ok=False, quarantined=True, error=attempt.error)

    if not attempt.ok:
        # A failed fetch, or a method no module handles. Either way there is no body,
        # and each assessor already renders its own failure, so route through the one
        # that matches rather than inventing a second shape here.
        body = b""

    if method not in ASSESSORS:
        return models.SourceResult(source_id=source_id, ok=False, error=attempt.error)

    fetched = _rebuild(method, attempt, body or b"")
    previous = store.read_snapshot(source_id, ext=SNAPSHOT_EXT.get(method, "tsv"))
    if method == "page_text":
        return parse_page.assess(source, fetched, previous)
    if method == "github_readme":
        return parse_readme.assess(
            source, parse_readme.REPO_CONFIGS.get(source_id), fetched, previous)
    if method in ats.BULK:
        return parse_ats.assess_bulk(source, fetched, previous)
    return parse_ats.assess(source, fetched, previous)


def build(
    sources: list[dict[str, str]], attempts: list[models.FetchAttempt]
) -> list[models.SourceResult]:
    """One SourceResult per attempt, in the order the attempts were made."""
    by_id = {source["source_id"]: source for source in sources}
    results: list[models.SourceResult] = []
    for attempt in attempts:
        source = by_id.get(attempt.source_id)
        if source is None:
            # A body with no row in sources.csv. Nothing else would ever say so.
            results.append(models.SourceResult(
                source_id=attempt.source_id, ok=False,
                error="fetched, but no longer present in sources.csv"))
            continue
        result = assess_one(source, attempt, artifacts.read_body(attempt.source_id))
        if attempt.meta.get("retried"):
            # Refused on the first pass and asked again (gather.collect.retry_refused).
            # Carried so HEALTH can say whether the second try worked.
            result.extra["retried"] = attempt.meta["retried"]
        results.append(result)
    return results


def load_attempts() -> list[models.FetchAttempt]:
    """Read the raw index `gather` wrote."""
    return artifacts.read(artifacts.RAW_INDEX, "raw-index", list[models.FetchAttempt])
