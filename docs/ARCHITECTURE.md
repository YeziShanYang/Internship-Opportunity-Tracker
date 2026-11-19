# Architecture and engineering notes

> This is the deep document: how the pipeline is put together, which failure modes it
> was built against, and what each of them cost before it was fixed. For what the
> project *is* and how to run it, start at the [README](../README.md).

A daily job that watches a fixed list of sources for quant / math / CS opportunities
relevant to one specific person — a Stanford first-year, class of 2030 — and delivers a
digest each morning twice: as a GitHub Issue, and as an HTML email carrying every row.

Runs on GitHub Actions, so it does not care whether your laptop is awake. State lives in
this repo as CSVs, which makes **git the database**: `git log` answers "which page
changed on which morning" forever, for free.

**Status: Phases 1 and 2 shipped.** 250 sources — 12 GitHub repo trackers, 91 ATS
boards (Greenhouse 59, Workday 21, Ashby 7, Lever 2, Phenom 1, Eightfold 1), three bulk
sources reading 343 small employers' boards, the NSF awards feed, 135 watched pages
(10 rendered in headless Chromium, `render_js=true`) and 8 `manual` rows — with
Issue delivery, one digest a day and a weekly source-discovery pass. 242 of the 250 are
fetched; the `manual` eight are sites that block automation or the Actions runner and
surface as calendar reminders instead.

Sources divide by *intake path*, not by tier: aggregator lists that someone else
curates, and named employers. For a named employer the watcher points either at the
whole jobs board or at a specific programme's own page — never at an individual
posting, since those URLs expire inside the year.

---

## Setup (once)

1. Create a GitHub repo and push this code. It works the same public or private;
   note that a **private** repo on a free account sits in the lowest Actions scheduler
   priority tier, which is the source of the lateness described in failure mode 3.
2. Add repo secrets (Settings → Secrets and variables → Actions):
   - `GH_PAT` — a fine-grained PAT, **read-only, public repositories**. Lifts the GitHub
     API limit from 60 requests/hour to 5,000. Optional but recommended.
   - `ANTHROPIC_API_KEY` — for the relevance classifier, running Claude. **Optional.**
   - `AZURE_OPENAI_API_KEY` — the alternative classifier backend: a `gpt-5-mini`
     deployment on a Microsoft Foundry resource, funded by Azure for Students credit.
     **Optional.** Used only when `ANTHROPIC_API_KEY` is absent.
   - Either key is enough, and neither is required. Without both, the job still runs and
     still reports everything; changes simply arrive unclassified rather than being
     dropped. Set `CLASSIFIER_PROVIDER` to `anthropic` or `azure` to force one.
   - `GITHUB_TOKEN` is provided automatically.
   - `RESEND_API_KEY`, `DIGEST_EMAIL_FROM`, `DIGEST_EMAIL_TO` — the full digest by
     email (`deliver/mail.py`). **Optional**: with any one unset, the email is skipped
     and the issue delivers alone. The sending domain is verified in Resend by DNS
     records; the key needs only sending access to that domain. Test the settings with
     `run.py render` then `run.py deliver --email-only`.
3. Enable Actions and confirm `daily` appears in the Actions tab.
4. Run it once manually: Actions → `daily` → **Run workflow**. It should open an Issue.
5. **Set a recurring monthly calendar reminder: "check opportunity-tracker Actions tab."**
   This one matters — see [Failure modes](#failure-modes) below.

After that it is unattended.

## Running it locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python run.py all --dry-run    # fetch everything, print the digest, change nothing
.venv/bin/python run.py all --only nuft-2027 --dry-run
.venv/bin/python build_xlsx.py           # regenerate out/programs.xlsx
.venv/bin/python -m unittest discover -s tests -v
```

`--dry-run` opens no Issue and writes no state. Use it freely.

### One stage at a time

`run.py all` runs every stage in one process, which is what the Actions job does. Each
stage is also runnable on its own, reading the previous stage's artifact out of `.run/`
instead of recomputing it:

```bash
.venv/bin/python run.py gather     # fetch all 242 sources into .run/raw/. The only slow one
.venv/bin/python run.py process    # parse and diff .run/raw/. No network at all
.venv/bin/python run.py enrich     # fetch the posting behind each changed row
.venv/bin/python run.py screen     # rule out what a quoted phrase settles. No model
.venv/bin/python run.py classify   # the model calls, and only the model calls
.venv/bin/python run.py render     # rebuild the digest. No network, no model
.venv/bin/python run.py deliver    # post it, subject to the one-a-day cap
```

This is for the mornings when a digest looks wrong and the question is *which stage
produced it*. `run.py render` rebuilds the exact bytes that would be posted from
`.run/` alone, and `run.py process` can be re-run over one `gather` as many times as
you like — it makes no requests and produces byte-identical output.

`.run/` is gitignored: it is ~43MB of fetched bodies plus the JSON handoff, all
re-fetchable, and committing it would bury the CSV history that `git log` exists to
answer.

## How it works

Eight stages, in a fixed order, each allowed to import only the ones below it. The
rule is checked mechanically by `tests/test_layering.py`, which walks every module's
AST — a rule that lives only in a document is how the previous shape of this code ended
up with one function doing fourteen things including a disk read and a markdown render.

```
run.py                   CLI: gather | process | enrich | screen | classify | render
                              | deliver | all

core/       (level 0)    shapes and constants. No I/O of any kind
  models.py              Change, SourceResult, Judgment, Usage, the artifact payloads
  paths.py               every path, the User-Agent, timeouts, column lists
  clock.py               time, hashing, change_id (the join key) and change_key (mute)
  profile.py             OWNER_PROFILE — perishable input, with its review date
  codec.py               the ~70-line JSON codec for the stage artifacts
  text.py                HTML → readable text, and the classifier's text budget

persist/    (level 1)    ALL storage, read AND write. Below gather, not last
  store.py               every read_*/write_* of the canonical CSVs and snapshots
  artifacts.py           .run/* — the stage handoff
  cache.py               the fetched-posting cache

gather/     (level 2)    network I/O for sources, and whether to fetch at all
  collect.py             the stage: fetch every source, write the bodies
  clients.py             the two HTTP clients. Two, because it is a credential boundary
  breaker.py             the circuit breaker — fetch policy, so it lives here
  github_readme.py       fetch a README
  ats.py                 fetch an ATS feed (greenhouse/lever/ashby/workday/phenom/eightfold)
  page.py                fetch a page, recording the FINAL url

process/    (level 3)    pure transformation. No network, no model, no writes
  build.py               the stage: read .run/raw/, assess every source
  snapshot.py            Row/Snapshot/diff — the shape every source type shares
  parse_readme.py        repo READMEs, diffed at the row level
  parse_ats.py           ATS JSON → canonical rows, and the student/US screens
  parse_page.py          normalise a page to lines and diff; two floors, no browser
  redirect.py            where a fetch landed is part of whether it succeeded
  suppress.py            the muted and applied.tsv filters

enrich/     (level 4)    the SECOND network stage, named rather than hidden
  bodies.py              fetch the posting behind each CHANGED row. O(changes)
  postings.py            the fetcher and its content floor

screen/     (level 5)    deterministic verdicts only. May rule OUT, never IN
  rules.py               the phrase patterns, and the guards that stop false positives
  verdicts.py            the loop, and the FilterReport it owes HEALTH

classify.py (level 6)    one model call per change the screen had no opinion on

deliver/    (level 7)    every string the owner reads is composed here
  digest.py              the one-table body
  health.py              the HEALTH block: sources, filters, spend
  urgency.py             High vs Medium, the reason, the underclassman block
  issue.py               the cadence decision and the Issue POST

jobs/                    compose stages into something runnable. Not stages themselves
  daily.py               the morning digest
  discovery.py           the Monday pass that proposes new sources and never adds one,
                         triaged high/low priority with one sentence on each survivor

calendar_reminders.py    standing reminders for what cannot be automated
build_xlsx.py            CSVs → out/programs.xlsx (yours) + out/tracked.xlsx (the tool's)
seed_programs.py         one-time seed from the original spreadsheet
add_opportunity.py       helper behind the /add-opportunity skill
data/programs.csv        CANONICAL program list — edit this, not the xlsx
data/sources.csv         what to check, and how
data/manual.csv          what this tool CANNOT tell you about → "Manual Watch" sheet
data/priority.csv        ranked shortlist to watch yourself → "Priority" sheet
data/snapshots/*.tsv     one canonical line per listing row, committed
data/proposals.log       append-only: proposed edits the tool refused to make itself
out/programs.xlsx        GENERATED — yours: only what you must check by hand
out/tracked.xlsx         GENERATED — the tool's: all programs, sources, applied, discovered
```

### Why two network stages and not one

"Gather everything up front" is not achievable here, and pretending otherwise would be
the dishonest kind of tidy. Which postings to fetch is only knowable *after* diffing:
bodies are fetched for the handful of rows that moved, not for the ~4,000 sitting on the
boards. So there are exactly two source-network stages — `gather` and `enrich` — and the
pipeline says so out loud rather than hiding the second one inside the classifier, which
is where it used to be.

### Why the snapshots are TSV, not the READMEs

The stored snapshot is a sorted, canonical `section → key → value` rendering rather than
the README itself. A newly posted role is then exactly **one added line** in `git diff`,
instead of a 700KB blob reflowing. It also means the diff is keyed on company + role, so
badge churn and emoji edits cannot produce a false positive.

Each repo needs its own parser config, because the six READMEs share almost nothing:
NUFT puts the company in the `##` heading above a two-column table; Cruz-Lopez has seven
different header layouts; Simplify uses HTML `<table>` and a **relative** `Age` column
(`1d`, `2mo`) that is excluded from the diff, because diffing it would report all 490
rows as changed every single day.

Simplify also needs two markers neutralised: a bare `↳` in the Company cell meaning
"same company as above" (resolved into both the key *and* the value, so a row shifting
position within its group does not churn), and `🔥` meaning "recently posted", which
falls off after a few days and would otherwise make a posting look removed and re-added.
Cruz-Lopez uses `🔥` differently — as `Status=🔥 [CLOSING SOON]`, which is real signal —
so the strip is scoped per repo, not global.

One more value churns independently of any posting, and it is the most damaging of them
because it is not in a cell at all: a **section heading that carries its own row tally**.
zshah-2027 writes `## Summer 2027 (300 employer-stated)`, and `Row.identity` is
section-qualified — deliberately, since two repos list the same company under more than
one heading — so the count is part of the diff key of every row beneath it. One posting
appearing anywhere in the section renumbers the heading, and all ~300 rows report as a
`removed` and an `added` at once. On 2026-09-14 two of that repo's three counts ticked
overnight, the run reported **469 changes** over rows whose own text had not moved, and
then delivered nothing at all: the digest exceeded GitHub's 65,536-character issue body
limit, the POST failed `422`, and because the crash came before the state commit the day
left no snapshot either. `parse_readme.stable_heading` strips a trailing parenthetical
that begins with a digit, anchored on the digit so a heading whose name really does end
in parentheses — `Quantitative Finance (Advanced)` — is left alone.

Enumerating a repo's decorations by hand is itself the weakness: zshah-2027 already had
three markers configured and a fourth still churned 39 postings, because the list can
only hold what somebody already noticed. So `_key_text` strips emoji and pictographs
from **the key** generically, for every repo including ones not added yet, and never
from the value — the value is the record of what the row said. Arrows are excluded
deliberately (`↳` is the carry-forward marker and is resolved, not dropped), and the
rule is written as character ranges rather than "non-ASCII" because `Société Générale`
is identity, not decoration.

Behind all of it is a guard that does not need to know what the decoration was. A repo
that reports more than `MAX_CHANGE_RATIO` (25%) of its rows changed in one run, and at
least `MIN_CHANGES_TO_COLLAPSE` (25) of them, has restructured rather than restocked:
the changes collapse into a single item and the count goes to HEALTH. Both conditions
are required, and the ratio is measured against the larger of the two snapshots so a
repo that *empties* trips the same rule as one that doubles. It is proportional, unlike
the flat count `parse_ats` uses, because an aggregator legitimately posts dozens of real
rows on a busy morning — simplify-2027 moved 38 of 625 on the morning zshah-2027 moved
480 of 514. A collapse still writes its snapshot, so the run re-baselines and tomorrow
diffs against today rather than replaying the storm.

`tests/test_acceptance.py::NoiseRegressionTests` and `RepoCollapseTests` lock all of
these behaviours down.

### Reading the git log

Every run commits, because `last_checked` and `last_success` advance whether or not
anything moved. The message tells you which kind it was:

```
state: heartbeat 2026-09-06 (no upstream changes)   <- the job ran, nothing changed
state: 2026-09-06 upstream changes detected         <- something actually moved
```

So `git log --oneline --grep="upstream changes"` is the list of real change days, and the
heartbeat commits are independent evidence the schedule is still alive.

### Cadence

**Exactly one digest a day, every day** — no more and no less.

This replaced a changes-only cadence on 2026-09-08. That version kept the inbox quieter,
but a morning with no email meant either "nothing changed" or "the job is broken" and you
could not tell which without opening the Actions tab. A fixed daily arrival removes the
ambiguity: **silence now always means broken.** Notification fatigue is handled in the
content instead of by withholding mail — a quiet day is titled `(no changes)` and its body
is just the calendar and health blocks, which you can triage without opening it.

The **no more** half matters just as much. A scheduled tick and a hand-dispatched run
can overlap (see failure mode 3) and each would otherwise open its own issue; two digests
for one date is the same fatigue failure as an unread daily. So delivery is capped at one
issue per date, and the cap is **absolute** — it suppresses real changes and failing sources too,
because a retry has nothing to tell you that the morning's issue did not already carry.
Nothing is lost either way: the state commit and `data/proposals.log` still record
everything the retry saw.

The cap cannot rest on `data/last_delivered.txt` alone, because a run reads that marker
from the commit it checked out and late ticks do not always dispatch in cron order — so a
tick can see a stale marker and mail a duplicate. The authoritative check is the issue
list itself: the issues **are** the delivery log, and every run sees the same one whatever
it checked out. The email is sent only by the run that opened the issue, so the cap
covers it too, and a failed email is reported as a comment on that issue. The marker is the fallback for when GitHub
cannot be reached.

### What the classifier will not do

It never rewrites the `eligible` column. That column is hand-verified research, and a
model overwriting it destroys work. Proposed changes go to `data/proposals.log` and into
the digest, for you to apply or ignore.

## Adding opportunities from an article

There is a Claude Code skill for this: paste a link to an article and it opens the page,
extracts every opportunity in it, probes each one to see whether it can actually be
monitored, and files it into `programs.csv` plus the right watchlist.

```
.claude/skills/add-opportunity/SKILL.md
```

The same file is copied one level up, in the workspace's `.claude/skills/`, so the skill
is found whether your working directory is this repo or its parent. **If you edit one,
copy it over the other.**

It leans on `add_opportunity.py`, which is also useful directly:

```bash
.venv/bin/python add_opportunity.py --probe "https://some-program"   # can this be watched?
.venv/bin/python add_opportunity.py --add opps.json --dry-run        # validate, write nothing
```

Three guardrails, because an agent drives it: additions are append-only and deduplicated
by name, so hand-verified rows cannot be clobbered; `eligible: "YES"` is **rejected**
(the agent must use `CHECK` and let you confirm); and a `robots.txt` disallow or a 403
routes an opportunity to Manual Watch rather than being worked around.

## Failure modes

1. **Silent success** — a page redesigns, the fetch returns nothing, the job exits 0, and
   you conclude nothing has opened for four months. Mitigated: every source returns a
   result object; "returned zero rows" and "fetch failed" are first-class alerts;
   `consecutive_failures` and `last_success` are tracked per source; **three consecutive
   failures marks the source SOURCE BLIND** in HEALTH, with a blind count on the summary
   line. It was a High row at the top of the opportunities table until 2026-11-19, when
   the table was narrowed to things to apply to. Each source also
   has a plausibility floor — and for NUFT that floor is on *section* count, not row
   count, because its row count legitimately drains to near zero out of season.
2. **GitHub disables scheduled workflows on inactive repos.** This is the classic way
   projects like this die around month six. Whether the bot's own state commits reset that
   clock is not worth relying on. **Mitigation: the monthly calendar reminder in step 5.**
3. **Cron is best-effort — and worse than "delayed".** GitHub does not merely postpone a
   scheduled tick when it is busy, it silently drops it, and there is no run, no log and
   no notification to tell you so. This was observed live: the first two days of the
   schedule produced *zero* scheduled runs while `workflow_dispatch` worked perfectly.
   **Mitigation, 2026-09-07 to 2026-09-15: three ticks each morning (07:00 / 08:20 /
   09:40 UTC) with delivery capped at one issue per date.** Cut back to a single 07:00
   tick on 2026-09-15 once the history had an answer: across the eight days the retries
   were live every tick fired, 24 of 24, and the first tick delivered on every one of
   them, so the 16 retries delivered nothing. A retry only rescues a *dropped* tick; when
   the first tick fails inside the code the retries fail with it, as on 2026-09-14 and
   2026-09-15. The drop is still real, and what makes one tick acceptable is that a
   missing digest is now unambiguous -- the cadence mails every day, so silence means
   broken -- with `workflow_dispatch` to recover the morning by hand.

   The ticks GitHub *does* run, it runs badly late. Measured 2026-09-07/08: every tick
   ran 3h27m–4h48m behind its cron, because a private repo on a free personal account
   sits in the lowest scheduler priority tier. A 13:30 UTC cron aimed at 06:30 Pacific
   opened its issue at 10:18 Pacific. No cron value fixes that — the delay is unbounded.
   **Mitigation: schedule with headroom instead of on target.** The job runs at midnight
   Pacific, so even the worst delay seen still lands before 05:00. Arriving early is
   harmless; arriving at lunchtime is not. Nothing assumes an exact run time, and after
   this nothing assumes the ticks arrive in cron order either.
4. **Notification fatigue** — see Cadence.

## Politeness

Requests are serialised with a delay, identify themselves as
`opportunity-tracker/1.0` -- an automated tracker, never a browser string, and with
nothing that identifies the owner (no email, name or repo link since 2026-09-26) -- and
are never retried
aggressively. `robots.txt` was checked against every target domain: **no watched path is
disallowed.** Where a site blocks automation, it is marked `manual` in `sources.csv` and
moved to the Manual Watch sheet rather than worked around. The tool never logs in to
anything.

## The two manual sheets

`out/programs.xlsx` has five sheets — Check By Hand, Manual Watch, Priority, Legend and
Left Out. Two of them exist because automation cannot cover everything, and pretending
otherwise is how you miss a deadline:

- **Manual Watch** (31 rows) — things this tool provably cannot alert you about, each
  with the reason, how to check, and when. Three groups: sites that return 403 to every
  automated client (all of Citadel, IAS/PCMI, MAA/Putnam), competitions announced on
  Instagram and listservs *before* the website changes (Cornell CTC, UChicago UTC,
  Traders@MIT, Berkeley), and pages with nothing to diff (The Deck Game yields ~28
  characters of text).
- **Priority** (36 rows) — a ranked shortlist to keep an eye on yourself in case this
  repo breaks. Band A is high value with real selection risk (Jane Street FTTP first);
  Band B is Stanford-internal, where your odds are genuinely best (CURIS, SURIM, VPUE
  grants, Directed Reading, Section Leading); Band C is open entry, where showing up is
  the only gate. Every row states **whether this tool will actually alert you** — 11 of
  the 30 are named in the repos already tracked, 13 need Phase 3, and 4 are never
  automatable.

## Verified source facts (probed 2026-09-05)

Worth knowing before extending this, because several came out differently than expected.
**Read this as a dated record, not as current state** — corrections are appended below
rather than rewritten in place, because the useful thing to know is not just the right
answer but that the old answer was wrong and when that was found out:

- **All of Citadel blocks automation** — `citadel.com`, `citadelsecurities.com` and the
  datathon page return 403 to a bot user-agent *and* a browser user-agent. Discover
  Citadel is freshman-eligible and marked YES, and this tool is structurally blind to it.
  It is the largest gap in the system.
- **~10 of the ~40 Tier 3 pages are JS shells**: AQR university jobs returns *zero*
  characters of text from 62KB of HTML; CURIS 5; SLAC 4; SIG Discovery 520 from a 402KB
  page. These need Playwright.
- **Google Careers ASDI search is server-rendered** (21KB of text) — no browser needed.
  Search for `Associate Software Developer Intern`; it was renamed from STEP.
- **Stale URLs**: Berkeley's `competition.html` is a 404 (site rebuilt in Astro; use
  `/competition/`), D. E. Shaw's `/fellowships` path is gone, DRW 308-redirects to
  `/work-at-drw/listings?filterType=campus&value=Campus`.
- **`LuisaE/opportunities` is on `master`**, and SimplifyJobs is on `dev` — which is why
  the branch is read from the API rather than assumed.
- **Kaggle** listing pages are JS shells and its official API 401s without credentials
  (free: `KAGGLE_USERNAME` / `KAGGLE_KEY`). An unauthenticated internal RPC endpoint does
  return 200, but building a daily job on an undocumented endpoint is not worth it.
- **MIT Pokerbots** is pollable over plain HTTP, but the sheet already marks it `NO`: the
  competition server requires a teammate with MIT certificates.

**Corrections since (2026-09-12 to 2026-09-17).** Three of the findings above did not
survive contact:

- **SIG is not unreachable.** Its careers page really does yield 221 characters from
  402KB of HTML, but that is a Phenom/iCIMS front end, and `careers.sig.com/api/jobs`
  answers the tracker's own User-Agent with 263 postings, descriptions and a recruiting
  category. It is now `method=phenom`, not `manual`. The general lesson —
  **a JavaScript shell is a reason to look for the JSON behind it, not a reason to give
  up** — is now automated: `jobs.discovery.mine_pages` fingerprints every watched page
  against vendor *domains* weekly and proposes what it finds.
- **Thirteen `page_text` rows were marketing pages a click above the real board**, each
  one carrying a note asserting the firm was on no public ATS. That claim was false for
  all thirteen. Two were worse than wrong — they were *succeeding* off the wrong page.
  `aqr-internship-program` 302'd to `aqr.com/404` and reported success every run off the
  error page's own prose, 4,683 characters past both content floors. This is why
  `process.redirect.verdict` exists: **where a fetch landed is part of whether it
  succeeded**, and the two content floors only ask whether a page has *enough text*, so
  they catch a JS shell and are blind to a prose-filled page with no listings.
- **Nine pages returned 403 from the Actions runner and 200 from a laptop** under the
  identical User-Agent — the runner's datacenter IP range was refused, not its identity.
  All nine are resolved. The diagnostic that did it is worth reusing: `dig +short CNAME`
  on the host. Every 403 was a firm's own marketing site behind a WAF (`gtsx.com` and
  `www.twosigma.com` are both `wp.wpenginepowered.com`), while every vendor-hosted ATS
  host in the watchlist answers the runner fine. A 403 from CI is a signal to go and find
  the vendor host behind the marketing page, not a dead end.

Citadel is the one that has not moved, and it is the sharpest case in the project. It is
**not** unreachable: `citadel.com/careers/students/` returns 403 to this tool's
User-Agent and HTTP 200 with 129KB to a spoofed Chrome string. The reason it stays
unfixed is that the only route through is to misrepresent who the client is, and this
project fetches under an honest User-Agent. So a page that reads fine in a
browser-presenting fetcher is not evidence the tracker can read it — re-measure with the
tracker's own client before moving a row off `manual`.

## Acceptance tests

Spec section 14. `tests/test_acceptance.py` runs offline against a stubbed GitHub client.
The two rows marked *verified in production* are not asserted by the suite — they are
properties of the live repo, and the digest history and `git log` are the evidence.

| # | What it checks | Status |
|---|---|---|
| 14.1 | `workflow_dispatch` completes and opens an Issue | verified in production |
| 14.2 | Hand-edited snapshot → reported as High | passing |
| 14.3 | A 404 → HEALTH, not a change; `consecutive_failures` increments | passing |
| 14.4 | A new "Freshman Insight Program" row → discovery candidate | passing |
| 14.5 | Women-only program → `relevant: false`, identity gate cited | **needs `ANTHROPIC_API_KEY`** |
| 14.5c | Same identity gate, enforced on the `gpt-5-mini` backend | **needs `AZURE_OPENAI_API_KEY`** |
| 14.6 | A quiet run → an Issue anyway, titled `(no changes)` | passing |
| 14.6b | A same-day retry mails nothing, even when it finds changes or a failure | passing |
| 14.6c | "Cannot ask GitHub" answers `None`, never "not yet delivered" | passing |
| 14.7 | `build_xlsx.py` matches the seed formatting | passing |
| 14.8 | `git log --follow data/programs.csv` is line-level readable | verified in production |

## What is not built yet

- **Playwright for JS-shell pages.** Still no browser ships, and the case for one is
  weaker than it looks: the pages that first motivated it turned out to have JSON
  endpoints behind them, which `jobs.discovery.mine_pages` now finds automatically. What
  a browser would actually buy is the residue — Stanford CURIS (5 characters of text),
  SLAC (4), The Deck Game (~28) — at roughly 3–6 minutes added to every run.
- **First-class `icims` and `avature` methods.** GTS is on iCIMS and Two Sigma on
  Avature; both are watched as plain `page_text`, which works but diffs prose rather than
  rows. They sit in `jobs.discovery._UNSUPPORTED_VENDORS` so the weekly probe correctly
  declines to propose them. On GTS the `in_iframe=1` query parameter is load-bearing:
  without it the response is the WordPress wrapper at 2,877 characters and ratio 0.017,
  under the content floor; with it, 10,156 characters at 0.241.
- **Asking the employer question once per employer.** Rule 8 — is this a generic software
  internship at an unremarkable firm — is asked once per *posting*, and Figma appeared
  four times in one digest when the answer cannot differ between them. This is the right
  fix if classification cost ever becomes the constraint.
- **Folding the four non-stage scripts into the stage model.** `build_xlsx.py`,
  `add_opportunity.py`, `seed_programs.py` and `calendar_reminders.py` sit outside it on
  purpose and are named in the layering test's exception list with a reason each.

Cross-source dedupe *was* on this list and is now built: `process.suppress` groups rows
on `core.text.posting_identities` and keeps one. See the README for why the identity has
to come from the link rather than the title.
