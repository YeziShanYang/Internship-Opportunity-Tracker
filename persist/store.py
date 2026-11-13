"""Every read and write of the canonical data files.

CSV is the source of truth (spec section 4), so every write goes through the helpers
here to keep column order and quoting byte-stable. An unstable writer would make every
run look like a whole-file change and destroy the `git log` audit trail that is the
entire reason state lives in the repo.

**Paths are read off `core.paths` at call time, never imported by value.** Every
function here says `paths.PROGRAMS_CSV`. Writing `from core.paths import PROGRAMS_CSV`
would bind the path at import and silently defeat the test suite's ability to redirect
the database at a temp directory -- while leaving every test green, which is the worst
possible failure shape. See the fingerprint guard in tests/test_acceptance.py.
"""
from __future__ import annotations

import csv
import pathlib

from core import clock, paths


def read_csv(path: pathlib.Path, columns: list[str]) -> list[dict[str, str]]:
    """Read a CSV, guaranteeing every configured column is present on every row."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row.pop(None, None)  # tolerate stray trailing commas
        for column in columns:
            row.setdefault(column, "")
            if row[column] is None:
                row[column] = ""
    return rows


def write_csv(path: pathlib.Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=columns, extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") or "" for column in columns})


def read_programs() -> list[dict[str, str]]:
    return read_csv(paths.PROGRAMS_CSV, paths.PROGRAM_COLUMNS)


def write_programs(rows: list[dict[str, str]]) -> None:
    write_csv(paths.PROGRAMS_CSV, paths.PROGRAM_COLUMNS, rows)


def read_sources() -> list[dict[str, str]]:
    return read_csv(paths.SOURCES_CSV, paths.SOURCE_COLUMNS)


def write_sources(rows: list[dict[str, str]]) -> None:
    write_csv(paths.SOURCES_CSV, paths.SOURCE_COLUMNS, rows)


def snapshot_path(source_id: str, ext: str = "txt") -> pathlib.Path:
    """Tier 1 and 2 store canonical `.tsv` rows; Tier 3 stores `.txt` page text."""
    return paths.SNAPSHOTS / f"{source_id}.{ext}"


def read_snapshot(source_id: str, ext: str = "txt") -> str | None:
    """None means "never seen"; "" means "seen, and it really was empty"."""
    path = snapshot_path(source_id, ext)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_snapshot(source_id: str, text: str, ext: str = "txt") -> None:
    paths.SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    snapshot_path(source_id, ext).write_text(text, encoding="utf-8")


def read_applied() -> dict[str, str]:
    """{key: marked_at}. Empty when nothing has ever been muted."""
    if not paths.APPLIED_TSV.exists():
        return {}
    applied: dict[str, str] = {}
    for line in paths.APPLIED_TSV.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if not parts[0]:
            continue
        applied[parts[0]] = parts[1] if len(parts) > 1 else ""
    return applied


def write_applied(applied: dict[str, str], titles: dict[str, str] | None = None) -> None:
    titles = titles or {}
    lines = ["# key\tmarked_at\ttitle"]
    lines += [
        f"{key}\t{marked}\t{titles.get(key, '')}" for key, marked in sorted(applied.items())
    ]
    paths.DATA.mkdir(parents=True, exist_ok=True)
    paths.APPLIED_TSV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_discovered() -> list[dict[str, str]]:
    return read_csv(paths.DISCOVERED_CSV, paths.DISCOVERED_COLUMNS)


def write_discovered(rows: list[dict[str, str]]) -> None:
    write_csv(paths.DISCOVERED_CSV, paths.DISCOVERED_COLUMNS, rows)


def read_backers() -> list[dict[str, str]]:
    return read_csv(paths.BACKERS_CSV, paths.BACKER_COLUMNS)


def write_backers(rows: list[dict[str, str]]) -> None:
    write_csv(paths.BACKERS_CSV, paths.BACKER_COLUMNS, rows)


def read_standing() -> list[dict[str, str]]:
    return read_csv(paths.STANDING_CSV, paths.STANDING_COLUMNS)


def write_standing(rows: list[dict[str, str]]) -> None:
    write_csv(paths.STANDING_CSV, paths.STANDING_COLUMNS, rows)


def read_snapshots(ext: str = "tsv") -> dict[str, str]:
    """Every stored snapshot of one kind, keyed by source_id."""
    if not paths.SNAPSHOTS.exists():
        return {}
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(paths.SNAPSHOTS.glob(f"*.{ext}"))
    }


def read_board_list(name: str) -> list[str]:
    """One slug per line from data/boards/<name>; blank lines and # comments skipped."""
    path = paths.BOARD_LISTS / name
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def read_coverage() -> list[dict[str, str]]:
    return read_csv(paths.COVERAGE_CSV, paths.COVERAGE_COLUMNS)


def write_coverage(rows: list[dict[str, str]]) -> None:
    write_csv(paths.COVERAGE_CSV, paths.COVERAGE_COLUMNS, rows)


def read_last_discovery() -> str:
    path = paths.LAST_DISCOVERY
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def write_last_discovery(date: str | None = None) -> None:
    paths.DATA.mkdir(parents=True, exist_ok=True)
    paths.LAST_DISCOVERY.write_text((date or clock.today_iso()) + "\n", encoding="utf-8")


def append_proposal(line: str) -> None:
    """Append-only log. Spec section 8 rule 5: never silently rewrite `eligible`."""
    paths.PROPOSALS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with paths.PROPOSALS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{clock.iso()}\t{line}\n")


def read_last_delivered() -> str:
    """The ISO date of the last delivered digest, or "" if none was ever delivered."""
    if not paths.LAST_DELIVERED.exists():
        return ""
    return paths.LAST_DELIVERED.read_text(encoding="utf-8").strip()


def write_last_delivered(date: str | None = None) -> None:
    paths.LAST_DELIVERED.parent.mkdir(parents=True, exist_ok=True)
    paths.LAST_DELIVERED.write_text(f"{date or clock.today_iso()}\n", encoding="utf-8")
