"""The gather stage: fetch every watched source and write the bodies to `.run/raw/`.

This is one of only three packages allowed to make a network request, and the only one
that makes ~157 of them. Nothing here parses, filters or diffs; the bodies land on disk
exactly as fetched and `process` decides what they mean.

Two clients, not one, and that is a credential boundary rather than a style choice --
see `gather.clients`.

Three things that are *not* failures and must each stay distinguishable from one, and
from each other, all the way to HEALTH (spec 10.1):

* `method=manual` -- deliberately not fetched, no attempt recorded at all. These reach
  the owner as calendar reminders.
* quarantined -- the breaker skipped the fetch, so nothing was learned and no counter
  may move. Still reported, because "we have stopped looking" is the strongest form of
  blind-not-quiet.
* an unrecognised method -- a configuration error. This used to be a bare `continue`,
  which produced no result at all: no health line, no failure count, and a silently
  unwatched source indistinguishable from a quiet one.
"""
from __future__ import annotations

import dataclasses
import json
import time
from typing import Callable

from core import models, paths
from gather import ats, breaker, clients, github_readme, page
from persist import artifacts

GITHUB_CLIENT = "github"
WEB_CLIENT = "web"

# method -> which client it needs. The companion table in `process.build` maps the same
# methods to their assessors, and `tests/test_stages.py` asserts the two agree -- a
# method in one and not the other is a source that fetches and is never read, or is
# read and never fetched.
CLIENT_FOR = {
    "github_readme": GITHUB_CLIENT,
    "greenhouse": WEB_CLIENT,
    "lever": WEB_CLIENT,
    "ashby": WEB_CLIENT,
    "workday": WEB_CLIENT,
    "phenom": WEB_CLIENT,
    "eightfold": WEB_CLIENT,
    "nsf_awards": WEB_CLIENT,
    "greenhouse_bulk": WEB_CLIENT,
    "ashby_bulk": WEB_CLIENT,
    "lever_bulk": WEB_CLIENT,
    "page_text": WEB_CLIENT,
}

UNWATCHED = paths.UNWATCHED_METHOD


def _encode_body(method: str, fetched) -> bytes:
    """The bytes to store for this method's fetch.

    JSON for the ATS feeds, because their "one fetch" is a list of documents plus a map
    of Workday detail documents, and a faithful replay needs both. Text otherwise.
    """
    if method in ats.ENDPOINTS or method in ats.BULK:
        return json.dumps(
            {"pages": fetched.pages, "details": fetched.details},
            ensure_ascii=False, default=str,
        ).encode("utf-8")
    text = getattr(fetched, "html", None)
    if text is None:
        text = getattr(fetched, "text", "")
    return text.encode("utf-8")


def fetch_one(
    source: dict[str, str],
    by_name: dict,
    wants_detail: Callable[[dict], bool],
) -> tuple[models.FetchAttempt, bytes]:
    """Fetch one source and return (attempt, body). Never raises."""
    source_id = source["source_id"]
    method = (source.get("method") or "").strip()
    client = by_name[CLIENT_FOR[method]]

    if method == "page_text":
        fetched = page.fetch(source, client)
    elif method == "github_readme":
        fetched = github_readme.fetch(source, client)
    elif method in ats.BULK:
        fetched = ats.fetch_bulk(source, client)
    else:
        fetched = ats.fetch(source, client, wants_detail=wants_detail)

    attempt = fetched.attempt
    if attempt.source_id != source_id:
        attempt = models.FetchAttempt(**{**vars(attempt), "source_id": source_id})
    return attempt, (_encode_body(method, fetched) if attempt.ok else b"")


def collect(
    sources: list[dict[str, str]],
    only: str | None,
    *,
    wants_detail: Callable[[dict], bool],
) -> list[models.FetchAttempt]:
    """Fetch every watched source, writing each body to `.run/raw/{source_id}.body`.

    Returns one FetchAttempt per source that was considered -- including the quarantined
    ones and the misconfigured ones, because a source that produced no record is a
    source nobody will notice has gone blind.

    `wants_detail` decides which Workday listings are worth a second request for their
    description. It is a parameter rather than an import because deciding what to keep
    belongs to `process`, and a stage may only import at or below its own level -- a
    lazy function-local import would have satisfied the letter of that and not the
    point of it.
    """
    attempts: list[models.FetchAttempt] = []
    with clients.build_github_client(clients.github_token()) as github, \
            clients.build_web_client() as web:
        by_name = {GITHUB_CLIENT: github, WEB_CLIENT: web}
        for source in sources:
            source_id = source["source_id"]
            if only and source_id != only:
                continue
            method = (source.get("method") or "").strip()
            if method == UNWATCHED:
                continue

            if method not in CLIENT_FOR:
                attempts.append(models.FetchAttempt(
                    source_id=source_id, ok=False,
                    error=f"sources.csv sets method={method!r}, which no module handles"))
                continue

            # `--only` is an explicit instruction to look at this source now, so it
            # bypasses the breaker; that is the affordance you want when you are
            # debugging the source that is quarantined.
            if not only:
                skip, why = breaker.quarantine_state(source)
                if skip:
                    attempts.append(models.FetchAttempt(
                        source_id=source_id, ok=False, quarantined=True, error=why))
                    continue

            attempt, body = fetch_one(source, by_name, wants_detail)
            if attempt.ok:
                artifacts.write_body(source_id, body)
            attempts.append(attempt)
            time.sleep(paths.REQUEST_DELAY_SECONDS)  # spec section 11: be a good citizen
        retry_refused(sources, attempts, by_name, wants_detail)
    artifacts.write(artifacts.RAW_INDEX, "raw-index", attempts)
    return attempts


# A server that answers 429 is saying "come back later", so a source refused that way
# is asked once more at the end of the run instead of being reported failed on the
# spot. On 2026-11-14 and 2026-11-16 every Workday board failed in one run (bodies
# that were not JSON, then HTTP 429), across four Workday clusters and from the first
# board on, and all of them read normally on 2026-11-15. That is the runner's address
# being throttled, not this run's own volume, so slowing the crawl would not help.
RETRY_STATUSES = (429, 503)
RETRY_PAUSE_SECONDS = 60
# Two refusals in a row on the second pass means the throttle has not lifted. The rest
# keep their first failure rather than each being asked again for nothing.
RETRY_GIVE_UP_AFTER = 2


def retry_reason(source: dict[str, str], attempt: models.FetchAttempt) -> str:
    """Why a failed fetch is worth asking again later in the run, or "" if it is not.

    A 403 or 404 is never retried: a refusal is an answer. A JSON API that returned
    something other than JSON is treated as throttled, because that is what Workday's
    boards did on 2026-11-14, the morning before the same boards returned 429.
    """
    if attempt.ok or attempt.quarantined:
        return ""
    if attempt.status in RETRY_STATUSES:
        return f"HTTP {attempt.status}"
    method = (source.get("method") or "").strip()
    if method in ats.ENDPOINTS and attempt.error.startswith("JSONDecodeError"):
        return "a response that was not JSON"
    return ""


def retry_refused(
    sources: list[dict[str, str]],
    attempts: list[models.FetchAttempt],
    by_name: dict,
    wants_detail: Callable[[dict], bool],
) -> None:
    """Ask each throttled source once more, after a pause, replacing its attempt.

    The attempt that replaces it carries `meta["retried"]` with the first failure's
    reason, whatever the second outcome, so HEALTH can say the retry happened.
    """
    by_id = {source["source_id"]: source for source in sources}
    pending = [(index, reason) for index, attempt in enumerate(attempts)
               if (reason := retry_reason(by_id.get(attempt.source_id, {}), attempt))]
    if not pending:
        return
    time.sleep(RETRY_PAUSE_SECONDS)
    refused_running = 0
    for index, reason in pending:
        source = by_id[attempts[index].source_id]
        attempt, body = fetch_one(source, by_name, wants_detail)
        if attempt.ok:
            artifacts.write_body(source["source_id"], body)
            refused_running = 0
        elif retry_reason(source, attempt):
            refused_running += 1
        attempts[index] = dataclasses.replace(
            attempt, meta={**attempt.meta, "retried": reason})
        time.sleep(paths.REQUEST_DELAY_SECONDS)
        if refused_running >= RETRY_GIVE_UP_AFTER:
            break
