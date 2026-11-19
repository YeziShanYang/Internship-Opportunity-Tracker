"""Stage runner. One verb per pipeline stage, plus `all` for the daily job.

    run.py all --dry-run          the whole pipeline, printing the digest
    run.py gather                 fetch all ~157 sources into .run/raw/ and stop
    run.py process                parse .run/raw/ and diff it. No network at all
    run.py enrich                 fetch the posting behind each changed row
    run.py screen                 rule out what a quoted phrase settles. No model
    run.py render                 rebuild the digest from .run/. No network, no model
    run.py classify               the model calls, and only the model calls
    run.py deliver                post the rendered digest, subject to the daily cap

The point of the verbs is not that anyone runs them one at a time every morning --
`run.py all` does the whole thing in one process, so the Actions workflow keeps its
single step and nothing about the schedule changes. The point is that when a digest
looks wrong, the question "which stage produced this" has an answer you can read off
disk in `.run/`, instead of requiring the prompts to be reconstructed from the state
commits. That reconstruction has been done once, to work out why 2026-09-12 cost 34
cents, and it should never have needed doing.

Every stage is separately runnable, and each one reads the previous stage's artifact
rather than recomputing it. `run.py gather` then `run.py process` twice produces
byte-identical output with no second fetch; `run.py all` then `run.py render` produces
the byte-identical digest with no network at all.
"""
from __future__ import annotations

import argparse
import sys

# Stages in pipeline order. `all` is not a stage; it is the composition of them.
STAGES = ("gather", "process", "enrich", "screen", "classify", "render", "deliver")

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("verb", choices=(*STAGES, "all"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the digest; open no issue and write no state. Stage artifacts are "
             "still written -- they are derived scratch, not the database, and a dry "
             "run is exactly when you want to read them",
    )
    parser.add_argument("--only", help="restrict to a single source_id")
    parser.add_argument(
        "--force-discovery", action="store_true",
        help="run the weekly source-discovery pass regardless of the day",
    )
    parser.add_argument(
        "--no-classify", action="store_true", help="skip the LLM step entirely"
    )
    parser.add_argument(
        "--force-health", action="store_true",
        help="render the quiet-day digest and ignore the once-a-day delivery lock",
    )
    parser.add_argument(
        "--email-only", action="store_true",
        help="with `deliver`: email the rendered digest without opening an issue or "
             "checking the daily cap -- to test the email settings, or to re-send a "
             "morning whose email failed",
    )
    args = parser.parse_args(argv)

    # Imported here rather than at module scope so `run.py --help` does not pay for
    # httpx, openpyxl and the provider SDKs.
    from jobs import daily

    if args.verb == "all":
        return daily.run(args)
    if args.verb == "gather":
        return daily.gather_only(args)
    if args.verb == "process":
        return daily.process_only(args)
    if args.verb == "enrich":
        return daily.enrich_only(args)
    if args.verb == "screen":
        return daily.screen_only(args)
    if args.verb == "render":
        return daily.render_only(args)
    if args.verb == "classify":
        return daily.classify_only(args)
    if args.verb == "deliver":
        return daily.deliver_only(args)
    raise AssertionError(f"unreachable: no handler for {args.verb!r}")


if __name__ == "__main__":
    sys.exit(main())
