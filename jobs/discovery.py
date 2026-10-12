"""Weekly pass for sources the watchlist does not have yet. A job, not a stage.

It does GitHub search (gather), snapshot mining (a read), `record()` (persist) and
`lines()` (render), so it cannot sit at any single level of the stage graph -- which is
why it lives here beside the daily digest rather than pretending to be one layer.


Every other part of this tool watches a fixed list. That list was assembled by hand
once, and it goes stale the moment a new repo appears or a firm moves onto an ATS.
This looks for what is missing, once a week.

It **proposes and never adds**. Adding a source silently changes what the tool watches
forever, and a bad automatic add poisons the digest in a way that is hard to notice --
the same reasoning that keeps the classifier away from the hand-verified `eligible`
column. Candidates land in `data/discovered.csv` and in the digest; accepting one is a
deliberate act through the `/add-opportunity` skill, which already probes robots.txt
and picks the right watchlist.

Three signals, all free:

1. **GitHub search.** `GH_PAT` is already configured and authenticated search allows 30
   requests a minute, so a handful of queries once a week costs nothing.
2. **ATS fingerprint mining of the snapshots we already store.** Zero network, and the
   highest-yield of the three: `drweng`, `schonfeld`, `dvtrading` and
   `belvederetrading` were all sitting in `data/snapshots/*.tsv` as links, unnoticed,
   while the firms were assumed to have no public board. Each mined slug is verified
   with one live call so a dead one is never proposed.
3. **Slug guessing, but only for firms newly added to a tracked repo.** Rotating
   through every known firm weekly would cost hundreds of requests for a low hit rate;
   the Tier 1 diff already reports when a firm appears for the first time, which is
   exactly when guessing is worth doing.

The digest cap is one issue per date and it is absolute, so this cannot open its own
issue. It rides along in Monday's digest as an extra section.
"""
from __future__ import annotations

import datetime
import html as html_module
import re
import time
import urllib.parse
from dataclasses import dataclass, replace

import httpx

import classify
from core import clock, paths, profile
from persist import store
from gather import ats, github_readme
from process import parse_ats, parse_readme

GITHUB_SEARCH = "https://api.github.com/search/repositories"

SEARCH_QUERIES = (
    "underclassmen internships in:name,description",
    "freshman sophomore internship in:name,description",
    "quant internships in:name,description",
    # This season's and next season's lists, so a new year's trackers are proposed as
    # soon as they are created rather than after someone notices the old ones went quiet.
    f"Summer {clock.recruiting_cycle()} Internships in:name",
    f"Summer {clock.recruiting_cycle() + 1} Internships in:name",
    "quantitative finance internship list in:name,description",
)

MIN_STARS = 25
PUSHED_WITHIN_DAYS = 120
MAX_SLUG_VERIFICATIONS = 20
BUDGET_SECONDS = 180.0

# Where a board slug hides inside a link we already store, or inside a careers page.
#
# Every pattern is anchored on the vendor's DOMAIN, never on a bare keyword. That is
# not stylistic. CLAUDE.md used to advise grepping the HTML for `lever`, `workday`,
# `greenhouse` and friends, and measured across the 65 watched pages that produced ten
# false positives out of twenty-four hits -- nine pages matched only on the word
# "leverage", and one on "the use of leverage" plus an "HR Workday system" disclosure.
# A keyword grep cannot tell a job board from an adjective.
#
# The `?for=` and `embed/job_board` shapes matter as much as the plain link: Verition's
# and Eclipse's slugs appear nowhere on their careers pages except inside a Greenhouse
# embed script, which is exactly the "click a couple more buttons" case.
_FINGERPRINTS = (
    (ats.GREENHOUSE, re.compile(
        r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"
    )),
    (ats.GREENHOUSE, re.compile(r"greenhouse\.io/embed/job_board[^\"'\s]*?[?&]for=([A-Za-z0-9_-]+)")),
    (ats.GREENHOUSE, re.compile(r"grnhse_app[^\"'\s]*?[?&]for=([A-Za-z0-9_-]+)")),
    # The API host, which a site calling Greenhouse from its own JavaScript embeds
    # directly. Graham's slug appears nowhere else on its careers page, and
    # "boards-api" is not "boards", so the link patterns above miss it.
    (ats.GREENHOUSE, re.compile(
        r"boards-api(?:\.eu)?\.greenhouse\.io/v\d+/boards/([A-Za-z0-9_-]+)"
    )),
    (ats.LEVER, re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)")),
    (ats.ASHBY, re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)")),
)

# Slugs that are never a board. `embed`, `job_board` and friends appear in the path of
# the very URLs the patterns above match, so without this the probe proposes a board
# called "embed" on every Greenhouse-embedding page in the watchlist.
# Links worth following one hop from a watched careers page. The bug, as found by hand,
# was that the real board sat a couple of clicks below the watched page, and measured:
# probing only the watched page rediscovered 8 of the 11 boards the hand audit found.
# Eclipse's and Verition's slugs appear nowhere on their careers pages -- they are on
# an "all jobs" / "open positions" page one link deeper, inside a Greenhouse embed.
_JOB_LINK = re.compile(
    r"""href=["']([^"']*(?:all-?jobs|open-?positions|open-?roles|job-?openings|"""
    r"""current-?openings|careers?/(?:jobs|openings|search)|view-?jobs|"""
    r"""join-?us/jobs|positions)[^"']*)["']""",
    re.IGNORECASE,
)
# Bounded hard: this runs over ~65 pages inside the weekly discovery budget, so a page
# that links to forty things must not turn into forty fetches.
MAX_HOPS_PER_PAGE = 3

_NOT_A_SLUG = frozenset({
    "embed", "job_board", "job_boards", "jobs", "js", "board", "boards", "api", "v1",
    "for", "www", "assets", "static", "images", "css", "error", "404",
})

# Vendors we can recognise but cannot yet watch, because their slug is a whole tenant
# URL rather than a name, or because no method reads them. Finding one is still worth
# saying: it means the firm HAS a machine-readable board and the page_text row watching
# its marketing page is the wrong target. Reported for a human, never auto-proposed.
_UNSUPPORTED_VENDORS = (
    ("workday", re.compile(r"[A-Za-z0-9_-]+\.(?:wd\d+\.)?myworkdayjobs\.com|myworkdaysite\.com")),
    ("phenom", re.compile(r"cdn\.phenompeople\.com|phenom\.com/api")),
    ("eightfold", re.compile(r"\.eightfold\.ai|/api/apply/v2/jobs")),
    ("workable", re.compile(r"(?:apply|[A-Za-z0-9_-]+)\.workable\.com")),
    ("smartrecruiters", re.compile(r"(?:api|jobs|careers)\.smartrecruiters\.com")),
    ("icims", re.compile(r"[A-Za-z0-9_-]+\.icims\.com")),
    ("rippling", re.compile(r"(?:api|ats)\.rippling\.com")),
    ("teamtailor", re.compile(r"[A-Za-z0-9_-]+\.teamtailor\.com")),
)


@dataclass(frozen=True)
class Candidate:
    kind: str  # "repo" | ats method name
    key: str  # dedupe key, e.g. "repo:owner/name" or "greenhouse:drweng"
    title: str
    url: str
    evidence: str
    # Filled by `triage`. "high" | "low", or "" when the judgment did not run at all --
    # which is a third state and not a synonym for "low", because an unjudged candidate
    # is surfaced rather than discarded.
    priority: str = ""
    # One sentence: what this source is, and why it is or is not worth watching. This is
    # what the digest prints; `evidence` is the mechanical proof that it can be watched
    # at all and stays in data/discovered.csv for whoever goes and looks.
    description: str = ""
    # The watched *aggregator* this slug was mined out of, when it was. Set only for
    # aggregators, because that is the case where adding the board is redundant: the
    # aggregator's own rows already reach the digest every morning. A slug found behind
    # a watched `page_text` row is the opposite -- the 2026-09-12 audit found 13 of
    # those pointing at marketing pages above the real board -- so those are left blank.
    covered_by: str = ""


PRIORITY_SYSTEM_PROMPT = f"""You triage newly discovered *sources* for an opportunity tracker.

A source is a job board, a company careers page, or a GitHub repository that lists
internships. It is watched every morning and its changes are reported. You are not
judging a posting -- you are judging whether adding this source is worth the owner's
attention at all.

THE PERSON:
{profile.OWNER_PROFILE}

The watchlist already holds about 150 sources: five internship aggregators, and the
boards of some seventy named firms. So the question is never "is this a real internship
source". It is "does this carry something the owner would not otherwise see". Be
sparing. Three sources the owner adds are worth more than eighteen skimmed past.

"priority": answer "high" only if at least one of these is true.

  - QUANTITATIVE FINANCE. A proprietary trading firm, market maker, hedge fund or quant
    research shop. This is this person's first field, and the one where the goal is every
    posting a firm has rather than whichever ones an aggregator happened to list.
  - A LARGE AGGREGATOR. A repository with thousands of stars tracking maths, CS or quant
    internships. A small one only duplicates the five already watched.
  - YOU CANNOT TELL WHAT THE FIRM DOES. Not a licence to guess: if the material does not
    identify the business, say "high" and let a human look. Discarding is permanent, so
    an unidentified source must never be thrown away on a hunch.

Answer "low" for everything else. In particular:

  - ALREADY COVERED. The input says "already watched: <source>" when the board was mined
    out of an aggregator this tracker reads every morning, which means its postings
    already reach the owner in the digest. On its own that is enough for "low" -- overridden
    only by quantitative finance, where whole-board coverage is the point rather than
    whichever postings an aggregator happened to carry.
  - A generic software employer that runs an internship. Real, but one of thousands, and
    the aggregators list it already.
  - A business whose engineering is not software: aerospace, energy, hardware,
    manufacturing, agriculture, insurance. One software internship among twenty
    mechanical ones does not change what the firm is.
  - Experienced-hire-only, clearance-gated, or recruiting only outside the United States.
  - A test or staging duplicate of a board already proposed.

"description": exactly one sentence, plain and specific, saying what the source is and
  why it does or does not matter to this person. Name the firm's actual business rather than
  restating its name. No preamble."""


PRIORITY_SCHEMA = {
    "type": "object",
    "properties": {
        "priority": {"type": "string", "enum": ["high", "low"]},
        "description": {"type": "string"},
    },
    "required": ["priority", "description"],
    "additionalProperties": False,
}

# A weekly pass finds a handful; this is a ceiling against a search that suddenly
# returns hundreds, not an expected volume. Anything past it is kept unjudged rather
# than discarded, for the same reason the prompt breaks ties towards "high".
MAX_PRIORITY_JUDGMENTS = 40


def triage(candidates: list[Candidate]) -> tuple[list[Candidate], list[Candidate], list[str]]:
    """Split into (keep, discard, notes). Never raises.

    Degrades towards keeping. With no API key, with a provider that will not build, or
    on a call that fails, every candidate is kept unjudged and the digest says so -- the
    same rule the classifier follows on a change it could not read (spec 8 rule 3).
    Discarding is permanent in effect, because `run` never re-proposes a key already on
    file, so it may only ever happen on an answer the model actually gave.
    """
    if not candidates:
        return [], [], []

    provider = classify.select_provider()
    if provider is None:
        return list(candidates), [], [
            f"discovery: no API key, so {len(candidates)} proposal(s) are listed "
            "unjudged rather than filtered by priority."
        ]
    try:
        client, deployment = classify.build_client(provider)
    except Exception as exc:
        return list(candidates), [], [
            f"discovery: the priority triage could not start "
            f"({type(exc).__name__}), so {len(candidates)} proposal(s) are unjudged."
        ]

    keep: list[Candidate] = []
    discard: list[Candidate] = []
    notes: list[str] = []
    failures = 0
    for candidate in candidates:
        if len(keep) + len(discard) >= MAX_PRIORITY_JUDGMENTS:
            keep.append(candidate)
            continue
        parsed, error = classify.call_json(
            provider, client, deployment,
            system=PRIORITY_SYSTEM_PROMPT,
            user=(
                f"kind: {candidate.kind}\n"
                f"name: {candidate.title}\n"
                f"url: {candidate.url}\n"
                f"how it was found: {candidate.evidence}"
                + (f"\nalready watched: {candidate.covered_by}"
                   if candidate.covered_by else "")
            ),
            schema=PRIORITY_SCHEMA,
            schema_name="source_priority",
        )
        if parsed is None:
            failures += 1
            keep.append(candidate)
            continue
        priority = str(parsed.get("priority") or "").strip().lower()
        description = " ".join(str(parsed.get("description") or "").split())
        judged = replace(candidate, priority=priority, description=description)
        # Only an explicit "low" discards. An answer that is neither is a malformed
        # response, and a malformed response must not be read as a verdict.
        (discard if priority == "low" else keep).append(judged)

    if failures:
        notes.append(
            f"discovery: {failures} proposal(s) could not be judged for priority and "
            "are listed unjudged."
        )
    return keep, discard, notes


def due(today: str | None = None) -> bool:
    """Monday, or any day when the last run was over a week ago.

    The fallback matters more since the schedule dropped to a single tick: a Monday on
    which that tick is dropped -- which this project has already seen happen -- would
    otherwise skip a whole week in silence, and silence is the one signal this tool is
    built to make meaningful.
    """
    date = datetime.date.fromisoformat(today or clock.today_iso())
    if date.weekday() == 0:
        return True
    last = store.read_last_discovery()
    if not last:
        return False
    try:
        return (date - datetime.date.fromisoformat(last)).days >= 7
    except ValueError:
        return False


def _known(sources: list[dict[str, str]]) -> set[str]:
    known = set()
    for source in sources:
        url = (source.get("url") or "").strip()
        method = (source.get("method") or "").strip()
        if method == "github_readme":
            # The repo, not the file: a row may name a path inside it.
            known.add(f"repo:{github_readme.split_target(url)[0].lower()}")
        elif method in ats.SINGLE_SHOT:
            known.add(f"{method}:{url.lower()}")
        elif method in ats.BULK:
            # Every board in a bulk source's list is watched, though none has a row.
            vendor = ats.BULK[method][0]
            known |= {f"{vendor}:{slug.lower()}" for slug in store.read_board_list(url)}
    return known


def search_repos(client: httpx.Client, known: set[str], deadline: float) -> list[Candidate]:
    found: dict[str, Candidate] = {}
    cutoff = datetime.date.fromisoformat(clock.today_iso()) - datetime.timedelta(
        days=PUSHED_WITHIN_DAYS
    )
    for query in SEARCH_QUERIES:
        if time.monotonic() > deadline:
            break
        try:
            response = client.get(
                GITHUB_SEARCH,
                params={"q": query, "sort": "updated", "per_page": 15},
            )
            response.raise_for_status()
            items = response.json().get("items", [])
        except Exception:
            continue  # one bad query must not lose the others
        for repo in items:
            full = (repo.get("full_name") or "").strip()
            key = f"repo:{full.lower()}"
            if not full or key in known or key in found:
                continue
            if repo.get("archived") or repo.get("fork"):
                continue
            if (repo.get("stargazers_count") or 0) < MIN_STARS:
                continue
            pushed = (repo.get("pushed_at") or "")[:10]
            try:
                if datetime.date.fromisoformat(pushed) < cutoff:
                    continue
            except ValueError:
                continue
            found[key] = Candidate(
                kind="repo",
                key=key,
                title=full,
                url=repo.get("html_url") or "",
                evidence=(
                    f"{repo.get('stargazers_count')} stars, pushed {pushed}; matched "
                    f"\"{query.split(' in:')[0]}\""
                ),
            )
        time.sleep(paths.REQUEST_DELAY_SECONDS)
    return list(found.values())


def mine_snapshots(known: set[str]) -> list[Candidate]:
    """Board slugs already sitting in the snapshots we store. No network."""
    found: dict[str, Candidate] = {}
    if not paths.SNAPSHOTS.exists():
        return []
    for path in sorted(paths.SNAPSHOTS.glob("*")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for method, pattern in _FINGERPRINTS:
            for slug in pattern.findall(text):
                key = f"{method}:{slug.lower()}"
                if key in known or key in found:
                    continue
                found[key] = Candidate(
                    kind=method,
                    key=key,
                    title=slug,
                    url=ats.ENDPOINTS[method].format(slug=slug),
                    evidence=f"linked from {path.name}; not in sources.csv",
                    # REPO_CONFIGS *is* the aggregator set: a github_readme source
                    # without a parser config cannot be read at all. A slug mined from
                    # any other snapshot -- an ATS board, a watched page -- is not
                    # covered by anything and is left blank.
                    covered_by=(
                        path.stem if path.stem in parse_readme.REPO_CONFIGS else ""
                    ),
                )
    return list(found.values())


def _read_page(client: httpx.Client, url: str) -> list[str]:
    """Fetch one page, or [] if it cannot be read.

    A page we cannot read is page_watch's problem to report, not the probe's -- the
    probe going quiet about an unreachable page is correct, because something else is
    already shouting about it.
    """
    try:
        response = client.get(url)
        if response.status_code >= 400:
            return []
        body = response.text
    except Exception:
        return []
    time.sleep(paths.REQUEST_DELAY_SECONDS)
    return [body]


def _job_links(html: str, base: str) -> list[str]:
    """Absolute, same-site links from a careers page that look like a listing page."""
    out: list[str] = []
    seen = set()
    for href in _JOB_LINK.findall(html):
        target = urllib.parse.urljoin(base, html_module.unescape(href))
        parts = urllib.parse.urlsplit(target)
        if parts.scheme not in ("http", "https"):
            continue
        # Same registrable-ish host only. Following off-site links would turn a probe
        # of our own watchlist into a crawler.
        if parts.netloc.removeprefix("www.") != urllib.parse.urlsplit(base).netloc.removeprefix("www."):
            continue
        clean = parts._replace(fragment="").geturl()
        if clean.rstrip("/") == base.rstrip("/") or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def mine_pages(
    sources: list[dict[str, str]], client: httpx.Client, known: set[str], deadline: float
) -> tuple[list[Candidate], list[str]]:
    """Probe every page_text source for the real job board hiding behind it.

    This exists because of a specific, repeated failure. A `page_text` row is supposed
    to be the fallback for a firm on no public ATS, and the decision that a firm has no
    public ATS was a documented *manual* step -- so roughly fifty rows were added
    without anyone running the check. A manual check found the real board a couple of
    clicks below the watched page, on a Greenhouse site with the actual job board
    postings on it. The subsequent audit found thirteen such rows,
    every one carrying a note asserting the firm was on no public ATS.

    A rule that lives only in a document is a rule that gets skipped, so the probe now
    runs itself. It **proposes and never adds** (`discover.record` writes
    data/discovered.csv, never sources.csv), which is the standing rule for everything
    in this module.

    Returns (candidates, notes). The notes carry the vendors we can recognise but
    cannot watch -- finding one still means the firm has a machine-readable board and
    the page_text row is pointed at the wrong thing.
    """
    found: dict[str, Candidate] = {}
    notes: list[str] = []
    for source in sources:
        if (source.get("method") or "").strip() != "page_text":
            continue
        if time.monotonic() > deadline:
            notes.append("discovery: the page probe ran out of budget before finishing.")
            break
        url = (source.get("url") or "").strip()
        if not url:
            continue
        pages = _read_page(client, url)
        if not pages:
            continue
        # One hop deeper, because that is where the bug actually lives.
        for link in _job_links(pages[0], url)[:MAX_HOPS_PER_PAGE]:
            if time.monotonic() > deadline:
                break
            pages += _read_page(client, link)
        html = "\n".join(pages)

        for method, pattern in _FINGERPRINTS:
            for slug in pattern.findall(html):
                if slug.lower() in _NOT_A_SLUG:
                    continue
                key = f"{method}:{slug.lower()}"
                if key in known or key in found:
                    continue
                found[key] = Candidate(
                    kind=method,
                    key=key,
                    title=slug,
                    url=ats.ENDPOINTS[method].format(slug=slug),
                    evidence=(
                        f"found in the HTML of {source['source_id']}, which is watched "
                        f"as page_text; not in sources.csv"
                    ),
                )
        for vendor, pattern in _UNSUPPORTED_VENDORS:
            if pattern.search(html):
                notes.append(
                    f"discovery: {source['source_id']} is watched as page_text but its "
                    f"HTML carries a {vendor} fingerprint — it has a real board this "
                    "tool cannot read yet. Worth a look by hand."
                )
                break
    return list(found.values()), notes


def verify(candidates: list[Candidate], client: httpx.Client, deadline: float) -> list[Candidate]:
    """Drop mined slugs that do not actually answer, so no dead proposal is made."""
    checked: list[Candidate] = []
    budget = MAX_SLUG_VERIFICATIONS
    for candidate in candidates:
        if candidate.kind == "repo":
            checked.append(candidate)
            continue
        if budget <= 0 or time.monotonic() > deadline:
            break
        budget -= 1
        source = {"source_id": candidate.key, "method": candidate.kind,
                  "url": candidate.title, "program_names": ""}
        fetched = ats.fetch(source, client, wants_detail=parse_ats.wants_workday_detail)
        # No previous snapshot by construction -- a candidate has never been watched --
        # so this is a baseline assessment, which is exactly what "does the slug
        # actually answer" needs.
        result = parse_ats.assess(source, fetched, previous=None)
        if not result.ok:
            continue
        rows = result.extra.get("rows", 0)
        total = result.extra.get("postings", 0)
        checked.append(
            Candidate(
                kind=candidate.kind,
                key=candidate.key,
                title=candidate.title,
                url=candidate.url,
                evidence=f"{candidate.evidence}; API returns {total} postings, {rows} US student rows",
            )
        )
        time.sleep(paths.REQUEST_DELAY_SECONDS)
    return checked


def run(
    github: httpx.Client, web: httpx.Client, sources: list[dict[str, str]]
) -> tuple[list[Candidate], list[str]]:
    """Never raises. Returns (candidates, notes-for-the-health-block)."""
    deadline = time.monotonic() + BUDGET_SECONDS
    notes: list[str] = []
    known = _known(sources)
    seen = {row["key"] for row in store.read_discovered()}

    candidates: list[Candidate] = []
    try:
        candidates += search_repos(github, known, deadline)
    except Exception as exc:
        notes.append(f"discovery: the GitHub repo search failed ({type(exc).__name__}).")
    try:
        candidates += verify(mine_snapshots(known), web, deadline)
    except Exception as exc:
        notes.append(f"discovery: ATS fingerprint mining failed ({type(exc).__name__}).")
    try:
        mined, page_notes = mine_pages(sources, web, known, deadline)
        notes += page_notes
        candidates += verify(mined, web, deadline)
    except Exception as exc:
        notes.append(f"discovery: the page_text ATS probe failed ({type(exc).__name__}).")

    # A key already on file is never re-proposed, whatever its status. `rejected` is a
    # permanent tombstone; without this the same candidates arrive every Monday.
    fresh = [c for c in candidates if c.key not in seen]
    return fresh, notes


DISCARDED = "discarded-low-priority"


def record(candidates: list[Candidate], discarded: list[Candidate] | None = None) -> None:
    """Append to data/discovered.csv. Never touches sources.csv.

    A discarded candidate is written too, and that is the whole of its recoverability:
    `run` skips any key already on file, so the row is what stops it being re-proposed
    every Monday, and it is also the only place its one-sentence description survives.
    Setting its status back to `proposed` puts it in the next digest.

    Its status is deliberately not `rejected`. That word means the owner looked at a
    proposal and said no, and it is worth being able to tell the two apart later --
    one is a judgment the owner made and the other is a judgment made on the owner's
    behalf.
    """
    if not candidates and not discarded:
        return
    rows = store.read_discovered()
    today = clock.today_iso()
    for candidate, status in (
        [(c, "proposed") for c in candidates] + [(c, DISCARDED) for c in discarded or []]
    ):
        rows.append(
            {
                "first_proposed": today,
                "last_proposed": today,
                "kind": candidate.kind,
                "key": candidate.key,
                "title": candidate.title,
                "url": candidate.url,
                "evidence": candidate.evidence,
                "description": candidate.description,
                "status": status,
            }
        )
    store.write_discovered(rows)


def lines(
    candidates: list[Candidate], limit: int = 10, discarded: int = 0
) -> list[str]:
    """The DISCOVERED block. High priority only, one sentence each.

    The sentence replaces the mechanical evidence string that used to be printed here
    ("greenhouse slug found on the careers page"), which answered "can we watch this"
    when the only question the owner has is "is this worth watching". The evidence is
    still on the row in data/discovered.csv.
    """
    out = [
        "_Proposals only — nothing has been added. Accept one with the "
        "`/add-opportunity` skill; silence it for good by setting `status=rejected` "
        "on its row in `data/discovered.csv`._",
        "",
    ]
    for candidate in candidates[:limit]:
        # An unjudged candidate has no sentence, so it falls back to the evidence rather
        # than printing a bare title. It is here *because* it could not be judged.
        note = candidate.description or f"unjudged — {candidate.evidence}"
        out.append(f"- **{candidate.kind}** `{candidate.title}` — {note} → {candidate.url}")
    if len(candidates) > limit:
        out.append(f"- …and {len(candidates) - limit} more in `data/discovered.csv`.")
    if discarded:
        # Every filter reports what it removed. A triage that quietly ate the whole
        # week's findings would look exactly like a quiet week for new sources, and an
        # implausible count here is the cheapest signal that the prompt has drifted.
        out.append(
            f"- _{discarded} further proposal(s) judged low priority and discarded; "
            "they are on file in `data/discovered.csv`._"
        )
    return out
