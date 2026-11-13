"""Reuse a judgment the model already made instead of paying for the question again. Pure.

Measured over the 30 digests to 2026-11-13, real classifier demand had reached ~320 calls
a day against a cap of 250, and a large share was the same question asked twice:

* 16% of judged rows had been judged on an earlier day -- a second aggregator listing a
  posting days after the first, or an edited row on the same list;
* 7% duplicated another row the same morning, the same role at the same employer
  arriving through two lists or in two cities;
* 1,255 rule-8 "large employer" rule-outs fell on just 465 employers -- RTX was judged a
  large defence contractor about a hundred times.

So a judgment is keyed on the posting -- employer plus title, normalised so that the
lists' different spellings meet -- and reused for the rest of the recruiting cycle, for
`REUSE_DAYS` at most. Three limits keep this from hiding anything:

* **Only a model judgment is stored.** The screen is free and re-derives itself, and an
  unjudged, failed or over-budget row has no answer to reuse.
* **A judgment made on the title alone is not reused for a posting whose text is now
  available.** The text is what the title-only call was missing, and it can change the
  answer.
* **The employer verdict is reused only where rule 8 is unambiguous**: an employer the
  model has ruled out as a large non-technology firm and never once kept, a generic
  title, and no first- or second-year wording in the posting. A programme for first-years
  at the same employer still goes to the model.

Location is deliberately not in the key -- the same role in two cities has the same
requirements -- so a reused judgment keeps the row's own location rather than the
stored one.
"""
from __future__ import annotations

import datetime
import re

from core import models
from core import text as coretext

# How long a stored judgment stays usable. A posting's requirements do not change while
# it is up, but an employer's title can be reused for next year's posting, which the
# cycle check below also guards against.
REUSE_DAYS = 45
# Rows are kept a while past their reuse window so the file shows what was reused, then
# dropped so it does not grow without bound.
KEEP_DAYS = 90

_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_EMPLOYER_SUFFIX = frozenset({
    "inc", "llc", "corp", "corporation", "co", "company", "ltd", "limited", "group",
    "holdings", "plc", "lp", "llp", "the",
})
_TITLE_NOISE = re.compile(
    r"^(?:20\d\d|summer|fall|autumn|winter|spring|term|season|internship|intern|interns"
    r"|co|op|coop)$")
_WORD = re.compile(r"[a-z0-9+#]+")

# Rule 8's "generic" internship: the posting the rule exists to rule out at a large
# non-technology employer.
_GENERIC_TITLE = re.compile(
    r"\b(?:software|it|information\s+technology|technology|tech|cyber\w*|network\w*|systems?"
    r"|web|developer|engineer\w*|co-?op|data\s+analyst|business\s+analyst|analyst)\b",
    re.IGNORECASE)
# What rule 8 keeps at any employer, or what makes a title more than generic.
_SPECIAL_TITLE = re.compile(
    r"machine\s+learning|\bml\b|\bai\b|research|quant\w*|scien\w*|algorithm\w*|first|freshm"
    r"|sophomore|second.?year|underclass|discovery|insight|explore|program|fellow|scholar"
    r"|rotation|leadership|trading|mathemat\w*|statistic\w*",
    re.IGNORECASE)
_UNDERCLASS_TEXT = re.compile(
    r"first.?year|1st.?year|freshm[ae]n|sophomore|second.?year|underclass"
    r"|all\s+(?:class\s+)?years|any\s+(?:class\s+)?year",
    re.IGNORECASE)


def employer_and_title(change: models.Change, source: dict[str, str]) -> tuple[str, str]:
    """The employer and the role, whichever list the row came from.

    Aggregator rows are keyed "Employer / Role @ Place". A board's own rows are keyed
    "Role @ Place", so the employer comes from the board the posting link names, then
    from the source's own label.
    """
    key = _MD_LINK.sub(r"\1", change.key)
    if " / " in key:
        employer, role = (part.strip() for part in key.split(" / ", 1))
    else:
        boards = coretext.ats_boards(change.posting_url, change.url)
        employer = boards[0][1] if boards else (
            source.get("program_names") or change.program_name or change.source_id)
        role = key
    head, sep, _place = role.rpartition(" @ ")
    return employer, (head if sep else role).strip()


def _place(change: models.Change) -> str:
    _head, sep, place = _MD_LINK.sub(r"\1", change.key).rpartition(" @ ")
    return place.strip() if sep else ""


def normalise_employer(name: str) -> str:
    words = _WORD.findall(_PARENTHETICAL.sub(" ", name.lower()))
    while words and words[-1] in _EMPLOYER_SUFFIX:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return "".join(words)


def normalise_title(title: str) -> str:
    return " ".join(w for w in _WORD.findall(title.lower()) if not _TITLE_NOISE.match(w))


def posting_key(change: models.Change, source: dict[str, str]) -> str:
    """"employer|title", or "" for a change that is not one posting: a page diff, a
    collapse notice, or a row with no employer or title left after normalising."""
    if change.structural or (source.get("method") or "").strip() == "page_text":
        return ""
    employer, title = employer_and_title(change, source)
    employer, title = normalise_employer(employer), normalise_title(title)
    return f"{employer}|{title}" if employer and title else ""


def _usable(row: dict[str, str], today: str, cycle: int) -> bool:
    try:
        age = (datetime.date.fromisoformat(today)
               - datetime.date.fromisoformat(row.get("judged_on") or "")).days
    except ValueError:
        return False
    return str(cycle) == (row.get("cycle") or "") and 0 <= age <= REUSE_DAYS


def index(rows: list[dict[str, str]], today: str, cycle: int) -> dict[str, dict[str, str]]:
    """The stored judgments that may be reused today, by posting key."""
    return {row["key"]: row for row in rows if row.get("key") and _usable(row, today, cycle)}


def employer_verdicts(usable: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Employer -> a rule-8 rule-out to reuse, for employers the model has ruled out as
    large non-technology firms and never kept a posting at."""
    kept: set[str] = set()
    ruled: dict[str, dict[str, str]] = {}
    for row in usable.values():
        employer = row["key"].split("|", 1)[0]
        if row.get("relevant") == "true":
            kept.add(employer)
        elif row.get("ruled_out_by") == "value" and row.get("employer_size") == "large":
            ruled.setdefault(employer, row)
    return {employer: row for employer, row in ruled.items() if employer not in kept}


def employer_rule_out(
    change: models.Change, source: dict[str, str], posting_text: str,
    verdicts: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    """The stored rule-8 verdict that settles this change, or None."""
    employer, title = employer_and_title(change, source)
    verdict = verdicts.get(normalise_employer(employer))
    if verdict is None:
        return None
    if not _GENERIC_TITLE.search(title) or _SPECIAL_TITLE.search(title):
        return None
    if _UNDERCLASS_TEXT.search(posting_text or "") or _UNDERCLASS_TEXT.search(title):
        return None
    return verdict


def may_reuse(row: dict[str, str], has_text_now: bool) -> bool:
    """A title-only judgment is not reused once the posting's text can be read."""
    return row.get("had_text") == "true" or not has_text_now


def reused(change: models.Change, row: dict[str, str]) -> models.Judgment:
    """A stored judgment applied to a new change, keeping the change's own location."""
    return models.Judgment(
        change=change, outcome=models.REUSED, program_name=change.program_name,
        relevant=row.get("relevant") == "true", why=row.get("why", ""),
        confidence=row.get("confidence") or "low",
        ruled_out_by=row.get("ruled_out_by", ""), class_year=row.get("class_year", ""),
        location=_place(change) or row.get("location", ""),
        deadline=row.get("deadline", ""), employer_size=row.get("employer_size", ""),
        reused_on=row.get("judged_on", ""),
    )


def as_row(judgment: models.Judgment, key: str, employer: str, title: str,
           had_text: bool, today: str, cycle: int) -> dict[str, str]:
    return {
        "key": key, "employer": employer, "title": title,
        "relevant": "true" if judgment.relevant else "false",
        "ruled_out_by": judgment.ruled_out_by, "why": judgment.why,
        "class_year": judgment.class_year, "location": judgment.location,
        "deadline": judgment.deadline, "employer_size": judgment.employer_size,
        "confidence": judgment.confidence, "had_text": "true" if had_text else "false",
        "cycle": str(cycle), "judged_on": today, "last_used": today,
    }


def merge(
    rows: list[dict[str, str]],
    judgments: list[models.Judgment],
    sources: dict[str, dict[str, str]],
    has_text: dict[str, bool],
    today: str,
    cycle: int,
) -> list[dict[str, str]]:
    """data/judgments.csv after this run: new model judgments in, reuse dates refreshed,
    rows past KEEP_DAYS or from another cycle out. `has_text` is by change_id."""
    by_key = {row["key"]: dict(row) for row in rows if row.get("key")}
    for judgment in judgments:
        source = sources.get(judgment.change.source_id, {})
        key = posting_key(judgment.change, source)
        if not key:
            continue
        if judgment.outcome == models.MODEL:
            employer, title = employer_and_title(judgment.change, source)
            by_key[key] = as_row(judgment, key, employer, title,
                                 has_text.get(judgment.change.change_id, False), today, cycle)
        elif judgment.outcome == models.REUSED and key in by_key:
            by_key[key]["last_used"] = today

    def keep(row: dict[str, str]) -> bool:
        try:
            age = (datetime.date.fromisoformat(today)
                   - datetime.date.fromisoformat(row.get("last_used") or "")).days
        except ValueError:
            return False
        return row.get("cycle") == str(cycle) and age <= KEEP_DAYS

    return [by_key[key] for key in sorted(by_key) if keep(by_key[key])]
