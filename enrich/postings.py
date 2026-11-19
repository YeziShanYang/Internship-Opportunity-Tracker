"""Fetch the real job posting behind a board row, so the classifier has something to judge.

A `simplify-2027` row is a title, a company and a location -- nothing a relevance filter
can act on. Measured on the snapshot: 7 of 592 rows mention any class-year word at all,
and all 7 are incidental matches on "Graduate Researcher". That is why the classifier
used to skip this source entirely: not because the rows were unimportant, but because
there was no information in them to be right or wrong about.

The posting page does carry it. Measured on a random sample of 22 live postings,
7 (~32%) state a class-year gate that excludes a first-year -- "rising junior",
"penultimate year", "third year", an expected-graduation window. Fetching turns a row
that could only ever be guessed at into one that can be decided.

Three findings that shape this module:

* **Prefer Simplify's own page when a row has one.** Simplify rows carry two links:
  the employer's ATS (Workday, iCIMS, Greenhouse, Oracle HCM) and
  `simplify.jobs/p/{uuid}`. The ATS link is often a dead end for a plain fetch --
  Workday returns literally zero characters, because the posting renders entirely in
  JavaScript -- while Simplify's page returns 13K-34K characters including the
  requirements block. A row with *only* an employer link (every zshah-2027 row) is no
  longer skipped: a page that comes back as a shell is re-read in headless Chromium by
  `enrich.browser`, which turned Workday's 0 characters into 7,457 (2026-09-26).
* **The honest User-Agent works.** `paths.USER_AGENT` and a browser string return byte
  identical responses (140,325 bytes on the page tested), so there is no reason to
  pretend to be a browser. `robots.txt` allows `/p/` (`Allow: /`, and `/p` is absent
  from the disallow list) and sets no crawl-delay.
* **A Workday posting is read through Workday's JSON endpoint, not rendered.** Its
  page is a shell, and rendering it costs 2-10 seconds in the browser, one page at a
  time. On 2026-11-19 those renders used up the fetch budget: 10 of 17 unreadable
  postings were skipped for time, 15 of the 17 on Workday. The same posting at
  `/wday/cxs/<tenant>/<site>/job/...` -- the endpoint the board readers in
  `gather.ats` already use -- returned the full description for 10 of 15 of them in
  about half a second each. A refusal from it (403, 404, 429) stays an error, as a
  refusal of the page would; an answer that is not JSON falls back to the page.
* **A short page is a failure, not an empty posting.** The JS-shell case returns HTTP
  200 with no content. Treating that as "posting has no requirements" would silently
  turn a fetch failure into a relevance judgment, which is the one thing this must not
  do -- see `fetch_text`.
"""
from __future__ import annotations

import concurrent.futures
import re
import time

import httpx

from core import paths, text as coretext
from gather import browser
from gather import clients
from persist import cache

# Rows put the ATS link and the Simplify link in the same `Application=` field, ATS
# first. Only the second one is fetchable, so match it specifically rather than taking
# the first URL in the row.
POSTING_URL = re.compile(r"https://simplify\.jobs/p/[0-9a-fA-F-]+")

# Below this, the response is a JavaScript shell rather than a posting. The real pages
# measured 13K-34K characters; the Workday shells measured 0.
MIN_USEFUL_CHARS = 500

# Re-exported so callers have one name for it; it lives in core.text because `process`
# needs it and may not import this layer.
MAX_TEXT_CHARS = coretext.MAX_TEXT_CHARS

# Politeness. robots.txt sets no crawl-delay, so this is self-imposed.
MAX_CONCURRENCY = 6
PER_REQUEST_DELAY_SECONDS = 0.2

# A hung host must never stall the daily run. Whatever has not been fetched when this
# expires is reported as a fetch failure, which degrades to unclassified rather than
# to a relevance decision.
TOTAL_BUDGET_SECONDS = 240.0
# The browser's own budget, started when rendering starts. It used to share the one
# above, so a slow morning of plain fetches left the renders no time at all, and the
# renders themselves -- one page at a time -- ran the shared budget out (2026-11-19).
RENDER_BUDGET_SECONDS = 240.0

# A Workday posting page, which maps onto Workday's JSON endpoint for the same posting.
# The locale segment ("en-US") is optional; the site name comes before "/job/".
_WORKDAY_POSTING = re.compile(
    r"^https://(?P<tenant>[^./]+)\.(?P<pod>wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/?#]+)/(?P<job>job/[^?#]+)")

# A link to one posting rather than to an employer. `core.text.posting_identities` is the
# strict test and is kept strict because deduplication rests on it; this adds the path
# shapes that plainly name one job without carrying an id that test recognises, such as
# Workday's `_JR111596` and `_REQ-48457` requisitions.
_ANY_URL = re.compile(r"https?://[^\s)\]<>]+")
_JOB_PATH = re.compile(r"/(?:job|jobs|posting|postings|careers/job)/[^/?#]+", re.I)
# Never a posting, whatever its path looks like: an aggregator's company page, and the
# aggregator repos themselves.
_NOT_A_POSTING = re.compile(r"simplify\.jobs/c/|github\.com/", re.I)


def posting_link(*haystacks: str) -> str:
    """The first link in `haystacks` that points at one specific posting, or "".

    For an aggregator row that is not Simplify -- zshah-2027 links the employer's own
    Workday, Ashby, Rippling or SmartRecruiters page directly, and until 2026-09-26
    `recover_posting_url` knew only Simplify's pattern, so none of those rows was ever
    fetched and the model judged every one of them on its title.
    """
    for haystack in haystacks:
        for raw in _ANY_URL.findall(haystack or ""):
            url = raw.rstrip(".,;:)]}\"'")
            if _NOT_A_POSTING.search(url):
                continue
            if coretext.posting_identities(url) or _JOB_PATH.search(url):
                return url
    return ""


def recover_posting_url(*haystacks: str) -> str:
    """Best-effort: find a fetchable posting link in already-rendered row text.

    A fallback, not the main path. The producer that built the row knows which of its
    links is the posting page and records it on `Change.posting_url`; this only runs for
    a row recovered from a committed TSV, where the typed roles were never stored
    because the snapshot format is frozen. Returns "" rather than None so callers do
    not need two absent values.
    """
    for haystack in haystacks:
        if not haystack:
            continue
        match = POSTING_URL.search(haystack)
        if match:
            return match.group(0)
    return ""


extract_text = coretext.extract_text


def fetch_text(client: httpx.Client, url: str) -> tuple[str, str]:
    """Return (text, error). Exactly one is non-empty. Never raises.

    An error must stay an error all the way to the digest. If a failed fetch were
    returned as empty text, the classifier would read "no stated requirements" and
    could rule the posting out -- inventing a relevance decision from a network
    problem.
    """
    cached = cache.read_posting(url)
    if cached is not None:
        return cached, ""

    api = workday_api_url(url)
    if api:
        outcome = _fetch_workday_json(client, api)
        if outcome is not None:
            text, error = outcome
            if text:
                cache.write_posting(url, text)
            return text, error

    try:
        response = client.get(url)
        response.raise_for_status()
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"

    text = extract_text(response.text)
    if len(text) < MIN_USEFUL_CHARS:
        return "", (
            f"{SHORT_PAGE}: page returned only {len(text)} characters of text "
            "(renders in JavaScript, or was blocked)"
        )

    cache.write_posting(url, text)
    return text, ""


def workday_api_url(url: str) -> str:
    """The JSON endpoint behind a Workday posting page, or "" for any other URL."""
    match = _WORKDAY_POSTING.match(url or "")
    if not match or match["site"] in ("wday", "job"):
        return ""
    return (f"https://{match['tenant']}.{match['pod']}.myworkdayjobs.com/wday/cxs/"
            f"{match['tenant']}/{match['site']}/{match['job']}")


def _fetch_workday_json(client: httpx.Client, api: str) -> tuple[str, str] | None:
    """(text, error) from Workday's posting JSON, or None to fall back to the page.

    None only when the answer is not the JSON expected, or its description is too short
    to be the real one: the page path (and the browser) may still read it. An HTTP error
    is returned as an error, because the page would refuse the same way.
    """
    try:
        response = client.get(api, headers={"Accept": "application/json"})
        response.raise_for_status()
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"
    try:
        info = response.json().get("jobPostingInfo")
    except (ValueError, AttributeError):
        return None
    if not isinstance(info, dict):
        return None
    facts = [info.get("title") or "", info.get("location") or "",
             " ".join(info.get("additionalLocations") or []), info.get("timeType") or ""]
    text = extract_text(" ".join(bit for bit in facts if bit) + " "
                        + (info.get("jobDescription") or ""))
    return (text, "") if len(text) >= MIN_USEFUL_CHARS else None


# The one error a browser can fix. A 403 or a 404 is not retried there: a site that
# refused this client has said no, and rendering it in Chromium would be the first step
# toward pretending to be someone else.
SHORT_PAGE = "short page"


def _render_shells(results: dict[str, tuple[str, str]], deadline: float) -> None:
    """Re-read every JavaScript shell in `results` through the browser, in place."""
    shells = [u for u, (_, error) in results.items() if error.startswith(SHORT_PAGE)]
    for url, (text, error) in browser.render_many(shells, deadline).items():
        if error:
            results[url] = ("", f"{results[url][1]}; {error}")
        elif len(text) < MIN_USEFUL_CHARS:
            results[url] = ("", f"{results[url][1]}; rendered page had only "
                                f"{len(text)} characters too")
        else:
            cache.write_posting(url, text)
            results[url] = (text, "")


build_client = clients.build_web_client


def fetch_many(
    urls: list[str], client: httpx.Client | None = None, *, render: bool = True
) -> dict[str, tuple[str, str]]:
    """Fetch every URL concurrently. Returns {url: (text, error)}.

    Renamed from `fetch_for_changes`, which took a list of Changes and did the
    URL-derivation itself -- so the fetching layer had to know how a row stores its
    links. It takes URLs now and `enrich.bodies` owns the derivation.
    """
    if not urls:
        return {}

    owned = client is None
    client = client or build_client()
    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    results: dict[str, tuple[str, str]] = {}

    def one(url: str) -> tuple[str, tuple[str, str]]:
        if time.monotonic() > deadline:
            return url, ("", "skipped: the run's total fetch budget was exhausted")
        text, error = fetch_text(client, url)
        time.sleep(PER_REQUEST_DELAY_SECONDS)
        return url, (text, error)

    try:
        with concurrent.futures.ThreadPoolExecutor(MAX_CONCURRENCY) as pool:
            for url, outcome in pool.map(one, urls):
                results[url] = outcome
    finally:
        if owned:
            client.close()
    if render:
        _render_shells(results, time.monotonic() + RENDER_BUDGET_SECONDS)
    return results
