# Internship Opportunity Tracker

## Introduction

<!-- Introduction: to be written by the owner -->

## Overview

A daily job that watches **250 sources, covering about 580 job boards and programme pages,** for quant, math and CS
internships, reads the postings that changed overnight, throws out the ones I can't
apply to, and mails me what's left as a single table. It has run every morning since
2026-09-05 and costs a median of about **8 cents a day**.

There is no server and no mailing list. The job runs on GitHub Actions and delivers by
opening a GitHub Issue, which means GitHub's own notification email is the delivery
mechanism — no SMTP, no mail credential to expire, and the issue history doubles as a
permanent archive of every digest.

```
Opportunity digest — 2026-09-20

## ■ FOR FRESHMEN & UNDERCLASSMEN (1)

| Company | Position                                      | Notes                                       |
|---------|-----------------------------------------------|---------------------------------------------|
| SIG     | Discovery Event: High School + First Year ... | • Year: first- and second-years             |

## ■ OPPORTUNITIES (5)

| Priority                           | Company       | Position                     | Notes                         |
|------------------------------------|---------------|------------------------------|-------------------------------|
| **High** · closes Oct 19 (11 days) | Manulife      | Spring Co-op 2027 – SWE      | • Year: any undergraduate     |
| Medium · rolling, no close date    | TikTok        | ML Engineer Intern           | • Location: San Jose, CA      |
| Medium · no deadline stated        | Fable         | Software Engineering Intern  | • Location: San Francisco, CA |
| …                                  |               |                              |                               |

<details><summary>■ RULED OUT (2)</summary>

- Axiom Space / Software Engineer Intern — juniors and above
- Waymo / 2027 Summer Intern, MS/PhD — graduate students only

</details>

## ■ HEALTH
- 144 sources checked · 144 healthy
- · job boards: 4117 postings were not student roles and 394 were outside the US
- classifier: 6 calls · 14,972 in · 2,950 out · ~$0.0071 est. (gpt-5-mini, effort=low)
```

That was a quiet Sunday — 5 opportunities and 6 classifier calls. A typical weekday is
35-65 calls and around 8 cents; see [Results](#results).

## Table of Contents
- [Overview](#overview)
- [How It Works](#how-it-works)
- [Results](#results)
- [Reflection](#reflection)
- [Resources](#resources)
- [Appendix](#appendix)

## Overview

This started because I got tired of finding out about things after they closed. As a
first-year, most of what I want is either a named underclassman programme (Jane Street
FTTP, IMC Launchpad, Akuna Academy) or a Summer 2027 posting sitting on some firm's job
board — and neither announces itself. Programmes open on no fixed schedule, quant firms
review on a rolling basis and fill before their stated deadline, and the aggregator
repos everyone uses are wide but late and thin on detail. Checking 150 pages by hand
every morning is not a thing a person does for more than four days.

So I wrote something that does it. The core idea is boring and turned out to be the
right one: **store a canonical snapshot of every source and diff it against yesterday.**
Everything interesting in this project is a consequence of that one decision going
wrong in specific ways, which is what most of the [Reflection](#reflection) is about.

What I picked up building it:

1. **Building a real pipeline**, and enforcing its shape mechanically rather than by
   good intentions — eight stages, each allowed to import only the ones below it,
   checked by walking every module's AST in a test.
2. **Designing for the failure you can't see.** The thing that kills a tool like this
   isn't a crash, it's a source that quietly returns zero rows for four months while you
   conclude nothing has opened.
3. **HTTP in anger** — `httpx`, async concurrency, circuit breakers, rate limits,
   User-Agent policy, `robots.txt`, and six different ATS vendor APIs.
4. **Parsing hostile input.** Markdown tables, HTML boards, JSON feeds, and a long tail
   of emoji, relative dates and section headings that churn for no reason.
5. **Using an LLM as one component rather than the whole program**, with a deterministic
   screen in front of it, a JSON schema on its output, and a degraded mode for when the
   API key is missing.
6. **Treating git as a database**, and writing byte-stable CSVs so `git log` stays
   readable as an audit trail.
7. **Cost accounting** — finding out that 81% of a bill was reasoning tokens, and then
   putting that number in the product so I'd never have to go find it again.
8. **Writing things down while they're still true**, which is most of
   [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

The files that matter most, roughly in the order I'd read them:

1. **[`run.py`](run.py)** — the entry point. Each of the eight stages is runnable on its
   own (`gather | process | enrich | screen | classify | render | deliver | all`), which
   is how you debug a morning that looks wrong.
2. **[`jobs/daily.py`](jobs/daily.py)** — composes the stages into the morning digest.
   Start here for the whole flow in one screen.
3. **[`process/parse_readme.py`](process/parse_readme.py)** — the noise problem, and the
   most interesting file in the repo. Everything in it is scar tissue from a specific
   morning.
4. **[`classify.py`](classify.py)** — the model call, its prompt, and the eight rules
   that prompt enforces.
5. **[`deliver/urgency.py`](deliver/urgency.py)** — what gets to be urgent, which I have
   now been wrong about in both directions.
6. **[`data/`](data/)** — the database: 351 programmes, 250 sources, the slug lists
   behind the three bulk board sources, and one snapshot file per source, all committed on
   every run.
7. **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — the long version: every failure
   mode, what it cost, and why the fix is shaped the way it is.

### What exactly is being watched?

A **source** is one of three things, and never a fourth:

- An **aggregator** — someone else's curated list, like the SimplifyJobs internship repo.
  Six of these. Wide, late, and thin on detail, but they catch firms I'd never think to
  look at.
- A **named employer's whole board** — every posting Jane Street or D. E. Shaw has,
  filtered down on my end. 79 of these run through an ATS API (Greenhouse, Workday,
  Ashby, Lever, Phenom, Eightfold); another ~37 are firms on no public ATS — Hudson
  River Trading, Millennium, Headlands, Renaissance — watched as plain page text.
- A **specific programme's own page** — Jane Street FTTP, IMC Launchpad, MIT Pokerbots.
  About 22 of these.

The fourth thing, which is never a source, is **a link to an individual posting**. Those
URLs expire inside the year, so a watcher pointed at one is guaranteed to rot quietly.
When I paste a posting link into the tool, it resolves *up* to the employer's board
before it will accept it.

That firm list came from published external lists rather than from memory, and that
turned out to matter a lot: an earlier sweep I built from recall missed WH Trading, GTS,
Bluefin, Eschaton, Kore, Vector, Freestone Grove, Laurion, Rokos, Quantbot, Trexquant,
Mako and BlackEdge — and missed the entire *category* of named first- and second-year
discovery programmes, which is the category I care about most.

## How It Works

Eight stages in a fixed order. Each one writes an artifact that the next one reads, so
any stage can be re-run on its own without redoing the one before it.

```
gather    →  fetch 242 sources                      ~11 min, the only slow stage
process   →  parse and diff against yesterday        0.7s, no network at all
enrich    →  fetch the posting body behind each      O(changes), not O(postings)
             row that actually moved
screen    →  rule out what a quoted phrase settles   deterministic, no model
classify  →  one model call per surviving change     ~$0.08 on a normal day
render    →  build the digest bytes
deliver   →  open the Issue, subject to one-a-day
```

Two things about that shape were deliberate and took a rewrite to get right.

**There are two network stages, not one.** "Fetch everything up front" isn't achievable
here, because *which* postings to fetch is only knowable after diffing — I pull bodies
for the handful of rows that moved, not for the ~4,000 sitting on the boards. `enrich`
used to hide inside the classifier, which made the model-call stage secretly an I/O
stage. Naming it fixed that.

**The stage boundary is verified, not assumed.** `run.py all` followed by `run.py render`
produces a byte-identical digest with no network call and no model call. That property
is why the health block renders from a stored metrics object rather than from live
results — an earlier version read module globals, so re-rendering in a fresh process
printed a confident summary of a run that had not happened.

### Two filters, and only one of them is a model

Nearly everything gets thrown away, and the order matters:

| Filter | Kind | What it does |
|:---|:---|:---|
| student / US screens | deterministic | Drop postings that aren't student roles or aren't in the US. On a typical day this is ~4,100 and ~400 rows. |
| `screen/rules.py` | deterministic | Rule out on a **quoted phrase** from the posting — "rising junior", "PhD candidates". May only rule *out*, never in. Removed 36% of model calls with zero wrong calls. |
| `classify.py` | one model call | Everything the screen had no opinion on. Returns a JSON judgment: relevant, class year, location, deadline, confidence. |

The screen is in front of the model purely because it's free and it's right. The rule I
hold to is that it **may only rule out, and only on a phrase it can quote** — no opinion
means the change goes to the model unchanged, and a missing posting body is never
grounds for a rule-out.

Everything either filter removes is **counted in the digest's HEALTH block**, and
anything the classifier rules out is still printed, with its reasoning, in a collapsed
RULED OUT section. A filter that hides silently is exactly how a source goes blind
without anyone noticing, and that has already happened twice here.

### The values you'd actually tune

| Name | Value | What it's for |
|:-----|:------|:--------------|
| `MAX_CHANGE_RATIO` | $0.25$ | An aggregator reporting more than 25% of its rows changed has restructured, not restocked. Collapse to one line instead of believing it. |
| `MIN_CHANGES_TO_COLLAPSE` | $25$ | Both conditions must trip, so a small repo legitimately turning over doesn't get collapsed. |
| `MAX_CHANGES_PER_BOARD` | $25$ | The same guard for ATS boards, flat rather than proportional — boards don't post 100 real roles overnight. |
| `QUARANTINE_AFTER_FAILURES` | $3$ | Three consecutive failures and the source backs off: $6 \to 12 \to 24 \to 48$ h, capped at $72$. |
| `FAILURE_ESCALATION_THRESHOLD` | $3$ | Three failures also escalates the source **into the High tier**, not into a footnote. |
| `MIN_TEXT_HTML_RATIO` | $0.0015$ | Text-to-HTML floor. Catches a JavaScript shell serving 221 characters out of 402KB. |
| `MIN_ABSOLUTE_CHARS` | $500$ | The other content floor. A page under this didn't really load. |
| `SHRINK_RATIO` | $0.4$ | A page that drops below 40% of its last good size is a redesign or a block, not a quiet day. Self-calibrating, so there's no per-source constant to rot. |
| `MAX_ISSUE_BODY_CHARS` | $65{,}536$ | GitHub's hard limit. Exceed it and the POST 422s and opens *nothing*. |
| `MAX_CLASSIFICATIONS_PER_RUN` | $250$ | Spend ceiling, counted after the deterministic screen. The worst day observed was 243 model calls (2026-10-03). |
| `CLASSIFY_REASONING_EFFORT` | `low` | Was `minimal`; see [Reflection](#the-model-needed-room-to-think). |
| `REQUEST_DELAY_SECONDS` | $1.0$ | Requests are serialised and identify themselves. Nothing is hammered. |

## Results

It works, and it has kept working, which for this kind of tool is the entire claim.

| | |
|:---|:---|
| Sources watched | **250** (242 fetched, 8 blocked and handled by hand); three of them read 343 small employers' job boards in bulk |
| Breakdown | 91 ATS boards · 135 watched pages · 12 aggregator repos · 3 bulk sources · the NSF REU feed · 8 manual |
| Read each morning | **~33,000** job-board postings, cut to the student roles and diffed against 106 board snapshots and 103 page snapshots |
| Programmes in the database | **351** |
| Digests delivered | **33 of 33**, one every day from 2026-09-06 to 2026-10-08 |
| Cost | **~$0.08/day** median over 23 digests (2026-09-16 to 2026-10-08), range $0.007-$0.25 |
| Tests | **339 passing** (6 skip without an API key) |
| Code | ~10,300 lines across 49 modules, plus ~5,500 lines of tests |
| Runtime | ~20 min on the Actions runner, almost all of it `gather` |

### Coverage against the largest public list

The question the project has to answer is whether watching 250 sources beats bookmarking
[Simplify](https://github.com/SimplifyJobs/Summer2027-Internships), the most-used internship
list. `process/coverage.py` answers it weekly and records it in
[`data/coverage.csv`](data/coverage.csv). Measured on 2026-10-08:

| | Tracker | Simplify | Tracker has, Simplify doesn't | Of those, on no public list |
|:---|---:|---:|---:|---:|
| Open US internships in quant, math, CS, data and ML | **1,640** | 1,526 | **571 (+37%)** | 125 |
| Quant only | **177** | 153 | **55 (+36%)** | 29 |

"On no public list" means the posting came from an employer's own job board and appears
on none of the aggregators the tracker reads, Simplify included.

How it is counted, and why the figure is a floor:

- **Simplify is measured from its whole `listings.json`**, every active posting whatever
  term it is filed under, not from the sections of its README the tracker happens to
  read. Comparing against the tracker's own snapshot of Simplify gave 2.3x; that was wrong.
- **Both sides are deduplicated the same way**: by the employer's posting link, and
  failing that by company plus most of the title words. The second rule merges some
  genuinely different roles, which can only lower the tracker's count.
- **Graduate-only and non-US postings are excluded on both sides.**
- The first baseline, on 2026-10-01, was +14%. The difference is the sources added on
  2026-10-01 and 10-02, after that baseline was taken, not a change of method. Sources
  added on 2026-10-08 are not yet in the figure.

### Selectivity

The digest is not a firehose. Across the 23 digests from 2026-09-16 to 2026-10-08:

- **59% of changed postings were ruled out before reaching the table** (1,462 of 2,459),
  on class year, field or employer, each with its reason printed in RULED OUT.
- **A median of 31 rows a day** reached the table.
- On the job boards, **about 90% of the postings read each morning are dropped as not
  student roles** before anything is diffed (30,002 of 33,122 on 2026-10-08).

### Speed is not the advantage

Measured on Greenhouse postings on 2026-10-01, Simplify lists a new posting a median of **4.1h** after the
employer publishes it; this tracker's daily run records it a median of **17.1h** after.
A daily digest is read once a day, so polling faster would not change what reaches the
reader. The advantage is coverage, and the numbers above are the claim.

A few numbers I find more interesting than the headline ones:

- **127 postings sit on more than one source.** The watchlist overlaps on purpose, so a
  new role at a well-covered firm arrives as three or four changes on one morning.
  Deduping them is harder than it sounds and is discussed [below](#one-row-per-opportunity).
- **81% of my worst bill was reasoning tokens**, not prompt size. Finding that out took
  reconstructing a run from git history; the digest now just says it.
- **7 of 592 rows on the biggest aggregator mention a class year at all.** Which is why
  urgency can't be decided from the row text, and has to read the posting body.
- **The digest ceiling is about 126 changes.** A table row costs 577 bytes, a ruled-out
  line 226, the footers 6,789. Past that the body trims itself rather than failing to
  send — see [the day it didn't](#the-day-it-delivered-nothing).

## Reflection

I'm happy with this. It does the thing I built it to do, it has not missed a morning,
and it caught postings I would not have found. But almost everything I'd point at as
good in it exists because something went wrong first, so this section is mostly that.

### The day it delivered nothing

On 2026-09-14 the job reported **469 changes** across roughly 480 rows whose text had
not moved, produced a digest past GitHub's 65,536-character issue limit, got a `422`
back, and — because the crash came before the state commit — left no snapshot either. So
the day lost its digest *and* its record.

The cause is my favourite bug in the project. One of the aggregator repos writes its
section headings like this:

```markdown
## Summer 2027 (300 employer-stated)
```

Row identities in my diff are **section-qualified**, deliberately — two repos list the
same company under more than one heading, so the section has to be part of the key. But
that means the tally in the heading is part of the diff key of every row beneath it. One
new posting anywhere in the section renumbers the heading, every row's key changes, and
all ~300 report as a `removed` and an `added` at once.

Three fixes came out of it, and the third is the one that matters:

1. `stable_heading` strips a trailing parenthetical that starts with a digit — anchored
   on the digit, so a real qualifier like `Quantitative Finance (Advanced)` survives.
2. The digest now **trims to fit** rather than failing to send, dropping the least urgent
   rows (the table is built in priority order precisely so trimming from the end is safe)
   and naming the count.
3. **A repo that moves too much of itself at once is collapsed, not believed.** The ATS
   parsers had had this guard since day one; the README parsers didn't, which is the only
   reason the bad day got as far as delivery. This is the guard that means a *newly added*
   source can't repeat the incident whether or not I've noticed its quirks yet.

That third point is the actual lesson. I'd spent a while enumerating each repo's
decorations by hand — emoji, relative-age columns, "recently posted" flames — and the
list can only ever hold what someone already noticed. One repo had three markers
configured and a fourth still churned 39 postings. Enumerating specific problems doesn't
scale; a guard that doesn't need to know what the problem was does.

### I was wrong about urgency, three times

The top tier only works if it's short. Getting there took three failures:

- **First attempt: "relevant and confidently classified."** That put 22 generic rows in
  ACT NOW and emptied the section below it. Useless.
- **Second attempt: narrow it to two regexes** on the row text. Overshot badly — on
  2026-09-13 the block held 27 rows, 18 of them real opportunities, and **not one of the
  18 was urgent.** Only the nine blind sources were genuinely there.

Both versions read the *snapshot row* — a title, a location, some links — while the
deadline was sitting in the posting body that `enrich` had already fetched and the model
had already read. The fix was to make urgency ask the evidence: a posting that says it
reviews on a rolling basis, or a stated close date inside 21 days.

That overshot too. By 2026-10-08, "the posting says rolling" was what put 10 of the 18
High rows there, because most postings say it. The tiers were renamed High and Medium, and
every row prints the reason it got its tier, so a wrong call is visible on the row.
Postings built for first-years have their own block above the table.

The fourth version dropped deadlines from ranking altogether. The digest is read the
morning a posting appears and applied to that day, so a close date sorts nothing: across
three digests 80-90% of rows read "Medium · no deadline stated". The tier is now the
reader's odds as a first-year. High means a discovery programme, a posting whose class-year
line names first-years even inside a wider list, or a startup or small firm. Medium means
size unknown, then large employers that recruit through junior-year pipelines, then rows the
classifier never saw. The deadline is still printed in the Notes cell. On the same three
digests High held 4-28 rows (18-29%).

The rule I settled on is that **every test for urgency must be a dated reason, never a
quality judgment.** "This is a great opportunity" is not a reason to put something at the
top; "this closes in nine days" is. A deadline the code can't parse never promotes a row
but is still printed, so I can read it myself.

### One row per opportunity

Because the watchlist overlaps, the same posting shows up three or four times on a good
morning. Grouping them is where I learned not to trust text. One PIMCO role read
"Software Engineering Intern - Technology Analyst" on one aggregator and "2027 Summer
Intern - Technology Analyst, Software Engineering" on another; the starkest case was
"Aquatic / QR" against "Quantitative Researcher, Intern (Summer 2027)" — the same job
with no shared words.

So the identity comes from **the link, not the title**: a Greenhouse job number, a
Workday requisition, a Lever or Ashby UUID. Two properties keep it conservative, and
both are load-bearing. Only a *posting* link dedupes, never a company page — the
company link sits right next to the posting link and would have collapsed every PIMCO
row into one. And **no identity means no dedupe**, so plain-text page rows always
survive. I'd rather show a duplicate than eat a real posting.

### The model needed room to think

I started the relevance classifier at `effort=minimal` to keep it cheap. On the first
morning of the rule that filters out generic software internships, it wrongly ruled out
Figma twice, Robinhood three times and Datadog once — every time by quietly substituting
an easier question ("is this firm *quant*?") for the one I asked ("is this role worth a
morning?"). Raising it to `low` and naming real companies in the prompt as calibration
fixed it.

That rule is also the one place the project is deliberately decisive rather than
inclusive, and it's only safe because **a ruled-out row is still printed with its
reasoning.** Nothing is hidden; it just moves out of the list of things to go and do, and
a 577-byte table row becomes a 226-byte audit line. Every *eligibility* question still
errs toward keeping, because a false negative costs a real opportunity and a false
positive costs a glance.

### Limitations

1. **Some sites just won't be read.** Citadel returns 403 to my User-Agent and HTTP 200
   to a browser string. I could spoof it in one line. I don't, because the only route
   through is to lie about who the client is, and this tool fetches under an honest
   User-Agent. Those sources are marked `manual` and surface as monthly calendar
   reminders instead. Citadel's internship postings now arrive through the speedyapply
   Quant lists, but Discover Citadel, a programme I'm actually eligible for, is still a
   blind spot. That's a real cost of the choice.
2. **Some pages render entirely in JavaScript** and yield almost no text. Those are now
   read in headless Chromium — five watched pages marked `render_js=true`, and any
   posting page that comes back as a shell — under the same honest User-Agent.
3. **GitHub silently drops scheduled cron ticks** — not delays, *drops*, with no run and
   no log. And the ticks it does run, it ran 3h27m–4h48m late every time I measured. I
   schedule at midnight Pacific purely as headroom, which is why the code doesn't assume
   an exact run time or even that ticks arrive in order.
4. **The owner profile is perishable.** My interests are a single paragraph passed to the
   model on every judgment, and a stale one doesn't fail loudly — it quietly mis-sorts
   every item in every digest while the output still looks perfectly well-formed. There's
   a review date next to it that nags me in the digest, which is a backstop and not a
   solution.
5. **It is built for exactly one person.** The profile, the eligibility rules and the
   watchlist are all mine. Generalising it is a rewrite, not a config file.

### What I'd do differently

Ask the "is this employer interesting" question **once per employer** instead of once per
posting — Figma turned up four times in one digest and the answer can't differ between
them. And I'd write the collapse guard before the parsers rather than after the incident;
it was obvious in hindsight that any diff over a list I don't control needs a sanity
bound on how much of it can move at once.

## Resources

- **[GitHub Actions docs](https://docs.github.com/en/actions)** for the scheduling,
  permissions and state-commit patterns — with the large caveat that the docs describe
  `schedule:` as more reliable than I measured it to be.
- **[httpx](https://www.python-httpx.org/)** for async HTTP, and the published API docs
  for Greenhouse, Lever, Ashby, Workday, Phenom and Eightfold. Every one of those boards
  has a documented or easily-observed JSON endpoint behind the rendered page, which is
  the single highest-leverage thing I learned about scraping job boards: **a JavaScript
  shell is a reason to go looking for the JSON behind it, not a reason to give up.**
- **[Anthropic's](https://docs.anthropic.com/) and
  [OpenAI's](https://platform.openai.com/docs) API docs** for structured output. The
  classifier runs against either backend; it uses `gpt-5-mini` on Azure day to day
  because that's funded by student credit.
- **Published lists of quant firms** rather than my own recall, for building the
  watchlist. Covered above — this was not a small difference.
- **AI Disclaimer:** I used Claude heavily on this project, considerably more than on the
  backtester, and it would be dishonest to present it otherwise. The design decisions are
  mine and I can defend every rule in here — most of them exist because I read a bad
  digest and said "that's wrong, here's why." But a lot of the implementation was written
  with an agent, and the long-form documentation in
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) was drafted that way too. Where that shows
  up as a risk, it showed up the same way it does for everyone: code that looked right,
  passed tests, and encoded an assumption nobody had checked. The 469-change morning was
  one of those. I kept the incident write-ups in the repo partly so that's on the record.

## Appendix

### Why git is the database

There's no Postgres here. State lives in the repo as CSVs and the job commits them back
every run, which means `git log` answers "which page changed on which morning" forever,
for free, with no infrastructure. Two commit shapes distinguish the cases:

```
state: heartbeat 2026-09-06 (no upstream changes)   <- ran, nothing moved
state: 2026-09-07 upstream changes detected         <- something actually moved
```

So `git log --oneline --grep="upstream changes"` is the list of real change days, and the
heartbeat commits are independent evidence the schedule is alive. This paid for itself
once already: working out why one run cost 34 cents meant pulling the before/after
snapshot trees out of two state commits, re-running the diff to recover the exact 77
changes, and re-tokenising the reassembled prompts. That was only answerable *because*
git is the database.

The constraint it imposes is that **CSV writes must be byte-stable.** An unstable writer
makes every run a whole-file diff and destroys the audit trail — which is also why the
Excel exports are rebuilt only when the CSVs actually change, since `openpyxl` output
isn't byte-stable and would put a binary diff in every single commit.

### A failure must never look like a quiet day

This is the rule the whole design hangs off. Every source check returns a result object,
and `ok=False` and `ok=True, changes=[]` are different facts reported differently. Zero
rows parsed is an **alert**, not a quiet day. An HTTP 200 whose *shape* is wrong is also
a failure: `{"error": "rate limited", "jobs": []}` must never read as an empty board.

The same logic drives the delivery cadence. The digest arrives **exactly once a day,
every day**, even when nothing changed — a quiet day is a title plus the calendar and
health blocks. It used to mail only on changes, which was quieter but meant a morning
with no email was either "nothing happened" or "the job is broken" and I couldn't tell
which. Now **silence always means broken.**

And it drives the circuit breaker's one strange property: a quarantined source still
appears in HEALTH and still escalates into the High tier. "We have stopped looking" is the
strongest possible form of blind-not-quiet, and it's the one a reader would most readily
assume hadn't happened.

### What an ATS is, and why it matters here

An Applicant Tracking System is the software a company's careers page is actually built
on — Greenhouse, Workday, Ashby, Lever and friends. The page you see is usually a thin
rendering layer over a JSON feed, and the feed is generally reachable, documented, and
far more stable than the HTML.

That's why 79 of my sources are ATS rows rather than page-text rows: I get structured
titles, locations, requisition IDs and often full descriptions, instead of diffing prose.
It's also why "this firm's careers page is a JavaScript shell" is a *weak* conclusion.
Susquehanna sat in my manual pile for exactly that reason — its careers page yields 221
characters of text out of 402KB of HTML — until I found `careers.sig.com/api/jobs`, which
answers my ordinary User-Agent with 263 postings, descriptions and a recruiting category.

I now do that automatically: a weekly pass fingerprints every watched page against known
vendor **domains** and proposes what it finds. Anchoring on domains rather than keywords
was not optional — the bare-keyword version I wrote first produced ten false positives
out of twenty-four hits, nine of which matched only on the word "leverage."

### Discovery proposes, and never adds

Once a week a separate job goes looking for sources I'm not watching yet. It writes
candidates to a CSV and into the digest, and it **never** edits the live watchlist. Each
candidate gets one model call for a high/low priority call and one sentence of
justification, and only an explicit "low" is discarded.

Three properties hold that together. A discarded candidate is still *written down*, under
a status that distinguishes "I judged this" from "something judged it for me" — so the
row is both the tombstone and the way back. The discarded **count** goes in the digest,
because a triage that quietly ate a whole week's findings would be indistinguishable from
a quiet week. And a *missing* judgment is never treated as a "low": no API key, a failed
call, or any answer that isn't literally `low` all keep the candidate. A wrong discard is
permanent in effect; a wrong keep costs one line I skim.

The sharpest single test is that **a board mined out of an aggregator I already watch is
redundant** — its rows reach me every morning anyway. That one rule took a re-judged
batch from 18 proposals down to 5. It has exactly one override, quantitative finance,
where I want every posting a firm has rather than whichever ones an aggregator happened
to carry; without the override it throws away Chicago Trading Company's Summer 2027 quant
internships, which is precisely the kind of thing I built this for.

### The layering rule

Eight levels, and a module may import only the levels at or below its own.

| Level | Package | Rule |
|:--|:--|:--|
| 0 | `core/` | Shapes and constants. No I/O of any kind. |
| 1 | `persist/` | **All** storage, read and write. Only this layer may open a file for writing. |
| 2 | `gather/` | Source network I/O, and whether to fetch at all. |
| 3 | `process/` | Pure transformation. No network, no model, no writes. |
| 4 | `enrich/` | The second network stage. Posting bodies, for changed rows only. |
| 5 | `screen/` | Deterministic verdicts. May rule **out**, never in. |
| 6 | `classify.py` | One model call per change the screen had no opinion on. |
| 7 | `deliver/` | Every string I actually read is composed here. |

Only `gather`, `enrich` and `deliver` may import `httpx`; only `persist` may write a
file. All three rules are checked by
[`tests/test_layering.py`](tests/test_layering.py), which walks every module's **AST**
rather than its imports — because importing a module executes its body, and still can't
see a function-local import, which is exactly where a violation hides. It caught one that
way during the refactor.

---

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python run.py all --dry-run        # fetch everything, print the digest, write nothing
.venv/bin/python run.py all --only nuft-2027 --dry-run
.venv/bin/python -m unittest discover -s tests
```

`--dry-run` still makes real requests to every source (~4 min), but opens no Issue and
writes no state. Six tests skip without `ANTHROPIC_API_KEY` or `AZURE_OPENAI_API_KEY`;
that's expected. Neither key is required to run the job — without them every change is
surfaced *unclassified* rather than dropped, because degraded mode should surface more,
not less.

Requires Python 3.14. Dependencies: `httpx`, `openpyxl`, `anthropic`, `openai`.
