"""The morning digest: the job that composes every stage.

Load state, check every enabled source, screen and classify what changed, render a
digest, deliver it as a GitHub Issue, then write state back so the commit that follows
is the audit trail.

A job is not a stage. This one reaches into `gather` (the source checks and, on a
Monday, discovery's GitHub search), `process` (the suppression filters), `classify` and
`deliver`, plus `persist` at both ends -- which is exactly why it lives in `jobs/`
rather than being squeezed into one level of the stage graph.

The process exits non-zero only when something is wrong with the *tool*. A source that
404s, gets blocked, or returns nothing is data: it lands in the health block and bumps
that source's failure count. Spec section 10.1 -- silent success is the failure mode
that kills projects like this, so "the job exited 0" must never be the only evidence
that anything happened.
"""
from __future__ import annotations

import argparse
import sys

import classify
from deliver import digest, health, issue, urgency
from jobs import coverage as cover
from jobs import discovery as discover
from core import clock, models, paths
from enrich import bodies as enrich_bodies
from gather import clients, collect
from persist import artifacts, store
from process import build, parse_ats, snapshot as snap, suppress
import screen


# Re-exported so the checker registry has one name for the rest of the tree. The
# method tables themselves live with the stages that own them: which client a method
# needs is a fetch concern (gather.collect.CLIENT_FOR) and which assessor reads it is a
# parse concern (process.build.ASSESSORS).
CHECKERS = collect.CLIENT_FOR
GITHUB_CLIENT = collect.GITHUB_CLIENT
WEB_CLIENT = collect.WEB_CLIENT
UNWATCHED = collect.UNWATCHED


def run_sources(
    sources: list[dict[str, str]], only: str | None
) -> list[models.SourceResult]:
    """Gather, then process. Two stages, one call, for the callers that want both."""
    return build.build(sources, _collect(sources, only))


def update_source_state(
    sources: list[dict[str, str]], results: list[models.SourceResult]
) -> None:
    by_id = {source["source_id"]: source for source in sources}
    now = clock.iso()
    for result in results:
        source = by_id.get(result.source_id)
        if source is None:
            continue
        if result.quarantined:
            # The breaker skipped the fetch, so nothing was learned. Leaving every
            # counter alone is what stops a quarantine from extending itself: if this
            # incremented, a source would back off further for not being looked at.
            continue
        source["last_attempt"] = now
        if result.ok:
            source["last_success"] = now
            source["consecutive_failures"] = "0"
        else:
            previous = int(source.get("consecutive_failures") or 0)
            source["consecutive_failures"] = str(previous + 1)


def update_program_state(
    programs: list[dict[str, str]],
    sources: list[dict[str, str]],
    results: list[models.SourceResult],
    judgments: list[classify.Judgment],
) -> None:
    """Update the tracking columns only.

    `eligible`, `notes` and the rest of the hand-researched columns are never touched
    here (spec section 8 rule 5). A status change proposed by the classifier is applied
    to the tracking `status` column, which the tool owns, and only when the model was
    reasonably sure.
    """
    now = clock.iso()
    by_source = {source["source_id"]: source for source in sources}
    by_name = {program["name"]: program for program in programs}

    for result in results:
        source = by_source.get(result.source_id)
        if source is None:
            continue
        names = [n.strip() for n in (source.get("program_names") or "").split("|") if n.strip()]
        for name in names:
            program = by_name.get(name)
            if program is None:
                continue
            program["last_checked"] = now
            if result.ok and result.snapshot_text is not None:
                program["snapshot_hash"] = clock.sha256_text(result.snapshot_text)
            if result.changes:
                program["last_changed"] = now

    # Aggregate job boards must not drive the status of their own programs.csv row.
    # `judgment.program_name` falls back to `change.program_name`, which for every
    # Simplify row is the single aggregate program "SimplifyJobs Summer 2027
    # Internships". Before postings were classified this path was unreachable for that
    # source; now ~35 per-row judgments a day would each try to set the status of one
    # row that represents the whole board, which is meaningless and would churn
    # proposals.log. Per-program sources are unaffected.
    noisy = {
        source["source_id"]
        for source in sources
        if (source.get("signal") or "high").strip().lower() == "low"
    }

    for judgment in judgments:
        if not (judgment.classified and judgment.relevant):
            continue
        if judgment.confidence == "low":
            continue
        if judgment.change.source_id in noisy:
            continue
        program = by_name.get(judgment.program_name)
        if program is None or judgment.new_status not in paths.PROGRAM_STATUSES:
            continue
        if judgment.new_status != "unknown" and program["status"] != judgment.new_status:
            store.append_proposal(
                f"{judgment.change.source_id}\t{judgment.program_name}\tstatus "
                f"{program['status']} -> {judgment.new_status}\t{judgment.why}"
            )
            program["status"] = judgment.new_status
            program["last_changed"] = now


def _collect(
    sources: list[dict[str, str]], only: str | None
) -> list[models.FetchAttempt]:
    """Run the gather stage, supplying the one decision it cannot make itself.

    Which Workday descriptions are worth a second request is a filtering judgment, so
    it belongs to `process`; `gather` may not import that layer. The job is the only
    place that knows about both, so the job hands the predicate over.
    """
    return collect.collect(
        sources, only, wants_detail=parse_ats.wants_workday_detail)


def _already_delivered(today: str) -> bool:
    """Has today's digest already gone out?

    Ask GitHub first and treat the local marker as the fallback, not the other way
    round: the marker comes from whatever commit this run checked out, and late ticks
    do not necessarily dispatch in cron order, so it can be stale. Either source saying
    "delivered" is enough -- a stale marker cannot cause a duplicate, only a redundant
    suppression, and it is only ever stale in the direction of a delivery this run did
    not see. `already_sent` returns None rather than False when GitHub cannot be asked,
    so "I could not check" is never read as "not yet delivered".
    """
    marker = store.read_last_delivered() == today
    asked = issue.already_sent(today)
    return marker if asked is None else (asked or marker)


def _enrich(
    changes: list[models.Change], args: argparse.Namespace
) -> dict[str, enrich_bodies.PostingBody]:
    """Run the enrich stage and record what it managed."""
    if args.no_classify:
        # Nothing downstream will read a posting body, so fetching ~35 of them would be
        # pure cost. Still writes the (empty) artifact, so a reader can tell the stage
        # ran and declined rather than never having run.
        bodies: dict[str, enrich_bodies.PostingBody] = {}
    else:
        bodies = enrich_bodies.collect(changes)
    artifacts.write(artifacts.ENRICHED, "enriched", bodies)
    return bodies


def _screen(
    changes: list[models.Change],
    bodies: dict[str, enrich_bodies.PostingBody],
) -> dict[str, screen.Verdict]:
    """Run the deterministic screen and record its verdicts."""
    verdicts = screen.apply(changes, bodies)
    artifacts.write(artifacts.SCREENED, "screened", verdicts)
    return verdicts


def _suppress(
    results: list[models.SourceResult],
    programs: list[dict[str, str]],
    sources: list[dict[str, str]] | None = None,
) -> tuple[list[models.Change], list[models.FilterReport]]:
    return suppress.suppress(
        [change for result in results for change in result.changes],
        muted=suppress.muted_programmes(programs),
        applied=store.read_applied(),
        # Read off sources.csv rather than hardcoded: "which sources are aggregators"
        # is a property of the watchlist, and this is what decides whether a duplicate
        # keeps the employer's row or the aggregator's.
        aggregators=frozenset(
            source["source_id"] for source in (sources or [])
            if (source.get("method") or "").strip() == "github_readme"
        ),
    )


def _write_change_set(
    metrics: list[models.SourceMetrics],
    changes: list[models.Change],
    filters: list[models.FilterReport],
) -> None:
    """Written whether or not this is a dry run. `.run/` is derived scratch rather than
    the database, and a dry run is exactly when someone wants to read it. Written after
    suppression, because a muted change is not a change this run is acting on -- but
    the count of them is, which is what the FilterReports carry.

    `filters` is every report from every filter site -- per-source and run-wide in one
    list, because "did every filter report what it removed?" has to be answerable in
    one place."""
    artifacts.write(artifacts.CHANGES, "changes", models.ChangeSet(
        changes=changes, metrics=metrics, filters=filters))


def _previous_live_ids(sources: list[dict[str, str]]) -> dict[str, set[str]]:
    """Every row on every board as of the snapshots on disk.

    Read here rather than inside `urgency` because this is disk I/O and `deliver` has
    no business doing it. The snapshots are yesterday's -- this run writes its own only
    after the digest is rendered -- which is exactly the basis `urgency.live_change_ids`
    expects: yesterday's rows plus today's diff *is* today's board.
    """
    out: dict[str, set[str]] = {}
    for source in sources:
        source_id = source["source_id"]
        text = store.read_snapshot(source_id, ext="tsv") or store.read_snapshot(source_id)
        if not text:
            continue
        parsed = snap.parse_snapshot(text)
        # A row its list marks closed is still in the snapshot, and would otherwise
        # keep its pin for as long as the list keeps the row -- which on aprameyak is
        # indefinitely.
        out[source_id] = {
            clock.change_id(source_id, row.identity) for row in parsed.rows
            if not snap.is_closed(row.value)
        }
    return out


def _pin_to_row(pin: models.PinnedRow) -> dict[str, str]:
    return {column: getattr(pin, column) for column in paths.STANDING_COLUMNS}


def _row_to_pin(row: dict[str, str]) -> models.PinnedRow:
    return models.PinnedRow(**{column: row.get(column, "") for column in paths.STANDING_COLUMNS})


def run(args: argparse.Namespace) -> int:
    """The whole morning, over already-parsed arguments from `run.py`."""
    sources = store.read_sources()
    programs = store.read_programs()
    if not sources:
        print("data/sources.csv is empty or missing", file=sys.stderr)
        return 2
    if not programs:
        print("data/programs.csv is empty or missing; run seed_programs.py", file=sys.stderr)
        return 2

    # Cleared before the run, not after: a stage must never read half of this run's
    # artifacts and half of yesterday's. The codec's version check catches a shape
    # change and cannot catch a source that failed today and left last run's body in
    # place, which would read as a fetch that worked.
    artifacts.reset()

    results = run_sources(sources, args.only)
    if not results:
        print("no sources matched; nothing to do", file=sys.stderr)
        return 2

    changes, filters = _suppress(results, programs, sources)
    suppressed_muted = suppress.removed_by(filters, suppress.MUTED)
    suppressed_applied = suppress.removed_by(filters, suppress.APPLIED)
    metrics = [models.SourceMetrics.of(result) for result in results]

    by_id = {source["source_id"]: source for source in sources}

    # The second source-network stage, named rather than hidden. O(changes): bodies are
    # fetched for the rows that moved, not for the ~4,000 postings on the boards.
    bodies = _enrich(changes, args)
    verdicts = _screen(changes, bodies)
    filters.append(screen.report(changes, verdicts))
    # One combined list, built once and used for both the artifact and the digest, so
    # a rebuild from `.run/` renders exactly what this run rendered.
    filters = [report for m in metrics for report in m.filters] + filters
    _write_change_set(metrics, changes, filters)
    judged = (
        models.JudgedSet() if args.no_classify
        else classify.classify(changes, by_id, bodies, verdicts)
    )
    judgments = judged.judgments

    # Underclassman postings stay in the top block until they leave their board (added
    # 2026-09-18). Only sources that were *checked and healthy* this run may
    # retire a pin: a failing, quarantined or --only-skipped source keeps every one of
    # its pins, because "we have stopped looking" must never render as "it closed".
    checked = {result.source_id for result in results if result.ok}
    judged.pinned = urgency.refresh_pins(
        judgments,
        [_row_to_pin(row) for row in store.read_standing()],
        urgency.live_change_ids(_previous_live_ids(sources), changes, checked),
        checked,
        clock.today_iso(),
        digest.company_and_position,
    )
    artifacts.write(artifacts.JUDGED, "judged", judged)

    update_source_state(sources, results)
    update_program_state(programs, sources, results, judgments)

    # Exactly one digest a day (spec section 9). One scheduled tick, but a manual
    # dispatch can overlap it, so more than one run can still reach this line on a
    # morning and only the first may mail.
    #
    # Ask GitHub first and treat the local marker as the fallback, not the other way
    # round: the marker comes from whatever commit this run checked out, and late ticks
    # do not necessarily dispatch in cron order, so it can be stale. See
    # deliver.issue.already_sent. Either source saying "delivered" is enough --
    # a stale marker cannot cause a duplicate, only a redundant suppression, and the
    # marker is only ever stale in the direction of a delivery this run did not see.
    today = clock.today_iso()
    delivered_today = _already_delivered(today)

    send, status_only = issue.should_send(
        judgments, metrics, already_delivered_today=delivered_today
    )
    if args.force_health:
        # A deliberate manual override: render the quiet-day shape on demand and ignore
        # the once-a-day lock.
        send, status_only = True, True

    # Discovery rides along in whichever run actually mails. The gate is "this run is
    # delivering", not "this is the first tick": when a tick is dropped, a later one
    # does the mailing, and the one-issue-per-date cap means discovery cannot open an
    # issue of its own.
    discovery_lines: list[str] = []
    discovery_notes: list[str] = []
    # --force-discovery works under --dry-run too, so the section can be eyeballed
    # before it ever mails; the writes below are what --dry-run actually suppresses.
    if (send and not args.dry_run and discover.due()) or args.force_discovery:
        try:
            with clients.build_github_client(clients.github_token()) as gh, \
                    clients.build_web_client() as web:
                candidates, discovery_notes = discover.run(gh, web, sources)
            # Priority triage is a model call, so it sits outside the client block with
            # the rest of the model work. It only ever splits the list: `run` has
            # already decided what a candidate *is*, and this decides whether the owner
            # is shown it.
            candidates, discarded, triage_notes = discover.triage(candidates)
            discovery_notes += triage_notes
            if not args.dry_run:
                discover.record(candidates, discarded)
                store.write_last_discovery()
            if candidates or discarded:
                discovery_lines = discover.lines(candidates, discarded=len(discarded))
        except Exception as exc:  # the digest must never be lost because this broke
            discovery_notes = [
                f"the weekly discovery pass failed: {type(exc).__name__}: {exc}"
            ]

    # Coverage rides the same gate as discovery: weekly, in the run that mails. It is
    # measured after discovery so a failure in either cannot take the other with it.
    coverage_line = None
    if (send and not args.dry_run and discover.due()) or args.force_discovery:
        try:
            todays = {
                r.source_id: r.snapshot_text for r in results
                if r.ok and r.snapshot_text is not None and r.snapshot_ext == "tsv"
            }
            with clients.build_web_client() as web:
                scopes = cover.run(web, todays)
            if not args.dry_run:
                cover.record(scopes)
            coverage_line = cover.line(scopes)
        except Exception as exc:  # the digest must never be lost because this broke
            discovery_notes.append(
                f"the weekly coverage measurement failed: {type(exc).__name__}: {exc}")

    title, body = digest.render(
        judgments, metrics, by_id, suppressed_applied=suppressed_applied,
        suppressed_muted=suppressed_muted,
        discovery_lines=discovery_lines, status_only=status_only,
        enriched=bodies, filters=filters, usage=judged.usage,
        pinned=judged.pinned, coverage_line=coverage_line,
    )
    for note in discovery_notes:
        body += f"\n- ⚠ {note}"

    # The exact bytes that would be posted. This is the artifact worth having most: it
    # is what a later `run.py render` has to reproduce byte-for-byte, and it is the
    # evidence for "the refactor did not change the digest".
    artifacts.write_text(artifacts.DIGEST, body)
    artifacts.write_text(artifacts.DIGEST_TITLE, title + "\n")

    print(f"{len(results)} sources checked, {len(changes)} changes, {len(judgments)} judged")
    for result in results:
        status = "ok" if result.ok else f"FAIL ({result.error})"
        print(f"  {result.source_id}: {status} rows={result.extra.get('rows', '-')}")

    if args.dry_run:
        print(
            "\n--- would send ---"
            if send
            else f"\n--- would NOT send: already delivered on {today} ---"
        )
        print(f"TITLE: {title}\n")
        print(body)
        print("\n(dry run: no issue opened, no state written)")
        return 0

    for result in results:
        if result.ok and result.snapshot_text is not None:
            store.write_snapshot(
                result.source_id, result.snapshot_text, ext=result.snapshot_ext
            )
    store.write_sources(sources)
    store.write_programs(programs)
    store.write_standing([_pin_to_row(pin) for pin in judged.pinned])

    if send:
        print(issue.deliver(title, body))
        store.write_last_delivered()
    else:
        print(
            f"a digest was already delivered on {today}: this run is a schedule retry, "
            "and the cadence is exactly one digest a day (spec section 9)"
        )
    return 0


def gather_only(args: argparse.Namespace) -> int:
    """`run.py gather`: fetch every source and stop. Writes `.run/raw/`."""
    sources = store.read_sources()
    if not sources:
        print("data/sources.csv is empty or missing", file=sys.stderr)
        return 2
    artifacts.reset()
    attempts = _collect(sources, args.only)
    if not attempts:
        print("no sources matched; nothing to do", file=sys.stderr)
        return 2
    failed = [a for a in attempts if not a.ok and not a.quarantined]
    quarantined = [a for a in attempts if a.quarantined]
    print(
        f"{len(attempts)} sources attempted, {len(attempts) - len(failed) - len(quarantined)} "
        f"fetched, {len(failed)} failed, {len(quarantined)} skipped by the breaker"
    )
    print(f"wrote {artifacts.path(artifacts.RAW_INDEX)}")
    return 0


def process_only(args: argparse.Namespace) -> int:
    """`run.py process`: read `.run/raw/` and write `.run/changes.json`. No network.

    Runnable repeatedly against one gather, which is the property the artifact boundary
    exists to give: a second run must make no requests and produce the same answer.
    """
    sources = store.read_sources()
    programs = store.read_programs()
    if not sources or not programs:
        print("data/sources.csv or data/programs.csv is empty or missing", file=sys.stderr)
        return 2
    results = build.build(sources, build.load_attempts())
    changes, filters = _suppress(results, programs, sources)
    metrics = [models.SourceMetrics.of(result) for result in results]
    _write_change_set(
        metrics, changes,
        [report for m in metrics for report in m.filters] + filters)
    print(f"{len(results)} sources processed, {len(changes)} changes")
    print(f"wrote {artifacts.path(artifacts.CHANGES)}")
    return 0


def enrich_only(args: argparse.Namespace) -> int:
    """`run.py enrich`: fetch the posting behind each change in `.run/changes.json`."""
    change_set = artifacts.read(artifacts.CHANGES, "changes", models.ChangeSet)
    bodies = enrich_bodies.collect(change_set.changes)
    artifacts.write(artifacts.ENRICHED, "enriched", bodies)
    line = enrich_bodies.health_line(bodies)
    print(line or f"{len(bodies)} change(s), none needed a posting fetch")
    print(f"wrote {artifacts.path(artifacts.ENRICHED)}")
    return 0


def screen_only(args: argparse.Namespace) -> int:
    """`run.py screen`: rule out what a quoted phrase settles. No network, no model."""
    change_set = artifacts.read(artifacts.CHANGES, "changes", models.ChangeSet)
    bodies = artifacts.read(
        artifacts.ENRICHED, "enriched", dict[str, enrich_bodies.PostingBody])
    verdicts = _screen(change_set.changes, bodies)
    print(health.filter_lines([screen.report(change_set.changes, verdicts)])[0]
          if verdicts else
          f"0 of {len(change_set.changes)} change(s) matched a rule-out phrase")
    print(f"wrote {artifacts.path(artifacts.SCREENED)}")
    return 0


def _read_run() -> tuple[models.ChangeSet, dict, models.JudgedSet]:
    """Every artifact the renderer needs, or a loud error naming the missing stage."""
    return (
        artifacts.read(artifacts.CHANGES, "changes", models.ChangeSet),
        artifacts.read(
            artifacts.ENRICHED, "enriched", dict[str, enrich_bodies.PostingBody]),
        artifacts.read(artifacts.JUDGED, "judged", models.JudgedSet),
    )


def render_only(args: argparse.Namespace) -> int:
    """`run.py render`: rebuild the digest from `.run/`. No network, no model.

    The point of the stage split, made concrete. This composes the exact bytes that
    would be posted from artifacts alone -- which is why HEALTH reads SourceMetrics
    rather than live results, and why the screen tally and the spend are values on the
    artifacts rather than module globals.

    Two things it cannot reproduce: the DISCOVERED section and the weekly coverage line. The weekly pass is a
    separate job with its own network budget and it rides along in whichever run
    actually mails, so it is not in any artifact. Said here rather than left as a
    surprising absence.
    """
    change_set, bodies, judged = _read_run()
    sources = {s["source_id"]: s for s in store.read_sources()}
    _, status_only = issue.should_send(
        judged.judgments, change_set.metrics, already_delivered_today=False)
    if args.force_health:
        status_only = True
    title, body = digest.render(
        judged.judgments, change_set.metrics, sources,
        suppressed_applied=suppress.removed_by(change_set.filters, suppress.APPLIED),
        suppressed_muted=suppress.removed_by(change_set.filters, suppress.MUTED),
        status_only=status_only, enriched=bodies,
        filters=change_set.filters, usage=judged.usage,
        # Off the artifact, never recomputed: `refresh_pins` reads the snapshots and
        # the standing file, and a re-render must not depend on whether the state
        # commit has landed since. This is what keeps the byte-identical property.
        pinned=judged.pinned,
    )
    artifacts.write_text(artifacts.DIGEST, body)
    artifacts.write_text(artifacts.DIGEST_TITLE, title + "\n")
    print(title)
    print(f"wrote {artifacts.path(artifacts.DIGEST)}")
    return 0


def classify_only(args: argparse.Namespace) -> int:
    """`run.py classify`: the model calls, and only the model calls.

    Reads what `enrich` fetched and what `screen` settled, so the changes a quoted
    phrase already decided cost nothing here. Running this twice makes no source
    requests at all -- the artifact boundary is what makes that true, and it is the
    check the plan asks for.
    """
    change_set = artifacts.read(artifacts.CHANGES, "changes", models.ChangeSet)
    bodies = artifacts.read(
        artifacts.ENRICHED, "enriched", dict[str, enrich_bodies.PostingBody])
    verdicts = artifacts.read(
        artifacts.SCREENED, "screened", dict[str, screen.Verdict])
    by_id = {s["source_id"]: s for s in store.read_sources()}
    judged = classify.classify(change_set.changes, by_id, bodies, verdicts)
    # The pins belong to judged.json, so the stage that writes it resolves them --
    # otherwise `run.py classify` then `run.py render` would drop every carried row.
    # Every source in the change set was checked to produce it, so all of them may
    # retire a pin.
    sources = list(by_id.values())
    checked = {m.source_id for m in change_set.metrics if m.ok}
    judged.pinned = urgency.refresh_pins(
        judged.judgments,
        [_row_to_pin(row) for row in store.read_standing()],
        urgency.live_change_ids(
            _previous_live_ids(sources), change_set.changes, checked),
        checked,
        clock.today_iso(),
        digest.company_and_position,
    )
    artifacts.write(artifacts.JUDGED, "judged", judged)
    spend = health.usage_line(judged.usage)
    print(f"{len(judged.judgments)} judged" + (f"; {spend}" if spend else ""))
    print(f"wrote {artifacts.path(artifacts.JUDGED)}")
    return 0


def deliver_only(args: argparse.Namespace) -> int:
    """`run.py deliver`: post `.run/digest.md`, subject to the one-a-day cap.

    The cap is applied here and not only in `all`, because this verb is exactly the
    thing that would otherwise mail a second issue for a date -- run by hand after a
    morning that already delivered. It asks GitHub first and treats the local marker as
    the fallback, for the same reason `all` does: late ticks do not dispatch in cron
    order, so a checked-out marker can be stale.
    """
    if not artifacts.exists(artifacts.DIGEST):
        print("no rendered digest; run `run.py render` first", file=sys.stderr)
        return 2
    body = artifacts.read_text(artifacts.DIGEST)
    title = artifacts.read_text(artifacts.DIGEST_TITLE).strip()

    today = clock.today_iso()
    if _already_delivered(today):
        print(
            f"a digest was already delivered on {today}: the cadence is exactly one "
            "digest a day (spec section 9), and a retry has nothing to add that the "
            "morning's issue did not already carry"
        )
        return 0
    if args.dry_run:
        print(f"--- would send ---\nTITLE: {title}\n\n{body}")
        return 0
    print(issue.deliver(title, body))
    store.write_last_delivered()
    return 0
