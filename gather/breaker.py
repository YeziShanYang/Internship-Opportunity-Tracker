"""The circuit breaker: whether a failing source is worth fetching this run.

Borrowed from zshah101's `health.py`. Measured 2026-09-12: nine sources sat at exactly
three consecutive failures with `last_success` empty -- they had never worked once --
and each was being fetched three times a morning forever. Boards do come back
(rate-limit storms, a page mid-deploy), so the window is capped rather than permanent
and one success resets everything.

This lives in `gather` because it is **fetch** policy, and it must never become
**reporting** policy. A quarantined source still appears in HEALTH and still escalates,
because "we have stopped looking" is the strongest form of "this source is blind, not
quiet" (spec 10.1) and the one a reader would most readily assume had not happened.
Quarantining a source silently would be the exact failure this project has been bitten
by twice.
"""
from __future__ import annotations

import datetime

QUARANTINE_AFTER_FAILURES = 3
QUARANTINE_HOURS = (6, 12, 24, 48)
QUARANTINE_CAP_HOURS = 72


def quarantine_hours(consecutive_failures: int) -> int:
    """How long to wait before retrying a source that has failed this many times."""
    step = consecutive_failures - QUARANTINE_AFTER_FAILURES
    if step < 0:
        return 0
    if step < len(QUARANTINE_HOURS):
        return QUARANTINE_HOURS[step]
    return QUARANTINE_CAP_HOURS


def quarantine_state(
    source: dict[str, str], now: datetime.datetime | None = None
) -> tuple[bool, str]:
    """Return (skip_this_run, human explanation).

    Returns `(False, "")` for a healthy source, for one below the threshold, and for
    one whose window has expired -- an expired window is exactly how a recovered board
    gets retried without anyone intervening.
    """
    if (source.get("gone_since") or "").strip():
        # A programme page that answered "not here" is watched for its return, and one
        # 404 a morning costs nothing; backing off would only delay seeing it come back.
        return False, ""
    failures = int(source.get("consecutive_failures") or 0)
    hours = quarantine_hours(failures)
    if not hours:
        return False, ""
    last_attempt = (source.get("last_attempt") or "").strip()
    if not last_attempt:
        # No record of an attempt, so nothing says the window has started. Try it.
        return False, ""
    try:
        started = datetime.datetime.fromisoformat(last_attempt)
    except ValueError:
        return False, ""
    if started.tzinfo is None:
        started = started.replace(tzinfo=datetime.timezone.utc)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    until = started + datetime.timedelta(hours=hours)
    if now >= until:
        return False, ""
    never = not (source.get("last_success") or "").strip()
    return True, (
        f"quarantined for {hours}h after {failures} consecutive failures, retry after "
        f"{until.isoformat(timespec='minutes')}"
        + (" — it has never succeeded, so check the URL rather than waiting" if never else "")
    )
