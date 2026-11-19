"""Reading and writing `.run/`, the stage handoff.

Each stage writes an artifact the next one reads, which is what makes every stage
separately runnable and separately inspectable. `run.py all` still runs them in one
process, so the Actions workflow keeps its single step and nothing about the schedule
changes -- the files are for the mornings when something looks wrong and the question is
*which stage* produced it.

`.run/` is gitignored, following the precedent already set by `data/postings_cache/`:
derived, re-fetchable data that would otherwise bury the CSV history that `git log`
exists to answer. The raw bodies in particular are ~157 fetched pages a day.

**Raw bodies are per-source files, not one raw.json.** JSON-escaping a 300 KB HTML page
inflates it 15-30% for nothing; `run.py process --only X` should not have to parse 15 MB
to read one source; and writing the bytes verbatim defers the decode decision to
`process`, where the parser knows the encoding, instead of forcing a lossy
`response.text` at the gather boundary. The other artifacts stay single files -- they are
small, and their whole value is being readable in one look.
"""
from __future__ import annotations

import pathlib
import shutil

from core import codec, paths

# One name per artifact, so a typo is an ImportError rather than a missing file that
# reads as an empty stage.
RAW_DIR = "raw"
RAW_INDEX = "raw/index.json"
CHANGES = "changes.json"
ENRICHED = "enriched.json"
SCREENED = "screened.json"
JUDGED = "judged.json"
DIGEST = "digest.md"
# The title is part of the delivered artifact and has to survive the stage boundary
# too, so it gets a file rather than being smuggled into the first line of the body.
DIGEST_TITLE = "digest.title"
# The emailed digest: every row, with no issue-body ceiling to trim to. The markdown is
# the email's plain-text part and the HTML is what a mail client shows.
DIGEST_FULL = "digest_full.md"
DIGEST_HTML = "digest.html"


def path(name: str) -> pathlib.Path:
    """Resolved against `paths.RUN_DIR` at call time, so tests can redirect it."""
    return paths.RUN_DIR / name


def reset() -> None:
    """Clear `.run/` at the start of a gather.

    A stage must never read half of this run's artifacts and half of yesterday's. The
    codec's version check catches a *shape* change; only removing the directory catches
    a source that failed today and so left last run's body in place, which would read
    as a successful fetch.
    """
    if paths.RUN_DIR.exists():
        shutil.rmtree(paths.RUN_DIR)
    paths.RUN_DIR.mkdir(parents=True, exist_ok=True)


def write(name: str, kind: str, obj) -> pathlib.Path:
    target = path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(codec.dumps(kind, obj), encoding="utf-8")
    return target


def read(name: str, kind: str, cls):
    """Decode an artifact, or raise ArtifactError naming the stage that should rerun."""
    target = path(name)
    if not target.exists():
        raise codec.ArtifactError(
            f"{name} has not been written. Run the stage that produces it first, or "
            f"use `run.py all`."
        )
    return codec.loads(kind, cls, target.read_text(encoding="utf-8"))


def exists(name: str) -> bool:
    return path(name).exists()


def write_text(name: str, text: str) -> pathlib.Path:
    target = path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def read_text(name: str) -> str:
    return path(name).read_text(encoding="utf-8")


def body_path(source_id: str) -> pathlib.Path:
    return path(f"{RAW_DIR}/{source_id}.body")


def write_body(source_id: str, data: bytes) -> pathlib.Path:
    """The undecoded bytes exactly as fetched. `process` owns the decode."""
    target = body_path(source_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def read_body(source_id: str) -> bytes | None:
    """None means this source was not fetched -- which is not the same as fetching
    nothing, and the caller has to be able to tell those apart."""
    target = body_path(source_id)
    return target.read_bytes() if target.exists() else None
