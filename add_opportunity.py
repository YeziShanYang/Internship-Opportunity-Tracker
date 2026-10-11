"""Add opportunities to the tracker, and probe whether they can be watched.

Two modes, both used by the `add-opportunity` skill:

  --probe URL         Report whether a URL can be polled at all: HTTP status, how much
                      visible text a plain fetch actually yields, whether robots.txt
                      allows the path, and a recommended tier/method. This is the same
                      check that classified ~40 sources during the initial build, where
                      it turned up four sites that 403 every client and ten that render
                      entirely in JavaScript.

  --add FILE|-        Append validated rows from a JSON array. Writes programs.csv and,
                      per row, sources.csv / manual.csv / priority.csv, so an addition
                      lands on the right watchlist rather than only in the master list.

Safety properties, because this is invoked by an agent:
  * Adding is append-only and deduplicated by name. An existing row is never modified,
    so hand-verified research cannot be clobbered (spec section 8 rule 5).
  * `eligible` is validated against the Legend codes and defaults to CHECK. The agent is
    not permitted to assert YES; that is a human judgment.
  * --dry-run prints exactly what would change and writes nothing.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

from core import clock, paths
from persist import store

VALID_ELIGIBLE = {
    "YES", "NO", "LATER", "CHECK", "INVITE", "VIA CLUB",
    "LOW VALUE", "USE", "USE WEEKLY", "STALE", "APPLIED - too early",
}
# The agent must not claim eligibility it cannot verify; a human sets these.
AGENT_FORBIDDEN_ELIGIBLE = {"YES"}

VALID_CATEGORIES = {
    "Quant firm program", "Competition", "Tech internship", "Research / math",
    "Stanford-only", "Conference / other", "Local (St. Louis)", "Tracker / resource",
}

MANUAL_COLUMNS = ["name", "category", "url", "coverage", "why_manual", "how_to_check", "when_to_check"]
PRIORITY_COLUMNS = ["rank", "band", "name", "category", "value", "probability",
                    "watch_window", "will_this_tool_alert_you", "why", "url"]


# --------------------------------------------------------------------------- probe

def _visible_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&[a-z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _robots_allows(url: str) -> tuple[bool | None, str]:
    """Check robots.txt, distinguishing "disallowed" from "could not be read".

    urllib's RobotFileParser treats a 401/403 on robots.txt itself as disallow-all,
    which conflates a site protecting its robots.txt with a site actually forbidding
    the path. Those warrant different notes, so fetch it directly instead -- and with a
    timeout, which RobotFileParser.read() does not accept and which otherwise lets a
    slow host hang the probe indefinitely.
    """
    parts = urllib.parse.urlparse(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    request = urllib.request.Request(robots_url, headers={"User-Agent": paths.USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(500_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return True, "no robots.txt (404) - nothing disallowed"
        return None, f"robots.txt itself returned HTTP {exc.code} - could not verify"
    except Exception as exc:
        return None, f"robots.txt unreachable ({type(exc).__name__}) - could not verify"

    if not body.strip() or "<html" in body[:200].lower():
        return None, "robots.txt is missing or not a robots file - could not verify"

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(body.splitlines())
    try:
        allowed = parser.can_fetch(paths.USER_AGENT, url)
    except Exception:
        return None, "robots.txt unparseable - could not verify"
    return allowed, "allowed by robots.txt" if allowed else "DISALLOWED by robots.txt"


def probe(url: str) -> dict:
    """Fetch a URL once, politely, and report whether it is watchable."""
    robots_ok, robots_note = _robots_allows(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": paths.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    result = {"url": url, "robots": robots_note, "robots_allows": robots_ok}
    try:
        with urllib.request.urlopen(request, timeout=paths.HTTP_TIMEOUT_SECONDS) as response:
            body = response.read(1_000_000).decode("utf-8", "replace")
            result.update(status=response.status, final_url=response.geturl())
    except urllib.error.HTTPError as exc:
        result.update(status=exc.code, error="HTTPError", html_length=0, text_length=0)
        result["recommendation"] = (
            "MANUAL - blocked" if exc.code in (401, 403, 429)
            else "CHECK URL - the page did not load"
        )
        return result
    except Exception as exc:
        result.update(status=None, error=f"{type(exc).__name__}: {exc}",
                      html_length=0, text_length=0)
        result["recommendation"] = "MANUAL - the page could not be fetched"
        return result

    text = _visible_text(body)
    result.update(html_length=len(body), text_length=len(text), sample=text[:200])

    if robots_ok is False:
        result["recommendation"] = "MANUAL - robots.txt disallows this path; do not poll it"
    elif len(text) < 50:
        result["recommendation"] = (
            f"MANUAL - only {len(text)} characters of visible text; there is likely "
            "nothing to diff even with a browser"
        )
    elif len(text) < 200:
        result["recommendation"] = (
            f"TIER 3 with render_js=true - only {len(text)} characters of visible text "
            f"from {len(body)} bytes of HTML, so the page renders in JavaScript. "
            "If it is under ~50 characters there may be nothing to diff at all: prefer MANUAL."
        )
    else:
        result["recommendation"] = (
            f"TIER 3 with render_js=false - {len(text)} characters of visible text over "
            "plain HTTP. Pollable as-is; consider a CSS selector if the page is "
            "marketing-heavy."
        )
    return result


# --------------------------------------------------------------------------- add

def _slugify(name: str, taken: set[str], limit: int = 40) -> str:
    """A readable, unique source_id.

    Truncates on word boundaries rather than mid-word -- a naive slice produced ids like
    `harvard-undergraduate-trading-competitio`, which are both ugly and prone to
    colliding with each other once truncated.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > limit:
            break
        slug = candidate
    slug = slug or (words[0][:limit] if words else "source")
    if slug not in taken:
        return slug
    for suffix in range(2, 100):
        candidate = f"{slug}-{suffix}"
        if candidate not in taken:
            return candidate
    raise ValueError(f"could not build a unique source_id for {name!r}")


def _load(path: pathlib.Path, columns: list[str]) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [{c: (row.get(c) or "") for c in columns} for row in csv.DictReader(fh)]


def _save(path: pathlib.Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") or "" for c in columns})


def validate(entry: dict, index: int) -> list[str]:
    problems = []
    for field in ("name", "website", "category", "notes"):
        if not str(entry.get(field, "")).strip():
            problems.append(f"[{index}] {field} is required")
    category = entry.get("category", "")
    if category and category not in VALID_CATEGORIES:
        problems.append(f"[{index}] category {category!r} is not one of {sorted(VALID_CATEGORIES)}")
    eligible = str(entry.get("eligible", "CHECK")).strip() or "CHECK"
    if eligible not in VALID_ELIGIBLE:
        problems.append(f"[{index}] eligible {eligible!r} is not a Legend code")
    if eligible in AGENT_FORBIDDEN_ELIGIBLE:
        problems.append(
            f"[{index}] eligible={eligible!r} is not allowed from this tool - use CHECK and "
            "let a human confirm it"
        )
    website = str(entry.get("website", ""))
    if website and not website.startswith(("http://", "https://")):
        problems.append(f"[{index}] website must be an absolute URL")
    return problems


def add(entries: list[dict], dry_run: bool, source_article: str = "") -> int:
    problems = [p for i, e in enumerate(entries) for p in validate(e, i)]
    if problems:
        print("refusing to add anything - fix these first:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    programs = store.read_programs()
    sources = store.read_sources()
    manual = _load(paths.DATA / "manual.csv", MANUAL_COLUMNS)
    priority = _load(paths.DATA / "priority.csv", PRIORITY_COLUMNS)

    existing = {p["name"].strip().lower() for p in programs}
    existing_sources = {s["source_id"] for s in sources}
    existing_manual = {m["name"].strip().lower() for m in manual}
    existing_priority = {p["name"].strip().lower() for p in priority}
    next_rank = max([int(p["rank"]) for p in priority if str(p["rank"]).isdigit()] or [0]) + 1

    added, skipped, placements = [], [], []
    today = clock.today_iso()

    for entry in entries:
        name = entry["name"].strip()
        if name.lower() in existing:
            skipped.append(name)
            continue

        row = {column: "" for column in paths.PROGRAM_COLUMNS}
        row.update(
            category=entry["category"],
            name=name,
            website=entry["website"],
            applications_open=entry.get("applications_open", "Check site"),
            target_years=entry.get("target_years", "Unknown"),
            eligible=str(entry.get("eligible", "CHECK")).strip() or "CHECK",
            notes=entry["notes"],
            status="unknown",
            first_seen=today,
            muted="false",
        )

        source = entry.get("source")
        if source:
            source_id = source.get("source_id") or _slugify(name, existing_sources)
            if source_id not in existing_sources:
                source_row = {column: "" for column in paths.SOURCE_COLUMNS}
                source_row.update(
                    source_id=source_id,
                    tier=str(source.get("tier", 3)),
                    method=source.get("method", "page_text"),
                    url=source.get("url") or entry["website"],
                    program_names=name,
                    selector=source.get("selector", ""),
                    render_js="true" if source.get("render_js") else "false",
                    job_list="true" if source.get("job_list") else "false",
                    consecutive_failures="0",
                    signal=source.get("signal", "high"),
                    notes=source.get("notes", ""),
                )
                sources.append(source_row)
                existing_sources.add(source_id)
                row["source_id"] = source_id
                placements.append(f"{name} -> sources.csv ({source_row['method']}, tier {source_row['tier']})")

        entry_manual = entry.get("manual")
        if entry_manual and name.lower() not in existing_manual:
            manual.append({
                "name": name,
                "category": entry["category"],
                "url": entry["website"],
                "coverage": entry_manual.get("coverage", "Planned - phase 3"),
                "why_manual": entry_manual.get("why_manual", ""),
                "how_to_check": entry_manual.get("how_to_check", "Open the page in your browser."),
                "when_to_check": entry_manual.get("when_to_check", ""),
            })
            existing_manual.add(name.lower())
            placements.append(f"{name} -> manual.csv (Manual Watch sheet)")

        entry_priority = entry.get("priority")
        if entry_priority and name.lower() not in existing_priority:
            priority.append({
                "rank": str(next_rank),
                "band": entry_priority.get("band", "C - open entry, participation is the only gate"),
                "name": name,
                "category": entry["category"],
                "value": entry_priority.get("value", "Medium"),
                "probability": entry_priority.get("probability", "Unknown"),
                "watch_window": entry_priority.get("watch_window", ""),
                "will_this_tool_alert_you": entry_priority.get("will_this_tool_alert_you", "No - needs phase 3 page watching"),
                "why": entry_priority.get("why", ""),
                "url": entry["website"],
            })
            existing_priority.add(name.lower())
            next_rank += 1
            placements.append(f"{name} -> priority.csv (Priority sheet, rank {next_rank - 1})")

        programs.append(row)
        existing.add(name.lower())
        added.append(row)

    print(f"{len(added)} added, {len(skipped)} already present")
    for row in added:
        print(f"  + [{row['eligible']:<8}] {row['name']}")
    for name in skipped:
        print(f"  = already tracked, left untouched: {name}")
    for placement in placements:
        print(f"  -> {placement}")

    if dry_run:
        print("\n(dry run: nothing written)")
        return 0
    if not added:
        return 0

    store.write_programs(programs)
    store.write_sources(sources)
    if manual:
        _save(paths.DATA / "manual.csv", MANUAL_COLUMNS, manual)
    if priority:
        _save(paths.DATA / "priority.csv", PRIORITY_COLUMNS, priority)
    for row in added:
        store.append_proposal(
            f"add-opportunity\t{row['name']}\tadded from {source_article or 'a supplied article'}"
            f"\teligible={row['eligible']} (agent-set; confirm by hand)"
        )
    print(f"\nwrote {paths.PROGRAMS_CSV.name}, {paths.SOURCES_CSV.name} and the watchlist CSVs")
    print("next: python build_xlsx.py, then review `git diff` before committing")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe", metavar="URL", help="report whether a URL can be watched")
    parser.add_argument("--add", metavar="FILE", help="JSON array of opportunities, or - for stdin")
    parser.add_argument("--from-article", default="", help="the article URL, recorded in proposals.log")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.probe:
        print(json.dumps(probe(args.probe), indent=2, ensure_ascii=False))
        return 0
    if args.add:
        raw = sys.stdin.read() if args.add == "-" else pathlib.Path(args.add).read_text(encoding="utf-8")
        entries = json.loads(raw)
        if isinstance(entries, dict):
            entries = [entries]
        return add(entries, args.dry_run, args.from_article)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
