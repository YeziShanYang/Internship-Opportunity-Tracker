# Internship Opportunity Tracker

A daily job that watches 279 sources (about 600 job boards and programme pages in all)
for ML/AI, quant, math and CS internships, on behalf of one first-year undergraduate.
Each morning it reads the postings that changed overnight, drops the ones its user
can't apply to, and sends what's left as one table. It has delivered a digest every day since 2026-09-06, and the model calls cost a
median of 19 cents a day.

There is no server. The job runs on GitHub Actions, and each digest goes out twice. It
opens a GitHub Issue, which keeps the archive and is what the once-a-day check reads,
and it emails the same digest as HTML through [Resend](https://resend.com). On a busy
morning the issue is cut to fit GitHub's 65,536-character limit. The email carries every
row.

```
Opportunity digest — 2026-11-18

## ■ FOR FRESHMEN & UNDERCLASSMEN (6)

| Company           | Position                                   | Notes                                    |
|-------------------|--------------------------------------------|------------------------------------------|
| AISC              | Fast Start Scholarships (2027–2028)        | • Deadline: 2026-12-01                   |
| Molina Healthcare | Customer Experience & Analytics Intern     | freshman or sophomore · pinned 11-13     |

## ■ OPPORTUNITIES (104)

| Priority                         | Company        | Position                                 | Notes                        |
|----------------------------------|----------------|------------------------------------------|------------------------------|
| **High** · startup / small firm  | a16z portfolio | Robotics & Physical AI Spring Intern     | • Location: Miami, FL        |
| **High** · startup / small firm  | GV portfolio   | Inertia Summer 2027 Internship           | • Backed by: GV              |
| Medium · size unknown            | AeroVironment  | Autonomy & Robotics Engineering Intern   | • Location: Simi Valley, CA  |
| …                                |                |                                          |                              |

<details><summary>■ RULED OUT (170)</summary>

- CACI / Cybersecurity Intern — large defence contractor
- Draper / Software Engineering & DevOps Intern — large defence contractor

</details>

## ■ HEALTH
- 267 sources checked · 235 healthy · 30 FAILING
- classifier: 146 calls · 570,697 in · 54,147 out · ~$0.157 est. (gpt-5-mini, effort=low)
```

That morning was a bad one for sources: most of the 30 failures were Workday boards
behind a new Cloudflare check, which is covered under [Limitations](#limitations).

## Table of Contents
- [Overview](#overview)
- [How It Works](#how-it-works)
- [Results](#results)
- [Reflection](#reflection)
- [Resources](#resources)
- [Appendix](#appendix)
- [Running it](#running-it)

## Overview

Opportunities for first-year students are easy to miss until after they close. Most of
them are either a named underclassman programme (Jane Street FTTP, IMC Launchpad, Akuna
Academy) or a Summer 2027 posting on some firm's job board, and neither announces
itself. Programmes open on no fixed schedule. Quant firms review on a rolling basis and
fill before their stated deadline. The aggregator repos most students use are wide but
late, and thin on detail. Checking 150 pages by hand every morning is not sustainable.

The core of the tracker is a snapshot of every source, diffed against yesterday's. Most of
the work since has been about the ways that diff went wrong, which is what the
[Reflection](#reflection) covers.

What the project involved:

1. How to give a pipeline a shape and enforce it with a test. There are eight stages,
   each may import only the stages below it, and a test walks every module's syntax tree
   to check.
2. How to design for failures that don't crash. The real risk in a tool like this is a
   source that quietly returns zero rows for four months while its user concludes
   nothing has opened.
3. HTTP against real job sites: `httpx`, circuit breakers, `robots.txt`, User-Agent
   policy, a Cloudflare check, and six ATS vendor APIs.
4. Parsing messy input: markdown tables, HTML boards and JSON feeds, full of emoji,
   relative dates and section headings that change for no reason.
5. How to use an LLM as one part of a program. A deterministic screen runs before it, its
   output has a JSON schema, and without an API key everything still reaches the digest,
   unclassified.
6. Git as a database, which needs CSV writes that come out byte-identical so that
   `git log` stays readable.
7. Cost accounting. 81% of the most expensive run turned out to be reasoning tokens, so
   the digest now prints what each run spent.

The files worth reading, roughly in order:

1. [`run.py`](run.py), the entry point. Each stage runs on its own (`gather | process |
   enrich | screen | classify | render | deliver | all`), which is how a morning that
   looks wrong gets debugged.
2. [`jobs/daily.py`](jobs/daily.py) composes the stages into the morning digest. It shows
   the whole flow on one screen.
3. [`process/parse_readme.py`](process/parse_readme.py) handles the noise problem. Almost
   every rule in it traces back to one bad morning.
4. [`classify.py`](classify.py) holds the model call and the nine rules its prompt
   enforces.
5. [`deliver/urgency.py`](deliver/urgency.py) decides what goes at the top. It has been
   wrong in both directions.
6. [`data/`](data/) is the database: 369 programmes, 279 sources, the board lists behind
   the three bulk sources, and a snapshot per source, committed on every run.
7. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is the long version, with every failure
   mode, what it cost, and why the fix looks the way it does.

### What is being watched

A source is one of three kinds:

- An aggregator: someone else's curated list, like the SimplifyJobs internship repo. There
  are 12. They are wide, late and thin on detail, but they catch firms nobody would think
  to look up. Sixteen venture-capital portfolio boards play the same role for startups, with
  one useful difference. A company is on Sequoia's or Khosla's board only because the firm
  invested in it, so every startup there is real.
- A named employer's whole board, filtered by the tracker. 99 of these are read through an ATS
  API (Greenhouse, Workday, Ashby, Lever, Phenom, Eightfold). Another 356 small employers'
  boards are read in bulk. Firms on no public ATS, such as Hudson River Trading, Headlands
  and Renaissance, are watched as page text.
- A specific programme's own page, such as Jane Street FTTP, IMC Launchpad or MIT
  Pokerbots.

A link to a single posting is never a source. Posting URLs expire within the year, so a
watcher pointed at one would go dead without saying so. When a posting link is added, the
tool climbs to the employer's board and watches that instead.

The firm list came from published lists of quant firms rather than from memory. An
earlier version built from recall missed WH Trading, GTS, Bluefin, Eschaton, Kore, Vector,
Freestone Grove, Laurion, Rokos, Quantbot, Trexquant, Mako and BlackEdge. It also missed
the whole category of named first- and second-year discovery programmes, which matters
most for a first-year user.

## How It Works

Eight stages run in a fixed order. Each writes a file that the next one reads, so any
stage can be re-run on its own.

```
gather    →  fetch 267 sources                       most of a ~21 min run
process   →  parse and diff against yesterday         0.7s, no network
enrich    →  fetch the posting behind each row        scales with changes, not postings
             that moved
screen    →  rule out what a quoted phrase settles    deterministic, no model
classify  →  one model call per remaining change      ~$0.10-0.16 a day since 2026-11-15
render    →  build the digest
deliver   →  open the issue (once a day), then email the full digest
```

There are two network stages. Which postings to fetch is only known after the diff, so
bodies are fetched for the rows that moved, not for the ~33,000 postings on the boards. `enrich`
used to run inside the classifier, which meant the model stage was also doing network
I/O.

The stage boundary is tested. Running `run.py all` and then `run.py render` produces a
byte-identical digest with no network or model call. That is why the HEALTH block reads
from a stored metrics object. An earlier version read module globals, so a re-render in a
fresh process printed a summary of a run that never happened.

### Three filters, one of them a model

Most of what is read gets thrown away, in this order:

| Filter | Kind | What it does |
|:---|:---|:---|
| Student and US screens | deterministic | Drop postings that aren't student roles or aren't in the US: about 30,000 and 600 rows on a typical day |
| `screen/rules.py` | deterministic | Rule a posting out on a phrase it can quote, like "rising junior" or "PhD candidates". It never rules a posting in. It removed 36% of model calls with no wrong rule-outs |
| `classify.py` | model | Judge what the screen had no opinion on. Returns JSON: relevant, class year, location, deadline, employer size, confidence |

The screen comes first because it is free, and in the measurement above it made no wrong
rule-outs. When it has no opinion the change goes to the model unchanged, and a posting whose
text could not be fetched is never ruled out for that.

Every filter's count appears in the digest's HEALTH block. Anything the classifier rules
out is still listed, with a short reason, in a collapsed RULED OUT section. A filter that
removes things silently is how a source goes blind unnoticed, and that has happened twice
here.

### Values worth tuning

| Name | Value | What it does |
|:-----|:------|:-------------|
| `MAX_CHANGE_RATIO` | 0.25 | An aggregator reporting more than 25% of its rows changed has been restructured. Collapse it to one line |
| `MIN_CHANGES_TO_COLLAPSE` | 25 | Must trip along with the ratio, so a small repo turning over normally is left alone |
| `MAX_CHANGES_PER_BOARD` | 25 | The same guard for ATS boards, as a flat number |
| `QUARANTINE_AFTER_FAILURES` | 3 | After three failures in a row a source backs off: 6, 12, 24, 48, then 72 hours |
| `FAILURE_ESCALATION_THRESHOLD` | 3 | After three failures in a row a source is marked SOURCE BLIND in HEALTH and counted on the summary line |
| `MIN_TEXT_HTML_RATIO` | 0.0015 | Text-to-HTML floor. Catches a JavaScript shell serving 221 characters out of 402KB |
| `MIN_ABSOLUTE_CHARS` | 500 | A page with less text than this didn't really load |
| `SHRINK_RATIO` | 0.4 | A page below 40% of its last good size was redesigned or blocked. Measured per source, so there is no constant to maintain |
| `MAX_ISSUE_BODY_CHARS` | 65,536 | GitHub's limit. Over it, the API returns 422 and opens nothing, so the issue drops its least urgent rows. The email is never trimmed |
| `MAX_CLASSIFICATIONS_PER_RUN` | 250 | Spend ceiling, counted after the screen. Hit on 17 of 35 days from 2026-10-10 to 11-13, and rows past it are printed unjudged. Since judgments started being reused per posting, runs from 2026-11-15 have used 86 to 146 calls |
| `CLASSIFY_REASONING_EFFORT` | `low` | Was `minimal`. See [the model needed room to think](#the-model-needed-room-to-think) |
| `REQUEST_DELAY_SECONDS` | 1.0 | Requests go one at a time under an honest User-Agent |

## Results

| Measure | Value |
|:---|:---|
| Sources | 279: 267 fetched, plus 12 that block automation and are checked by hand |
| Breakdown | 99 ATS boards, 152 watched pages (16 of them VC portfolio boards), 12 aggregator repos, 3 bulk sources covering 356 small employers' boards, the NSF REU feed, and the 12 manual ones |
| Read each morning | About 33,000 job-board postings, cut to student roles and diffed against 116 board snapshots and 153 page snapshots |
| Programmes in the database | 369 |
| Digests delivered | Every day from 2026-09-06 to 2026-11-18, 74 days |
| Cost | Median $0.19 a day over 64 digests (2026-09-16 to 11-18), range $0.007 to $0.30, $11.18 in total |
| Tests | 404, of which 6 skip without an API key |
| Code | About 12,000 lines across 53 modules, plus 6,500 lines of tests |
| Runtime | Median 21 minutes on the Actions runner (13 to 31 over the last 30 runs), almost all of it `gather` |

### Coverage against the largest public list

The question this project has to answer is whether watching 279 sources beats
bookmarking [Simplify](https://github.com/SimplifyJobs/Summer2027-Internships), the most
used internship list. `process/coverage.py` measures it weekly and records each week in
[`data/coverage.csv`](data/coverage.csv). On 2026-11-13:

| Scope | Tracker | Simplify | Tracker has, Simplify doesn't | Of those, on no public list |
|:---|---:|---:|---:|---:|
| Open US internships in quant, math, CS, data and ML | 2,644 | 1,670 | 1,300 (+78%) | 63 |
| Quant only | 198 | 157 | 73 (+46%) | 16 |

"On no public list" means the posting came from an employer's own board and appears on
none of the aggregators the tracker reads, Simplify included.

How it is counted, and why the figure is a floor:

- Simplify is measured from its whole `listings.json`, every active posting under any
  term. An earlier comparison used the tracker's own snapshot of Simplify, which reads
  only some sections, and came out at 2.3x. That was wrong.
- Both sides are deduplicated the same way, by the employer's posting link, or failing
  that by company plus most of the title words. The second rule merges some different
  roles, which can only lower the tracker's count.
- Graduate-only and non-US postings are excluded on both sides.
- The method has not changed, but the watchlist has. The first baseline on 2026-10-01 was
  +14%. It reached +37% on 2026-10-08 and has run between +53% and +93% in the weekly
  measurements since 2026-10-12.
- "On no public list" shrinks as the tracker reads more lists. It fell from 125 in
  October to 63, because a posting leaves that column as soon as any aggregator the tracker
  reads carries it.

### Selectivity

Across the 59 digests from 2026-09-16 to 2026-11-13, half of the changed postings (6,775
of 13,621) were ruled out before reaching the table, on class year, field or employer, and
each is listed in RULED OUT with its reason. A median of 72 rows a day reached the table,
plus 9 in the underclassman block. That number grew with the watchlist to a median of 131
over the last 30 of those days, so more coverage has meant a longer digest. On the job
boards about 92% of postings are dropped as non-student roles before anything is diffed
(30,295 of 33,082 on 2026-11-13).

### Speed is not the advantage

Measured on Greenhouse postings on 2026-10-01, Simplify lists a new posting a median of
4.1 hours after the employer publishes it. This tracker records it a median of 17.1 hours
after. The digest is read once a day, so polling faster would not change what reaches the
user. The advantage is coverage.

Some smaller numbers:

- About 1,300 posting links appear on more than one source (of 7,059 across the snapshots
  on 2026-11-13). The overlap is deliberate, so a new role at a well-covered firm arrives
  as three or four changes on one morning. Merging them is harder than it sounds (see
  [one row per opportunity](#one-row-per-opportunity)).
- 81% of the most expensive run was reasoning tokens, not prompt size. Finding that took
  rebuilding the run from git history. The digest now prints it.
- Of the 1,181 rows the tracker reads from the biggest aggregator, none mention a class
  year (7 of 592 when first measured in September). Urgency therefore can't come from the
  row text and has to read the posting itself.
- A table row costs about 577 bytes, so the issue fits roughly 126 changes. Past that it
  trims its least urgent rows. The email, added on 2026-11-18, carries every row.

## Reflection

The tracker has not missed a morning, and it finds postings that a manual search would
not. Most of its better design decisions exist because something broke first, so this
section is mostly about those breaks.

### The day it delivered nothing

On 2026-09-14 the job reported 469 changes across about 480 rows whose text had not
changed. The digest came out longer than GitHub's 65,536-character limit, the issue API
returned 422, and because the crash came before the state commit there was no snapshot
either. The day lost both its digest and its record.

The cause was one aggregator's section headings, which carry a count:

```markdown
## Summer 2027 (300 employer-stated)
```

Row identities in the diff include the section heading on purpose, because two repos list
the same company under more than one heading. So the count in the heading was part of the
key of every row beneath it. One new posting anywhere in the section changed the count,
which changed every key, and all ~300 rows reported as removed and added at once.

Three fixes came out of it:

1. `stable_heading` strips a trailing parenthetical that starts with a digit. It is
   anchored on the digit, so a real qualifier like `Quantitative Finance (Advanced)` is
   kept.
2. The digest trims to fit instead of failing to send. The table is built in priority
   order, so dropping rows from the end removes the least urgent ones, and the digest says
   how many it dropped.
3. A repo that changes too much of itself at once is collapsed into one line. The ATS
   parsers had this guard from the start and the README parsers didn't, which is the only
   reason the bad day reached delivery at all.

The third fix matters most because it protects sources that haven't been added yet.
Before it, each repo's decorations (emoji, relative-age columns, "recently posted"
flames) were listed by hand, and that list only ever held what had already been noticed: one repo had three
markers configured and a fourth still churned 39 postings. The collapse guard doesn't
need to know what changed.

### Urgency took four versions

The top tier is only useful if it is short. Getting there took four versions.

The first put every relevant, confidently classified row in the top tier. That was 22
generic rows, and the tier below it was empty.

The second narrowed it to two regexes on the row text, and overshot. On 2026-09-13 the
block held 27 rows, 18 of them real opportunities, and none of the 18 was urgent. Both
versions read only the snapshot row (a title, a location, some links), while the deadline
sat in the posting body that `enrich` had already fetched and the model had already read.

The third made urgency read that evidence: a posting that says it reviews on a rolling
basis, or a close date within 21 days. That overshot too. By 2026-10-08, "rolling" had put
10 of the 18 High rows there, because most postings say it.

The fourth drops deadlines from ranking. The digest is read the morning a posting appears
and applications go out that day, so a close date doesn't change the order of work. Across
three digests before the change, 80 to 90% of rows read "Medium · no deadline stated".
The tier now reflects a first-year's odds. High is a discovery programme, a posting whose
class-year line names first-years, or a startup or small firm. Medium is size unknown,
then large employers that recruit through junior-year pipelines, then rows the classifier
never saw. Each row prints the reason for its tier, the deadline is still in the Notes
cell, and postings built for first- and second-years get their own block above the table.

### One row per opportunity

Because the watchlist overlaps, the same posting can arrive three or four times in one
morning. Grouping them showed that the text can't be trusted. One PIMCO role read "Software
Engineering Intern - Technology Analyst" on one aggregator and "2027 Summer Intern -
Technology Analyst, Software Engineering" on another. Worse, "Aquatic / QR" and
"Quantitative Researcher, Intern (Summer 2027)" were the same job with no words in common.

So identity comes from the link: a Greenhouse job number, a Workday requisition, a Lever
or Ashby UUID. Two rules keep it conservative. Only a posting link counts, never a company
page, because the company link sits next to the posting link and would have merged every
PIMCO row into one. And a row with no posting link is never merged, so plain page-text
rows always survive.

### The model needed room to think

The classifier started at `effort=minimal` to keep it cheap. On the first morning of the
rule that filters out generic software internships at large non-tech firms, it wrongly
ruled out Figma twice, Robinhood three times and Datadog once. Each time it had answered
an easier question ("is this firm quant?") instead of the one asked. Raising effort to
`low` and naming real companies in the prompt as examples fixed it.

That rule is the one place the project leans toward ruling things out, and it is safe only
because a ruled-out row is still printed with its reason, as a short line in RULED OUT
instead of a full table row. Every eligibility question still leans toward keeping,
because a false negative costs a real opportunity and a false positive costs a glance.

Later it stopped asking the same question twice. A judgment is now stored per posting
(employer plus title) and reused for later sightings, and a "large non-tech employer"
verdict is reused for that employer's generic titles. Replayed over 30 days, that avoided
36% of model calls.

### Limitations

1. Some sites won't be read. Citadel returns 403 to the tracker's User-Agent and 200 to a
   browser's. Spoofing it would take one line, but the tracker doesn't, because the only
   way through is to lie about what the client is. Those sources are marked `manual` and
   show up as monthly calendar reminders. Citadel's internship postings arrive through the
   speedyapply Quant lists, but Discover Citadel, a first-year programme, is still a blind
   spot.
2. Workday put its job sites behind a Cloudflare check around 2026-11-14. The check
   answered `httpx` with a "Security Check" page and answered Python's built-in `urllib`
   and `curl` normally, all with the same User-Agent. From the Actions runner, `httpx` got
   through on 8 of 27 boards and `urllib` on 27 of 27. Workday requests now go through
   `urllib`. If Workday starts refusing that too, those boards go to `manual` like
   Citadel. The tracker will not impersonate a browser to get past it.
3. Some pages render entirely in JavaScript and yield almost no text. Those are read in
   headless Chromium (16 watched pages, plus any posting that comes back as an empty
   shell) under the same User-Agent.
4. GitHub drops scheduled runs without telling anyone: no run, no log. The runs it does
   start are late, by 3.5 to 5 hours in September and 7 to 10 hours in November. The job
   is scheduled at midnight Pacific as headroom, and the code doesn't assume runs arrive
   on time or in order.
5. The user's interests are one paragraph passed to the model on every judgment. If it
   goes stale nothing fails. Every digest is quietly sorted against old interests and still looks
   fine. A review date next to it puts a reminder in the digest, which helps but doesn't
   solve it.
6. It is built for one person. The profile, the eligibility rules and the watchlist are
   all specific to one user, and generalising them would be a rewrite.

### In hindsight

The collapse guard should have come before the parsers rather than after the incident.
Any diff over a list someone else controls needs a limit on how much of it can change at
once.

The error page should be read before naming the cause. When the Workday boards started
failing with 429, it was taken as rate limiting and a retry-after-a-pause was built. The response
body said "Security Check" the whole time, and the retry was refused on every board.

## Resources

- [GitHub Actions docs](https://docs.github.com/en/actions) for scheduling, permissions
  and the state-commit pattern. Measured here, `schedule:` was less reliable than the docs
  suggest.
- [httpx](https://www.python-httpx.org/), and the API docs for Greenhouse, Lever, Ashby,
  Workday, Phenom and Eightfold. Each of those boards has a JSON endpoint behind the
  rendered page. A JavaScript shell usually has JSON behind it worth finding.
- [Anthropic's](https://docs.anthropic.com/) and
  [OpenAI's](https://platform.openai.com/docs) API docs for structured output. The
  classifier runs against either. Day to day it uses `gpt-5-mini` on Azure, paid for with
  student credit.
- [Resend](https://resend.com) for the email, sent from a domain verified by DNS records.
- Published lists of quant firms, for building the watchlist (see above).
- **AI Disclaimer:** I used Claude heavily on this project, considerably more than on the
  backtester, and it would be dishonest to present it otherwise. The design decisions are
  mine and I can defend every rule in here. Most of them exist because I read a bad digest
  and said "that's wrong, here's why." But a lot of the implementation was written with an
  agent, and the long-form documentation in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
  was drafted that way too. Where that showed up as a risk, it showed up the way it does
  for everyone: code that looked right, passed tests, and encoded an assumption nobody had
  checked. The 469-change morning was one of those. I kept the incident write-ups in the
  repo partly so that's on the record.

## Appendix

### Why git is the database

There's no database server. State lives in the repo as CSVs, and the job commits them back
every run, so `git log` shows which page changed on which morning, with no infrastructure.
Two commit messages tell the cases apart:

```
state: heartbeat 2026-09-06 (no upstream changes)   <- ran, nothing moved
state: 2026-09-07 upstream changes detected         <- something actually moved
```

`git log --oneline --grep="upstream changes"` lists the real change days, and the
heartbeat commits show the schedule is running. This has already paid off once. Working
out why one run cost 34 cents meant pulling the before and after snapshots out of two
state commits, re-running the diff to recover the exact 77 changes, and re-tokenising the
prompts. That was only possible because the state was in git.

The cost is that CSV writes have to be byte-stable. An unstable writer turns every run into
a whole-file diff. For the same reason the Excel exports are rebuilt only when a CSV
changes, because `openpyxl` output differs on every save and would put a binary diff in
every commit.

### A failure must never look like a quiet day

The design rests on this rule. Every source check returns a result object, and "the fetch
failed" and "the fetch worked and nothing changed" are reported differently. Zero rows
parsed is an alert. An HTTP 200 with the wrong shape is a failure too:
`{"error": "rate limited", "jobs": []}` must not read as an empty board.

The same rule sets the schedule. A digest arrives every day, even when nothing changed,
and a quiet day is just the title plus the calendar and health blocks. It used to mail
only on changes, which was quieter, but then a morning without email could mean either
"nothing happened" or "the job is broken". Now a missing digest always means the job is
broken.

It also explains why a quarantined source is still reported. The circuit breaker stops
fetching a source that keeps failing, but the source still appears in HEALTH, marked
SOURCE BLIND. A source the job has stopped checking is the one a user would be least
likely to notice otherwise.

### What an ATS is, and why it matters here

An applicant tracking system (Greenhouse, Workday, Ashby, Lever and others) is the
software a company's careers page runs on. The page is usually a thin layer over a JSON
feed, and the feed is reachable, often documented, and much more stable than the HTML.

That is why 99 of the sources are ATS rows rather than page text. They give structured
titles, locations, requisition IDs and often full descriptions instead of prose to diff.
It is also why "this careers page is a JavaScript shell" doesn't mean a site can't be
read. Susquehanna was on the manual list for that reason (its careers page yields 221
characters of text from 402KB of HTML) until `careers.sig.com/api/jobs` turned up, which
answers the tracker's ordinary User-Agent with 263 postings, descriptions included.

A weekly pass now does this automatically. It checks every watched page for known ATS
vendor domains and proposes what it finds. Matching on domains rather than keywords was
necessary: a first keyword-based version produced ten false positives out of
twenty-four hits, nine of them on the word "leverage".

### Discovery proposes, and never adds

Once a week a separate job looks for sources the tracker doesn't watch yet. It writes candidates
to a CSV and into the digest, and it never edits the live watchlist. Each candidate gets
one model call for a high or low priority and one sentence on why, and only an explicit
"low" is discarded.

A discarded candidate is still written down, under a status that marks it as the model's
call rather than the user's, so the row is both a record and a way back. The number discarded
goes in the digest, so a triage that threw away a whole week's findings can't pass for a
quiet week. A missing judgment is never treated as "low": no API key, a failed call, or any
answer other than exactly `low` keeps the candidate. A wrong discard is effectively
permanent, and a wrong keep costs one line.

The strongest test is whether a board was found inside an aggregator already watched,
because its postings arrive anyway. That one rule cut a re-judged batch from 18 proposals
to 5. It has one exception, quantitative finance, where the tracker reads every posting a
firm has rather than whichever ones an aggregator carried. Without the exception it would have
discarded Chicago Trading Company's Summer 2027 quant internships.

### The layering rule

There are eight levels, and a module may import only the levels at or below its own.

| Level | Package | Rule |
|:--|:--|:--|
| 0 | `core/` | Shapes and constants. No I/O of any kind |
| 1 | `persist/` | All storage, read and write. The only layer that may open a file for writing |
| 2 | `gather/` | Source network I/O, and whether to fetch at all |
| 3 | `process/` | Pure transformation. No network, no model, no writes |
| 4 | `enrich/` | The second network stage: posting bodies for changed rows only |
| 5 | `screen/` | Deterministic verdicts. May rule out, never in |
| 6 | `classify.py` | One model call per change the screen had no opinion on |
| 7 | `deliver/` | Every string the user sees is composed here, and the issue and email are sent from here |

Only `gather`, `enrich` and `deliver` may import `httpx`, and only `persist` may write a
file. [`tests/test_layering.py`](tests/test_layering.py) checks all three by parsing each
module's source rather than importing it. Importing runs the module's code and still
can't see an import made inside a function, which is where a violation tends to hide. The
test caught one that way during the refactor.

---

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python run.py all --dry-run        # fetch everything, print the digest, write nothing
.venv/bin/python run.py all --only nuft-2027 --dry-run
.venv/bin/python -m unittest discover -s tests
```

`--dry-run` still makes a real request to every source, so a full run takes a while. It
opens no issue, sends no email and writes no state, and the email it would have sent is
saved to `.run/digest.html`. Six tests skip without `ANTHROPIC_API_KEY` or
`AZURE_OPENAI_API_KEY`, which is expected. Neither key is needed to run the job. Without
one, every change reaches the digest unclassified rather than being dropped.

The email needs three repository secrets: `RESEND_API_KEY`, `DIGEST_EMAIL_FROM` and
`DIGEST_EMAIL_TO`. Without them the issue is the only delivery.

Requires Python 3.14. Dependencies: `httpx`, `openpyxl`, `anthropic`, `openai`,
`playwright`, `markdown`.
