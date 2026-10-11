"""Turn a fetched page into normalised lines, and judge whether the fetch was real.

Spec section 7's insight, unchanged: do not write a scraper that extracts postings from
these pages. Snapshot the readable text, diff it, and let the model read the diff. A
competition site that adds "2027 registration is open" is reporting itself; a parser
that tried to understand the page would break on the next redesign.

Two content floors keep a broken page from reading as a quiet one (spec 10.1), and the
ratio rule is the load-bearing one: three of the watched pages sit near 1,000
characters, close enough that a partial render could clear any sane absolute floor while
losing everything that matters.

Both floors ask whether a page has *enough text*, which is why they catch a JavaScript
shell and are blind to the opposite failure -- a marketing or error page, full of prose,
containing no listings. That blind spot is `redirect.verdict`'s job, and it is checked
here before the floors, because a page that landed somewhere else is wrong regardless
of how much prose it returned.

Pure. No network, no disk: `previous` arrives as an argument, which is what lifted
`read_snapshot` out of the fetch path.
"""
from __future__ import annotations

import collections
import json
import re

from core import clock, models
from gather import page
from process import redirect
from core import text as coretext
from process import snapshot

# Below this a response is a JavaScript shell or a block page, not a short page.
# Calibrated against measurements, not guessed: the thinnest real page in the watchlist
# is hedgewestsf at 837 characters, while SIG's careers page yields 221 from 402KB of
# HTML. An earlier value of 200 let SIG through.
MIN_ABSOLUTE_CHARS = 500

# The stronger signal, because it does not care how big the page is. A shell is mostly
# markup: SIG measures 0.0005 text-to-HTML, while the thinnest genuine page in the
# watchlist (imc-launchpad-us, 2,472 chars from 678KB) measures 0.0036 -- seven times
# higher. Everything else measured between 0.01 and 0.31.
MIN_TEXT_HTML_RATIO = 0.0015

# Losing this much of a page is a redesign, a block, or a partial render. Self
# calibrating against the last good fetch, so there is no per-source constant to rot.
SHRINK_RATIO = 0.4

# A nav change can move dozens of lines; the digest only needs enough to judge.
MAX_DIFF_LINES = 12

# Past this many added lines a page diff is list churn, not one announcement, and its
# text no longer says anything about a single programme. On 2026-10-08 a Built In list
# page added 41 lines, matched the discovery and rolling-firm patterns somewhere in them,
# and went into the High tier as though it were a discovery programme. Its rows still
# reach the digest and the classifier; they just do not inherit either flag.
MAX_FLAGGED_ADDED_LINES = 10

# How much of a page under the absolute floor is quoted in its failure message. A block
# page is short by definition, so quoting it costs one line of HEALTH and answers the
# only question that matters: who answered. On 2026-10-08 three unrelated sites (on
# LiteSpeed, nginx and Apache, on three different hosts) failed from the Actions runner
# with an identical "73 characters of text" while all three answered a laptop in full,
# and nothing recorded what those 73 characters were.
SHORT_PAGE_QUOTE_CHARS = 160

# No single diff line may be longer than this. Several watched sources are vendor JSON
# feeds (Workable, Rippling, Teamtailor, Pinpoint) served as one unbroken line, and
# Wolverine's is 147,741 characters. Emitting that verbatim would put a single line past
# GitHub's 65,536-character issue-body limit and fail delivery outright -- turning a
# one-byte upstream edit into a missed digest.
MAX_DIFF_LINE_CHARS = 400

_DROP = re.compile(r"(?is)<(script|style|noscript|svg|iframe|template)\b.*?</\1>")
_HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")
_BLOCK_END = re.compile(
    r"(?is)</(p|div|li|tr|h[1-6]|section|article|header|footer|nav)\s*>|<br\s*/?>"
)
_TAG = re.compile(r"(?s)<[^>]+>")

# Per-request noise. Replaced with a placeholder rather than deleted so that a
# genuinely new element still shows up as an added line instead of vanishing into a
# collapse.
_NOISE = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"[?&](v|ver|t|ts|cb|nocache|_)=[^\s&\"'<>]+"), ""),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T[\d:.+Z-]+"), "<timestamp>"),
    (re.compile(r"(?i)\bcsrf[-_]?token\S*"), "<csrf>"),
    # A job list's relative age -- "Posted: 20 days" on the Getro boards, "Posted 8 hours
    # ago" on a16z's -- rewrites every row every day. Anchored on "Posted" so that a
    # sentence like "Today, OpenGov announced" keeps its word.
    (re.compile(
        r"(?i)\bposted:?\s+(?:today|yesterday|anytime|just\s+now"
        r"|(?:an?|\d+)\s+(?:minute|hour|day|week|month|year)s?(?:\s+ago)?)\b"),
     "Posted <age>"),
    # Built In's job cards print a bare age before the card's "Saved" control --
    # "11 Hours Ago Saved", "Reposted Yesterday Saved". Anchored on that control so a
    # sentence containing "yesterday" keeps its word.
    (re.compile(
        r"(?i)\b(?:reposted\s+)?(?:today|yesterday|just\s+now"
        r"|(?:an?|\d+)\s+(?:minute|hour|day|week|month|year)s?\s+ago)(?=\s+saved\b)"),
     "<age>"),
)


def normalise(raw_html: str) -> list[str]:
    """Readable text as lines.

    Line-preserving rather than one blob, so the committed snapshot diffs readably in
    `git log` the way the Tier 1 TSVs do. Deliberately not sorted: unlike a table, a
    page's order carries meaning, and sorting would turn a moved section into a pile of
    spurious additions.
    """
    text = _DROP.sub(" ", raw_html)
    text = _HTML_COMMENT.sub(" ", text)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = coretext.extract_text(text) if "<" in text else text
    # Vendor JSON feeds (Workable, Rippling, Teamtailor, Pinpoint) arrive as one line, so
    # a line differ can only ever say "the whole feed changed". Breaking on record
    # boundaries makes a new posting show up as one added line instead.
    if text.lstrip()[:1] in ("{", "["):
        text = text.replace("},{", "},\n{").replace("}, {", "},\n{")
    for pattern, placeholder in _NOISE:
        text = pattern.sub(placeholder, text)
    lines = []
    for line in text.split("\n"):
        collapsed = " ".join(line.split())
        if collapsed:
            if lines and _COUNTDOWN_UNIT.match(lines[-1]) and _COUNTDOWN_VALUE.match(collapsed):
                collapsed = "<n>"
            lines.append(collapsed)
    return lines



def _quote(lines: list[str]) -> str:
    """A short page's own text, on one line and clipped, for a failure message."""
    joined = " / ".join(lines).replace("`", "'")
    if len(joined) > SHORT_PAGE_QUOTE_CHARS:
        joined = joined[: SHORT_PAGE_QUOTE_CHARS - 1].rstrip() + "…"
    return f'"{joined}"' if joined else "(no text at all)"


def _same_feed(previous: str, text: str) -> bool:
    """True when both texts are the same vendor JSON feed, so a shrink is postings closing.

    The shrink guard exists for an HTML page that lost its body to a redesign, a block or
    a partial render. A feed cannot partially render, and a block page is not JSON with
    the feed's own keys, so when both versions parse to the same shape the smaller one is
    simply fewer postings. Lynx's Teamtailor feed went from two items to one on
    2026-09-30 when its hackathon closed, and without this the source would have failed
    every morning from then on, because a failed fetch never replaces its baseline.
    """
    try:
        old, new = json.loads(previous), json.loads(text)
    except ValueError:
        return False
    if isinstance(old, dict) and isinstance(new, dict):
        return old.keys() == new.keys()
    return isinstance(old, list) and isinstance(new, list)

# A listing page's own statement that it has nothing listed, as a whole line. Anchored at
# both ends so prose that merely contains the words ("there are no events like it") is
# not read as an empty state.
_EMPTY_LISTING = re.compile(
    r"(?i)^(?:there\s+are\s+)?(?:currently\s+)?no\s+"
    r"(?:upcoming\s+|current\s+|open\s+|available\s+)?"
    r"(?:events?|positions?|openings?|jobs?|opportunities|postings?|programs?)"
    r"(?:\s+(?:are\s+)?(?:available|found|posted|scheduled|listed|open|currently"
    r"|at\s+this\s+time))*\s*[.!]?$")


def _states_empty_listing(previous: str, lines: list[str]) -> bool:
    """True when the page now says, in a line of its own, that its listing is empty.

    The shrink guard is for a page that lost its body to a redesign, a block or a partial
    render. A listing page whose last item has passed loses most of its text too, and
    says so: IAQF's upcoming-events page went from 3,878 to 1,380 characters on
    2026-10-07 when its one seminar (6 Oct) passed, and now reads "No events available".
    A failed fetch never replaces its baseline, so without this the source would fail
    every morning until IAQF next schedules an event. The statement has to be new, so a
    page that already said it and then lost its body still fails.
    """
    before = set(previous.splitlines())
    return any(_EMPTY_LISTING.match(line) and line not in before for line in lines)


# A live countdown renders one unit per line -- "Days", "69", "Hours", "03", "Minutes",
# "07", "Seconds", "57" on the SMART Scholarship page (2026-09-26) -- so it would read as
# a changed page on every run. Only a bare number directly after a bare unit is masked:
# "Applications close in 5 days" keeps its number, because that is the news.
_COUNTDOWN_UNIT = re.compile(r"^(days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)$", re.I)
_COUNTDOWN_VALUE = re.compile(r"^\d{1,3}$")


# A line that names a posting, as a job list prints one: "Summer 2027 Internship - Embedded
# Software Engineering", "2027 Summer Intern: Video Operations Tools and Automation".
# Banks say "Summer Analyst" with no "intern" anywhere. Bounded in length so Built In's
# one-paragraph card summaries ("Tax interns support client engagements by ...") are not
# read as titles, and a URL never is: the tal.net feeds put the posting's own link, slug
# and all, on the line above its title.
_POSTING_TITLE = re.compile(
    r"(?i)\b(?:intern(?:ship)?s?|co-?ops?|apprentice(?:ship)?s?|fellowships?"
    r"|summer\s+(?:20\d\d|analyst|associate|scholar|student)s?)\b")
MAX_TITLE_CHARS = 160

# A line with one of these is a date or a deadline, so a change to its digits is the
# news ("Applications close October 15" -> "October 22") rather than a counter ticking.
_DATED = re.compile(
    r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b"
    r"|deadline|\bdue\b|\bclos|\bopen|apply\s+by|\d{1,2}/\d{1,2}")
_DIGITS = re.compile(r"\d[\d,.]*")


def _signature(line: str) -> tuple[str, ...]:
    """A line's words as a sorted bag, digits masked unless the line is a date.

    Two lines with one signature say the same thing: Getro's and Consider's boards
    reshuffle every card's skill tags daily ("Git SciPy Pandas" -> "SciPy Pandas Git"),
    and their headers recount ("18,442 jobs" -> "18,457 jobs").
    """
    words = line.lower().split()
    if not _DATED.search(line):
        words = [_DIGITS.sub("#", word) for word in words]
    return tuple(sorted(words))


def _minus(lines: list[str], other: collections.Counter, key=lambda line: line) -> list[str]:
    """`lines` without one occurrence per count in `other`, in order."""
    remaining = collections.Counter(other)
    out = []
    for line in lines:
        if remaining[key(line)] > 0:
            remaining[key(line)] -= 1
        else:
            out.append(line)
    return out


def page_news(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    """(added, removed) lines once reordering and churn are taken out.

    A multiset difference rather than a sequence diff. A sequence diff reports a moved
    block as removed in one place and added in another, and job lists move blocks all
    the time: on 2026-10-11 Lightspeed swapped two Amca cards and MLH two hackathons,
    which read as "40 added, 40 removed" and "10 added, 20 removed" while nothing was
    posted or taken down. Then lines that pair up by `_signature`, and lines with no
    letter at all (TreeHacks' banner emoji, a bare count), come off both sides.
    """
    added = _minus(new, collections.Counter(old))
    removed = _minus(old, collections.Counter(new))
    common = (collections.Counter(map(_signature, added))
              & collections.Counter(map(_signature, removed)))
    added = _minus(added, common, _signature)
    removed = _minus(removed, common, _signature)

    def worded(line: str) -> bool:
        return any(ch.isalpha() for ch in line)

    return [l for l in added if worded(l)], [l for l in removed if worded(l)]


def posting_titles(lines: list[str]) -> list[str]:
    return [
        line for line in lines
        if len(line) <= MAX_TITLE_CHARS and not line.startswith(("http://", "https://"))
        and _POSTING_TITLE.search(line)
    ]


# The one line every posting card on a watched job list carries, by vendor: Getro's
# "Location: Seattle, WA, USA", a16z's and Consider's "Scotts Valley, California · Posted
# <age>", Built In's "<age> Saved". A new one is a new card even when its title never
# says "intern" -- these boards are filtered to internships server-side, and on
# 2026-10-07 a16z listed "Founders Associate - CEO Office" that way.
_CARD_ANCHOR = re.compile(r"^Location:|Posted <age>$|^<age> Saved$")


def new_postings(added: list[str], new: list[str]) -> list[str]:
    """What a job list gained, named the way the digest should print it.

    Posting titles among the added lines, plus one name per added card anchor: the
    posting title among the three lines above the anchor, or failing that the two lines
    directly above it (Getro and a16z put title and company there).
    """
    names = posting_titles(added)
    wanted = collections.Counter(line for line in added if _CARD_ANCHOR.search(line))
    for index, line in enumerate(new):
        if wanted[line] <= 0:
            continue
        wanted[line] -= 1
        above = new[max(0, index - 3):index]
        titled = posting_titles(above)
        names.append(titled[-1] if titled else " · ".join(above[-2:]))
    return list(dict.fromkeys(name for name in names if name))


def is_job_list(source: dict[str, str]) -> bool:
    """Whether the row is marked as a list of postings rather than a programme page.

    A mark on the row rather than a count of title lines, because the count does not
    separate the two: measured over the snapshots on 2026-10-11, Thrive's board carries
    13 distinct posting titles and Stanford's symsys-summer programme page 22. On a job
    list a change is news only when it adds a posting -- its other lines (skill tags,
    salary bands, "Showing 12 out of 18") move daily and name nothing to apply to. A
    programme page has no postings to look for, so its own text stays the signal.
    """
    return (source.get("job_list") or "").strip().lower() == "true"


# The two reasons a page that moved still produces no row, as HEALTH names them.
PAGE_CHURN = "page-churn"
PAGE_NO_POSTING = "page-no-new-posting"


def page_verdict(old: list[str], new: list[str], job_list: bool = False) -> str:
    """Why a moved page has no row, or "" when it has one.

    Separate from `diff_pages` so `assess` can report the drop: every filter reports
    what it removed, and a page that changed every day while printing nothing would
    otherwise be indistinguishable from one that stopped changing.
    """
    if old == new:
        return ""
    added, _ = page_news(old, new)
    if not added:
        # Nothing was added, or nothing but reordering and counters. A page that only
        # lost lines has nothing new to apply to, which is the removed-posting rule.
        return PAGE_CHURN
    # A discovery phrase keeps the row even without a title line: "Discovery Day
    # applications are open" is the news a firm's careers page exists to carry.
    discovery = snapshot.DISCOVERY_PATTERN.search(" ".join(added))
    if job_list and not new_postings(added, new) and not discovery:
        return PAGE_NO_POSTING
    return ""


def diff_pages(
    source_id: str, old: list[str], new: list[str], source: dict[str, str]
) -> list[models.Change]:
    """At most one Change per page per run, and none for a page with no news.

    Page diffs are lumpy -- a nav tweak moves forty lines -- so one Change per changed
    line would drown the digest and burn the classification budget on boilerplate.

    A job list that gained postings is keyed on them, so the digest names the posting
    rather than printing "page updated". "page updated" is kept for a programme page,
    which has no postings to name -- its own text is the only sign that something moved.
    """
    job_list = is_job_list(source)
    if page_verdict(old, new, job_list):
        return []
    added, removed = page_news(old, new)
    if not added and not removed:
        return []
    titles = new_postings(added, new) if job_list else []

    blob = " ".join(added)
    flaggable = len(added) <= MAX_FLAGGED_ADDED_LINES

    def clip(line: str) -> str:
        return line if len(line) <= MAX_DIFF_LINE_CHARS else (
            line[:MAX_DIFF_LINE_CHARS] + f" … [+{len(line) - MAX_DIFF_LINE_CHARS} chars]"
        )

    body = [f"+ {clip(line)}" for line in added[:MAX_DIFF_LINES]]
    body += [f"- {clip(line)}" for line in removed[:MAX_DIFF_LINES]]
    if len(added) > MAX_DIFF_LINES or len(removed) > MAX_DIFF_LINES:
        body.append(f"... {len(added)} added and {len(removed)} removed lines in total.")

    program_name = source.get("program_names", "")
    name = program_name or source_id
    if titles:
        more = f" (+{len(titles) - 1} more)" if len(titles) > 1 else ""
        key = f"{name}: {titles[0]}{more}"
    else:
        key = f"{name} ({len(added)} added, {len(removed)} removed)"
    return [
        models.Change(
            source_id=source_id,
            kind="changed",
            # One change per page per run, so the page *is* the identity. Stable across
            # runs on purpose: `enrich` and `screen` look themselves up by this, and a
            # key that folded in the added/removed counts would change every morning.
            change_id=clock.change_id(source_id, "page"),
            key=key,
            detail="\n".join(body),
            url=source.get("url", ""),
            program_name=program_name,
            # Added text only. A removed "Freshman" line is a programme going away, not
            # a discovery, and flagging it would push a closure into ACT NOW.
            is_discovery_candidate=flaggable and bool(snapshot.DISCOVERY_PATTERN.search(blob)),
            rolling=flaggable and bool(snapshot.ROLLING_PATTERN.search(blob)),
            posting_text=blob[: coretext.MAX_TEXT_CHARS],
        )
    ]


def assess(
    source: dict[str, str], fetched: page.PageFetch, previous: str | None
) -> models.SourceResult:
    """Decide what one page fetch means. Pure.

    `previous` is the last committed snapshot text, or None for "never seen". It is a
    parameter rather than a `read_snapshot` call because that is the whole point of the
    split: a stage that reads the database cannot be replayed from an artifact.
    """
    source_id = source["source_id"]
    attempt = fetched.attempt
    if not attempt.ok:
        return models.SourceResult(
            source_id=source_id, ok=False, error=attempt.error,
            content_length=attempt.size,
        )

    # Checked before the content floors: a page that landed somewhere else is wrong
    # however much prose it returned, and the floors are structurally unable to notice.
    severity, redirect_note = redirect.verdict(attempt.requested_url, attempt.final_url)
    if severity == "fail":
        return models.SourceResult(
            source_id=source_id, ok=False, error=redirect_note,
            content_length=attempt.size,
        )

    lines = normalise(fetched.html)
    text = "\n".join(lines)
    ratio = len(text) / max(attempt.size, 1)

    if len(text) < MIN_ABSOLUTE_CHARS:
        return models.SourceResult(
            source_id=source_id,
            ok=False,
            content_length=len(text),
            error=(
                f"page returned only {len(text)} characters of text "
                f"(renders in JavaScript, or was blocked): {_quote(lines)}"
            ),
        )
    # The ratio exists to catch a JavaScript shell, and a rendered page is by
    # construction not one -- Wix's rendered DOM is still 0.001 text. The absolute floor
    # above still applies, so a render that produced nothing still fails.
    rendered = (source.get("render_js") or "").strip().lower() == "true"
    if ratio < MIN_TEXT_HTML_RATIO and not rendered:
        return models.SourceResult(
            source_id=source_id,
            ok=False,
            content_length=len(text),
            error=(
                f"page yielded {len(text)} characters of text from "
                f"{attempt.size} of HTML (ratio {ratio:.4f}) - this is a "
                "JavaScript shell, not a page with little on it"
            ),
        )
    if (
        previous is not None
        and len(text) < SHRINK_RATIO * len(previous)
        and not _same_feed(previous, text)
        and not _states_empty_listing(previous, lines)
    ):
        return models.SourceResult(
            source_id=source_id,
            ok=False,
            content_length=len(text),
            error=(
                f"page shrank from {len(previous)} to {len(text)} characters - "
                "likely a redesign, a block, or a partial render"
            ),
        )

    extra = {"rows": len(lines), "chars": len(text), "ratio": round(ratio, 4)}
    # A "warn" redirect is not a failed fetch -- the page loads and the text is real --
    # but the row is no longer watching what it was configured for, so it rides along
    # in extra and HEALTH says so every morning until the url is corrected.
    if redirect_note:
        extra["redirected"] = redirect_note

    if previous is None:
        return models.SourceResult(
            source_id=source_id,
            ok=True,
            baseline=True,
            snapshot_text=text,
            snapshot_ext="txt",
            content_length=len(text),
            extra=extra,
        )

    old_lines = previous.splitlines()
    verdict = page_verdict(old_lines, lines, is_job_list(source))
    reasons = {
        PAGE_CHURN: "the page moved, but only by reordering, recounting or removing "
                    "lines, so nothing on it is new",
        PAGE_NO_POSTING: "a job list changed, but none of the added lines is a posting "
                         "(skill tags, salary bands, page furniture)",
    }
    filters = [
        models.FilterReport(
            stage="process", filter_id=filter_id, considered=int(old_lines != lines),
            removed=int(verdict == filter_id), source_id=source_id, reason=reason,
            samples=(source_id,) if verdict == filter_id else (),
        )
        for filter_id, reason in reasons.items()
    ]
    return models.SourceResult(
        source_id=source_id,
        ok=True,
        changes=diff_pages(source_id, old_lines, lines, source),
        filters=filters,
        snapshot_text=text,
        snapshot_ext="txt",
        content_length=len(text),
        extra=extra,
    )
