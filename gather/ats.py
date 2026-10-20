"""Fetch an applicant-tracking feed. Transport only: no parsing, no screening.

Phase 1 could only see an opportunity once a volunteer had typed it into a markdown
table. This sees it the moment the firm creates it, days to weeks earlier, and with the
full description attached.

Six vendors, and they divide by *transport shape* rather than by popularity:

* **One request, one document.** Greenhouse, Lever and Ashby answer the whole board in
  a single GET. Greenhouse carries almost everything -- janestreet 230 jobs, imc 170,
  jumptrading 106, virtu 50, akunacapital 38, oldmissioncapital 38, transmarketgroup 19,
  fiveringsllc 16, aquatic 8, sevenresearch 8, and fingerprint mining the snapshots
  found drweng 157, worldquant 103 and schonfeld 71 on top.
* **Paged, with a total to stop at.** Workday, Phenom and Eightfold. Each reports how
  many postings exist, and each is refused outright rather than half-read if the pages
  do not add up -- see `_refuse_partial`.
* **Workday alone needs a second request per posting**, because its list response has
  no description and no employment type. Which postings are worth paying for is a
  filtering decision, and filtering belongs to `process`, so the predicate arrives as
  the `wants_detail` argument rather than being made here. That is what keeps this
  module from importing the layer above it.

Workday is also why "the firm has no public API" was wrong for so many employers. A
plain fetch of a Workday posting returns zero characters because the page renders
entirely in JavaScript, so it looked unreachable; the CXS endpoint behind it returns
clean JSON, and 84 of the 96 tenant/site pairs already linked from our own snapshots
answer it.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from core import models, paths
from persist import store

GREENHOUSE, LEVER, ASHBY, WORKDAY, PHENOM, EIGHTFOLD, NSF = (
    "greenhouse", "lever", "ashby", "workday", "phenom", "eightfold", "nsf_awards",
)

ENDPOINTS = {
    GREENHOUSE: "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    LEVER: "https://api.lever.co/v0/postings/{slug}?mode=json",
    ASHBY: "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    # Workday's slug is the whole CXS base URL, because the tenant, the site and the
    # wd1/wd3/wd5 shard all vary independently and guessing any of them is guesswork.
    WORKDAY: "{slug}/jobs",
    # Phenom, like Workday, takes the whole endpoint URL as its slug.
    PHENOM: "{slug}",
    # Eightfold likewise: the tenant host varies per employer
    # (campusjobs.mlp.com, mlp.eightfold.ai, career.mlp.com all front the same board).
    EIGHTFOLD: "{slug}",
    # NSF's public Award Search API; the slug is the keyword phrase. Not a job board,
    # but the same shape: a paged list of things that appear and disappear. A new REU
    # Site award is a new summer research programme.
    NSF: "https://api.nsf.gov/services/v1/awards.json",
}

# Bulk sources: one sources.csv row standing for a list of small employers' boards on
# the same vendor, so several hundred boards do not each need a row, a snapshot file and
# a HEALTH line. Greenhouse is read without `content=true`: descriptions are fetched by
# `enrich` for the few rows that change, rather than for every posting every day.
BULK = {
    "greenhouse_bulk": (GREENHOUSE, "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"),
    "ashby_bulk": (ASHBY, ENDPOINTS[ASHBY]),
    "lever_bulk": (LEVER, ENDPOINTS[LEVER]),
}
# Lever's robots.txt asks for a one-second crawl delay; the others publish none.
BULK_DELAY_SECONDS = {GREENHOUSE: 0.25, ASHBY: 0.25, LEVER: 1.0}

SINGLE_SHOT = (GREENHOUSE, LEVER, ASHBY)
PAGED = (WORKDAY, PHENOM, EIGHTFOLD, NSF)

# Workday rejects a limit above 20 (50 and 100 return an empty list), and it keeps
# serving rows past the end rather than stopping -- a naive "fetch until empty" loop
# pulled 412 rows from a 152-job board, cycling the same titles. Always stop at `total`.
WORKDAY_PAGE = 20
WORKDAY_MAX_PAGES = 40

# Descriptions are one extra request each, so they are fetched only for postings that
# already passed the plan. Capital Group's board is 152 jobs and 1 survivor. The cap
# must not bind silently -- several boards hit an earlier value of 25 exactly, which is
# the signature of a truncation rather than a coincidence -- so overflow refuses the
# board instead.
WORKDAY_MAX_DETAILS = 80

# Phenom pages 100 at a time and reports `totalCount`. Measured on Susquehanna: 263
# postings over three pages.
PHENOM_PAGE = 100
PHENOM_MAX_PAGES = 30

# Eightfold ignores a larger `num` and serves ten at a time whatever you ask for, so
# the page size is descriptive rather than a request. 40 pages is 400 postings, well
# clear of Millennium's 59.
EIGHTFOLD_PAGE = 10
EIGHTFOLD_MAX_PAGES = 40


# NSF serves at most 25 awards a page, and its paging is NOT stable: four passes over
# the same 447 REU awards on 2026-10-01 each repeated 6-11 awards in place of others,
# and only 425 appeared in all four, which would report phantom removals every morning.
# A single page is deterministic, so the query is split by award start date until each
# slice fits on one page; a single day that still overflows is paged repeatedly until
# the union reaches the stated total. `offset` is zero-based: `offset=1` silently drops
# each page's first award.
NSF_PAGE = 25
NSF_MAX_PASSES = 8
NSF_MAX_REQUESTS = 250
NSF_EARLIEST_START = datetime.date(2010, 1, 1)
NSF_FIELDS = (
    "id,title,awardeeName,awardeeCity,awardeeStateCode,expDate,startDate,"
    "dirAbbr,divAbbr,fundProgramName,abstractText"
)


class MalformedPayload(RuntimeError):
    """HTTP 200, but the response shape says we cannot trust what it contains.

    This is the JSON counterpart of the page text/HTML ratio floor, and it exists
    because the obvious reading of a broken payload is the dangerous one. Greenhouse
    answers a rate-limited request with `{"error": "...", "jobs": []}`, and
    `payload.get("jobs", [])` turns that into "this employer has no openings".

    The row-count guard already refuses a board that drops to zero from a non-zero
    snapshot, which catches this on a busy board. It cannot catch it on a quiet one: 13
    of the watched boards legitimately sit at zero student postings, and for those an
    error wearing an empty board's clothes is indistinguishable from an ordinary
    morning. So the shape is checked directly rather than inferred from the row count.

    Borrowed from zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships,
    whose `models.clean_listing` makes the same argument: return None, never [], because
    reading an error as an empty board closes every role the employer has.
    """


@dataclass
class AtsFetch:
    """Everything one board fetch returned, unparsed.

    `pages` holds the list documents in request order; `details` maps a Workday
    `externalPath` to its detail document.

    `listed` is how many rows the board actually had, for the paged vendors that report
    a total. It matters because for those methods a screen runs before `process` ever
    sees a Posting -- Workday's detail plan runs here, and Phenom's recruiting-category
    screen runs in the parser -- so without the original count those rows vanish with
    nothing to report. That is the silent filter this project has been bitten by twice,
    and it reappeared once while this module was being split: Phenom's screen briefly
    reported a 2-posting board as having 1 posting, which the suite caught.

    `planned` is how many Workday descriptions were worth a second request. It is here
    for the raw index rather than for the filter report, which `process` derives from
    `listed` minus the postings it produced -- one number covering both screens.
    """

    attempt: models.FetchAttempt
    pages: list[Any] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    listed: int = 0
    planned: int = 0


def _listing(payload, key: str) -> list[dict]:
    """The list at `payload[key]`, or raise rather than return a misleading empty.

    A missing key counts as malformed: every ATS here includes the collection key even
    when it is empty, so its absence means the shape changed under us.
    """
    if not isinstance(payload, dict):
        raise MalformedPayload(f"expected a JSON object, got {type(payload).__name__}")
    error = payload.get("error") or payload.get("errors")
    if error:
        raise MalformedPayload(f"payload carries an error: {str(error)[:200]!r}")
    items = payload.get(key)
    if items is None:
        raise MalformedPayload(f"no {key!r} key in the payload")
    if not isinstance(items, list):
        raise MalformedPayload(f"{key!r} is a {type(items).__name__}, not a list")
    if any(not isinstance(item, dict) for item in items):
        raise MalformedPayload(f"{key!r} contains a non-object member")
    return items


def _listing_root(payload) -> list[dict]:
    """Same contract for an API whose whole response is the list. Lever does this."""
    if not isinstance(payload, list):
        raise MalformedPayload(f"expected a JSON array, got {type(payload).__name__}")
    if any(not isinstance(item, dict) for item in payload):
        raise MalformedPayload("array contains a non-object member")
    return payload


def _page_items(payload, key: str) -> list[dict]:
    """One page of a paginated feed.

    Looser than `_listing` in exactly one way: a missing collection key is read as
    "past the last page" rather than as malformed, because the pagination loops stop on
    an empty batch. An explicit error key is still a failure.
    """
    if not isinstance(payload, dict):
        raise MalformedPayload(f"expected a JSON object, got {type(payload).__name__}")
    error = payload.get("error") or payload.get("errors")
    if error:
        raise MalformedPayload(f"payload carries an error: {str(error)[:200]!r}")
    items = payload.get(key) or []
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        raise MalformedPayload(f"{key!r} is not a list of objects")
    return items


def _refuse_partial(vendor: str, total: int, got: int) -> None:
    """Half a board that looks healthy is worse than a board that reports itself broken.

    Workday's searchText does not narrow usefully (NVIDIA: 2,000 jobs, 1,004 "matches"
    for "intern"), so when the pages do not add up to the total the honest move is to
    refuse the board rather than half-read it.
    """
    if total and got < total:
        raise RuntimeError(
            f"{vendor} reports {total} postings but only {got} could be paged; this "
            "source would be silently partial, so it is reported as failing instead"
        )


def _page_workday(client: httpx.Client, base: str) -> tuple[list[Any], list[dict], int, int]:
    """One pass over a Workday board's list pages: (pages, listed, total, requests).

    Workday states `total` on the first page only, so a posting that comes down while
    the later pages are being read leaves the pass one short of a total that is already
    stale. Vanguard did exactly this on 2026-10-20 (346 stated, 345 paged); a second
    pass a minute later read 350 of 350.
    """
    listed: list[dict] = []
    pages: list[Any] = []
    total = None
    requests = 0
    for page in range(WORKDAY_MAX_PAGES):
        offset = page * WORKDAY_PAGE
        if total is not None and offset >= total:
            break
        response = client.post(
            f"{base}/jobs",
            json={"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": offset,
                  "searchText": ""},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        requests += 1
        response.raise_for_status()
        payload = response.json()
        pages.append(payload)
        if total is None:
            total = payload.get("total") or 0
        batch = _page_items(payload, "jobPostings")
        if not batch:
            break
        listed += batch
    return pages, listed, total or 0, requests


def _fetch_workday(
    client: httpx.Client, base: str, wants_detail: Callable[[dict], bool]
) -> AtsFetch:
    # A short pass is re-read once from the top, because the board moving under the
    # crawl is the ordinary cause. A board that is short twice running is refused.
    pages, listed, total, requests = _page_workday(client, base)
    if total and len(listed) < total:
        pages, listed, total, more = _page_workday(client, base)
        requests += more

    _refuse_partial("board", total, len(listed))

    # The plan: which postings are worth a second request. Supplied by `process`, and
    # applied here because the whole point is not paying for descriptions that are
    # going to be discarded -- RBC's board is 37 student titles of which 34 are
    # Canadian, and paying for 37 to keep 3 is what pushed it past WORKDAY_MAX_DETAILS.
    planned = [job for job in listed if wants_detail(job)]
    if len(planned) > WORKDAY_MAX_DETAILS:
        raise RuntimeError(
            f"{len(planned)} postings passed the title filter but only "
            f"{WORKDAY_MAX_DETAILS} descriptions are fetched per run; raise "
            "WORKDAY_MAX_DETAILS rather than letting this board go partial"
        )

    details: dict[str, Any] = {}
    for job in planned:
        path = job.get("externalPath") or ""
        if not path:
            continue
        try:
            detail = client.get(f"{base}{path}")
            requests += 1
            if detail.status_code < 300:
                details[path] = detail.json()
        except Exception:
            pass  # a missing description is not a failed board
    return AtsFetch(
        attempt=models.FetchAttempt(source_id="", ok=True, status=200, requests=requests),
        pages=pages, details=details, listed=len(listed), planned=len(planned),
    )


def _fetch_phenom(client: httpx.Client, endpoint: str) -> AtsFetch:
    """Read a Phenom-hosted career site's JSON API.

    Phenom is why "this firm blocks automation" needed re-testing. Susquehanna's careers
    page is the canonical JavaScript shell in this codebase -- 221 characters of text out
    of 402KB of HTML, the very measurement the ratio floor was calibrated against -- so
    the firm sat at method=manual as unreachable. The JSON behind it answers our honest
    User-Agent with 263 postings, descriptions and recruiting categories included.
    Nothing was blocked; only the rendering was.

    Unlike Workday there is no second request per posting: the description and the
    qualifications both arrive in the list payload.
    """
    pages: list[Any] = []
    listed = 0
    total = None
    requests = 0
    for page in range(1, PHENOM_MAX_PAGES + 1):
        response = client.get(
            endpoint, params={"page": page, "limit": PHENOM_PAGE},
            headers={"Accept": "application/json"},
        )
        requests += 1
        response.raise_for_status()
        payload = response.json()
        pages.append(payload)
        if total is None:
            total = payload.get("totalCount") or 0
        batch = _page_items(payload, "jobs")
        if not batch:
            break
        listed += len(batch)
        if listed >= total:
            break
    _refuse_partial("site", total or 0, listed)
    return AtsFetch(
        attempt=models.FetchAttempt(source_id="", ok=True, status=200, requests=requests),
        pages=pages, listed=listed, planned=listed,
    )


def _fetch_eightfold(client: httpx.Client, endpoint: str) -> AtsFetch:
    """Read an Eightfold-hosted board.

    Millennium is why this exists, and it was the largest single hole the 2026-09-12
    source audit found: `millennium-students` watched a careers page whose note said
    "Millennium is on no public ATS", while `campusjobs.mlp.com` answers our honest
    User-Agent with 59 campus postings.

    Descriptions are absent from the list payload -- `job_description` is present and
    empty -- so postings come back with no text and `enrich` fetches the body for any
    row that actually changes. That is the cheaper shape anyway: 59 detail fetches a day
    to read two of them is what WORKDAY_MAX_DETAILS exists to prevent.
    """
    pages: list[Any] = []
    listed = 0
    total = None
    requests = 0
    for page in range(EIGHTFOLD_MAX_PAGES):
        response = client.get(
            endpoint, params={"start": page * EIGHTFOLD_PAGE, "num": EIGHTFOLD_PAGE},
            headers={"Accept": "application/json"},
        )
        requests += 1
        response.raise_for_status()
        payload = response.json()
        pages.append(payload)
        if total is None:
            total = payload.get("count") or 0
        batch = _page_items(payload, "positions")
        if not batch:
            break
        listed += len(batch)
        if listed >= total:
            break
    _refuse_partial("board", total or 0, listed)
    return AtsFetch(
        attempt=models.FetchAttempt(source_id="", ok=True, status=200, requests=requests),
        pages=pages, listed=listed, planned=listed,
    )


def nsf_window_start(today: datetime.date) -> str:
    """Awards still funded on 1 August of the coming summer, as NSF's mm/dd/yyyy.

    An award that ends before then cannot fund a site that summer, so including it
    would announce programmes that are not running.
    """
    year = today.year + 1 if today.month >= 8 else today.year
    return f"08/01/{year}"


def _fetch_nsf(client: httpx.Client, endpoint: str, keyword: str) -> AtsFetch:
    """Every award matching an exact keyword phrase, complete and in a stable set.

    `api.nsf.gov` publishes no robots.txt, while `www.nsf.gov/awardsearch/*` is
    disallowed, so this reads the API only. The abstract travels with each award and
    becomes the posting text, which is what stops `enrich` ever fetching an award page.
    """
    fmt = "%m/%d/%Y"
    today = datetime.date.today()
    window = nsf_window_start(today)
    pages: list[Any] = []
    requests = 0

    def page(first: datetime.date, last: datetime.date, offset: int = 0) -> dict:
        nonlocal requests
        if requests >= NSF_MAX_REQUESTS:
            raise MalformedPayload(f"NSF needed more than {NSF_MAX_REQUESTS} requests")
        response = client.get(endpoint, params={
            "keyword": f'"{keyword}"', "expDateStart": window,
            "startDateStart": first.strftime(fmt), "startDateEnd": last.strftime(fmt),
            "printFields": NSF_FIELDS, "rpp": NSF_PAGE, "offset": offset,
        }, headers={"Accept": "application/json"})
        requests += 1
        response.raise_for_status()
        payload = response.json()
        root = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(root, dict) or root.get("serviceNotification"):
            raise MalformedPayload(f"NSF returned no award list: {str(payload)[:200]!r}")
        if not isinstance(root.get("award", []), list):
            raise MalformedPayload("NSF 'award' is not a list")
        return payload

    def total(payload: dict) -> int:
        return int((payload["response"].get("metadata") or {}).get("totalCount") or 0)

    def exhaust(day: datetime.date, expected: int) -> None:
        # More than a page of awards starts on one day (1 September above all), so
        # this slice has to page, and paging is unstable. Each pass misses a few, but
        # their union converges on the full set; stop when it matches the stated total.
        seen: dict[str, dict] = {}
        for _ in range(NSF_MAX_PASSES):
            for offset in range(0, expected, NSF_PAGE):
                for award in page(day, day, offset)["response"].get("award", []):
                    seen[str(award.get("id"))] = award
            if len(seen) >= expected:
                pages.append({"response": {"award": list(seen.values())}})
                return
        raise MalformedPayload(
            f"NSF paging for {day:%Y-%m-%d} found {len(seen)} of {expected} awards "
            f"after {NSF_MAX_PASSES} passes")

    def walk(first: datetime.date, last: datetime.date) -> None:
        payload = page(first, last)
        if total(payload) <= NSF_PAGE:
            pages.append(payload)
            return
        if first >= last:
            exhaust(first, total(payload))
            return
        middle = first + (last - first) // 2
        walk(first, middle)
        walk(middle + datetime.timedelta(days=1), last)

    walk(NSF_EARLIEST_START, datetime.date(today.year + 2, 12, 31))
    listed = sum(len(p["response"].get("award", [])) for p in pages)
    return AtsFetch(
        attempt=models.FetchAttempt(source_id="", ok=True, status=200, requests=requests),
        pages=pages, listed=listed, planned=listed,
    )


def fetch_bulk(source: dict[str, str], client: httpx.Client) -> AtsFetch:
    """Fetch every board in a bulk source's slug list. Never raises.

    One board failing is recorded on its page and is not a failure of the source:
    `process` carries that board's rows forward from yesterday, so a board that could
    not be read never reports its postings as removed. The source as a whole fails
    only if its list is missing or empty.
    """
    source_id = source["source_id"]
    method = (source.get("method") or "").strip()
    vendor, endpoint = BULK[method]
    started = time.monotonic()
    slugs = store.read_board_list((source.get("url") or "").strip())
    if not slugs:
        return AtsFetch(attempt=models.FetchAttempt(
            source_id=source_id, ok=False, requested_url=source.get("url", ""),
            error=f"board list {source.get('url')!r} is missing or empty"))
    pages: list[Any] = []
    for slug in slugs:
        try:
            response = client.get(endpoint.format(slug=slug))
            response.raise_for_status()
            pages.append({"slug": slug, "payload": response.json()})
        except Exception as exc:
            pages.append({"slug": slug, "error": f"{type(exc).__name__}: {exc}"[:200]})
        time.sleep(BULK_DELAY_SECONDS[vendor])
    body = json.dumps(pages, sort_keys=True, default=str)
    return AtsFetch(
        attempt=models.FetchAttempt(
            source_id=source_id, ok=True, status=200,
            requested_url=f"{len(slugs)} {vendor} boards from {source.get('url')}",
            size=len(body),
            sha256=hashlib.sha256(body.encode("utf-8", "replace")).hexdigest(),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            requests=len(slugs),
            meta={"listed": str(len(slugs)), "planned": str(len(slugs))}),
        pages=pages, listed=len(slugs), planned=len(slugs))


def fetch(
    source: dict[str, str],
    client: httpx.Client,
    *,
    wants_detail: Callable[[dict], bool] | None = None,
) -> AtsFetch:
    """Fetch one board. Never raises: a failure is a result, not an exception.

    `wants_detail` is Workday's plan predicate and is required for that method only.
    """
    source_id = source["source_id"]
    method = (source.get("method") or "").strip()
    slug = (source.get("url") or "").strip()
    url = ENDPOINTS[method].format(slug=slug) if method in ENDPOINTS and slug else slug
    started = time.monotonic()

    def failed(error: str, **kwargs) -> AtsFetch:
        return AtsFetch(attempt=models.FetchAttempt(
            source_id=source_id, ok=False, error=error, requested_url=url,
            elapsed_ms=int((time.monotonic() - started) * 1000), **kwargs))

    if method not in ENDPOINTS or not slug:
        return failed(f"no fetcher for method={method!r} or empty slug")
    if method == WORKDAY and wants_detail is None:
        # A programming error, not a data error, and it would silently fetch zero
        # descriptions and report a board of titles with no requirements.
        return failed("workday needs a wants_detail plan and was not given one")

    try:
        if method == WORKDAY:
            result = _fetch_workday(client, slug.rstrip("/"), wants_detail)
        elif method == PHENOM:
            result = _fetch_phenom(client, slug.rstrip("/"))
        elif method == EIGHTFOLD:
            result = _fetch_eightfold(client, slug.rstrip("/"))
        elif method == NSF:
            result = _fetch_nsf(client, ENDPOINTS[NSF], slug)
        else:
            response = client.get(url)
            response.raise_for_status()
            payload = response.json()
            result = AtsFetch(
                attempt=models.FetchAttempt(
                    source_id=source_id, ok=True, status=response.status_code,
                    final_url=str(response.url)),
                pages=[payload])
    except Exception as exc:
        return failed(f"{type(exc).__name__}: {exc}")

    # The attempt is finished off here rather than in each fetcher, so the identity and
    # the timing are recorded in exactly one place.
    body = json.dumps(result.pages, sort_keys=True, default=str)
    result.attempt = models.FetchAttempt(
        source_id=source_id,
        ok=True,
        status=result.attempt.status or 200,
        requested_url=url,
        final_url=result.attempt.final_url or url,
        size=len(body),
        sha256=hashlib.sha256(body.encode("utf-8", "replace")).hexdigest(),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        requests=result.attempt.requests or 1,
        # How many rows the board said it had. Not derivable from the payloads once
        # parsing has screened them, and losing it is how a filter goes silent.
        meta={"listed": str(result.listed), "planned": str(result.planned)},
    )
    return result
