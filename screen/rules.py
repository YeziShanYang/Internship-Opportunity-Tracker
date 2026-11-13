"""Deterministic pre-screen: rule out what a quoted phrase settles (spec section 8).

Every change used to reach the model. Measured on the 2026-09-12 run, 40 of 77 were
ruled out, and reading the reasons shows most of them turned on a single sentence the
employer had already written plainly -- "Expected graduation date: 2027 or 2028",
"entering junior-level standing", "Pursuing a Master's or Ph.D. degree". Paying a
reasoning model to re-derive that is the expensive way to read a regex.

The design is borrowed from zshah101's `sponsorship.py`: phrase-anchored patterns of
what employers actually write, precision favoured hard over recall, and a `VERSION`
that invalidates stored verdicts when the rules change.

Three properties are load-bearing, in this order:

1. **This module may only rule OUT, never rule IN.** A change it has no opinion on goes
   to the model exactly as before. Nothing is dropped for being unrecognised, which is
   the same commitment spec 8 rule 3 makes about a missing API key.
2. **A rule-out must quote the phrase that caused it.** The reason lands in RULED OUT
   where it can be audited, so a wrong rule reads as a wrong rule rather than as an
   absence. A rule that cannot quote its evidence does not belong here.
3. **Absence of evidence is never evidence.** No posting text means no class-year
   rule-out -- the same instruction the prompt gives the model when a fetch failed.
   A gate the employer did not state cannot be inferred from silence.

The owner is a Stanford freshman, graduating June 2030. For a Summer 2027 internship that
makes a rising sophomore, so "rising sophomore" is a *match* and only junior-and-above
standing excludes. That asymmetry is why every standing rule carries a sophomore guard.

A graduation window counts against the owner when it closes before June 2030, which since
2026-09-30 includes 2029: any stated graduation range that does not include June 2030
rules the posting out. A 2029 window is the sophomore band (SIG's Discovery Programs, "winter
2028 or spring 2029"), which is exactly the label-versus-window trap that made 2029 look
inclusive before.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from process import parse_ats

# Bump when a rule changes. Stored on each verdict, so a rule fix re-screens the
# backlog instead of applying only to changes seen afterwards.
VERSION = 4

# The graduation years that exclude this owner, and the ones that include them. A window
# naming both ("graduating between 2027 and 2030") is inclusive and must not rule out.
EXCLUDING_YEARS = ("2027", "2028", "2029")
INCLUDING_YEARS = ("2030", "2031", "2032")

# Anchors that mark a year as a *graduation* year rather than the internship's own
# season. This distinction is the whole game: a "Software Engineer Intern - Summer 2027"
# posting says 2027 in its title, its body and its start date without saying anything
# whatsoever about when the applicant graduates.
_GRAD_ANCHOR = (
    r"(?:graduat\w*|class\s+of|commencement|degree\s+completion"
    r"|(?:obtain|receive|complete|earn)\w*\s+(?:your\s+|their\s+|a\s+|an\s+)?"
    r"(?:bachelor|master|undergraduate|b\.?s\.?|b\.?a\.?)\w*"
    r"|commit\s+to\s+future\s+full[\s-]?time\s+employment)"
)

# Anchor then year, or year then anchor. The gap is deliberately short and may not cross
# a sentence boundary, so "graduating soon. Internship runs Summer 2027" cannot match.
_GRAD_YEAR = re.compile(
    rf"(?:{_GRAD_ANCHOR}[^.!?;]{{0,70}}?\b(?:{'|'.join(EXCLUDING_YEARS)})\b"
    rf"|\b(?:{'|'.join(EXCLUDING_YEARS)})\b[^.!?;]{{0,45}}?{_GRAD_ANCHOR})",
    re.IGNORECASE,
)

# Class standing at or above junior. "Rising sophomore" is this owner's own status for
# Summer 2027, so it never appears here.
_ADVANCED_STANDING = re.compile(
    r"("
    r"rising\s+(?:junior|senior)"
    r"|entering\s+(?:your\s+)?junior(?:[\s-]level)?\s*(?:year|standing)?"
    r"|(?:junior|senior)[\s-]level\s+standing"
    r"|junior\s+or\s+senior\s+standing"
    r"|juniors?\s+(?:and|or)\s+seniors?\s+only"
    r"|penultimate\s+year"
    r"|(?:third|fourth|3rd|4th)[\s-]+(?:or\s+(?:fourth|4th)[\s-]+)?year\s+(?:student|undergraduate)"
    r"|(?:must\s+be|currently)\s+(?:a\s+)?(?:third|fourth)[\s-]year"
    r")",
    re.IGNORECASE,
)

# If any of these appear in the same sentence, the posting is enumerating years it
# accepts rather than a floor it requires -- "open to rising sophomores, juniors and
# seniors" includes this owner.
_STANDING_GUARD = re.compile(
    r"(?:sophomore|first[\s-]year|freshman|freshmen|any\s+(?:class\s+)?year"
    r"|all\s+(?:class\s+)?years|undergraduates?\s+of\s+all)",
    re.IGNORECASE,
)

# A programme open only to graduate students. Guarded on "bachelor", because
# "Bachelor's, Master's or PhD in a technical field" is the common inclusive phrasing.
_GRADUATE_ONLY = re.compile(
    r"(?:pursuing|enrolled\s+in|working\s+toward(?:s)?|candidates?\s+for)\s+"
    r"(?:a\s+|an\s+)?(?:master|ph\.?\s?d|m\.?s\.?|doctora)\w*",
    re.IGNORECASE,
)
_BACHELOR_GUARD = re.compile(r"bachelor|undergraduate|b\.?s\.?\b|b\.?a\.?\b", re.IGNORECASE)

# A year this owner can actually graduate in, appearing near the match, means the
# employer is enumerating an acceptable range rather than a ceiling.
_INCLUDING_YEAR = re.compile(r"\b(?:" + "|".join(INCLUDING_YEARS) + r")\b")

# Already holds the degree, so an undergraduate cannot apply at all.
_ALREADY_GRADUATED = re.compile(
    r"must\s+have\s+(?:already\s+)?(?:graduated|completed\s+(?:a|your|their)\s+"
    r"(?:bachelor|undergraduate)\w*)",
    re.IGNORECASE,
)

# Title-level fields plainly outside quant / maths / software, mirroring prompt rule 7.
_OUT_OF_FIELD = re.compile(
    r"\b(?:public\s+relations|human\s+resources|talent\s+acquisition|recruit(?:ing|er|ment)"
    r"|marketing|communications|social\s+media|brand|copywrit\w+|graphic\s+design"
    r"|change\s+management|sales\s+development|account\s+executive|customer\s+success"
    r"|payroll|benefits\s+administration|facilities|janitorial|warehouse)\b",
    re.IGNORECASE,
)

# A title naming sophomores or second-years and not first-years. The owner is a first-year,
# so "Associate Software Engineer - Sophomore Intern Summer 2027" (Thrivent, pinned into
# the top of the digest on 2026-10-08) is a class-year rule-out. "Rising sophomore" is the
# exception: for a Summer 2027 role that is this owner's own standing.
_SOPHOMORE = re.compile(r"\bsophomores?\b|\bsecond[\s-]?year\b|\b2nd[\s-]?year\b", re.IGNORECASE)
_FIRST_YEAR = re.compile(
    # A bare "first" rather than "first-year": "First- & Second-Year Undergrads" names
    # both years with the hyphen left hanging.
    r"freshm[ae]n|\bfirst\b|\b1st\b|underclass|rising\s+sophomore",
    re.IGNORECASE,
)


def sophomore_only(text: str) -> str | None:
    """The sophomore phrase in `text` when it names sophomores and not first-years."""
    hit = _SOPHOMORE.search(text or "")
    if hit and not _FIRST_YEAR.search(text):
        return hit.group(0)
    return None


# Rule 7's own caveat: when a role is technical at all, keep it. "Marketing Data
# Scientist" and "Recruiting Software Engineer" are in scope despite the first word.
_TECHNICAL = re.compile(
    r"\b(?:software|engineer\w*|developer|quant\w*|trading|trader|research\w*"
    r"|data|machine\s+learning|\bml\b|\bai\b|mathematic\w*|statistic\w*|algorithm\w*"
    r"|infrastructure|backend|front[\s-]?end|full[\s-]?stack|security|systems|platform"
    r"|analytics|scientist)\b",
    re.IGNORECASE,
)

# The guard that matters most, and the one whose absence produced every false positive
# in the first draft of this module. An employer naming 2027 is not necessarily naming a
# ceiling: "Expected graduation date of November 2027 or later", "graduating December
# 2027 and beyond" and "graduating after August 2027" are all open-ended upward and all
# include a 2030 graduate. Measured on the 2026-09-12 set, these three phrasings were
# the only wrong rule-outs the screen made, and they were all of them.
_OPEN_ENDED = re.compile(
    r"(?:or\s+(?:later|after|beyond|thereafter|subsequent\w*|following)"
    r"|and\s+(?:later|beyond|after|onward\w*)"
    r"|onward\w*"
    r"|no\s+earlier\s+than"
    r"|\bafter\s+(?:\w+\s+){0,2}20\d\d"
    r"|\bbeyond\s+20\d\d"
    r"|20\d\d\s*\+"
    r"|20\d\d\s*(?:or|-|–|to)\s*(?:later|beyond|present))",
    re.IGNORECASE,
)

# How far either side of a match the guards look. Deliberately generous: a missed
# rule-out costs one model call, a wrong rule-out costs a real opportunity. Windows are
# used rather than sentences because several ATS strip punctuation entirely -- two of
# the first draft's false positives came from a whole posting parsing as one "sentence".
_GUARD_BEFORE, _GUARD_AFTER = 50, 75


@dataclass(frozen=True)
class Verdict:
    """A deterministic rule-out. There is no such thing as a deterministic rule-in."""

    why: str
    rule: str
    version: int = VERSION
    # Which change this settled. Redundant with the key of the table that holds it, and
    # worth the duplication: `.run/screened.json` is read by a human wondering why a row
    # was ruled out, and an entry that does not name its own row is a puzzle.
    change_id: str = ""


def _quote(text: str, limit: int = 180) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed if len(collapsed) <= limit else collapsed[:limit].rsplit(" ", 1)[0] + "..."


def _context(text: str, start: int, end: int) -> str:
    return text[max(0, start - _GUARD_BEFORE):end + _GUARD_AFTER]


def _excluded_by(pattern, text: str, *, guards=()) -> str | None:
    """The first match of `pattern` whose context no guard vetoes, else None.

    Returns the matched phrase itself rather than the surrounding sentence, so the
    reason in RULED OUT quotes the evidence and nothing else.
    """
    for hit in pattern.finditer(text):
        context = _context(text, hit.start(), hit.end())
        if any(guard.search(context) for guard in guards):
            continue
        return hit.group(0)
    return None


# Rule 7's engineering that is not software, named in a job title. Added 2026-11-13 to
# take calls off the model: over the 30 digests to that date the model ruled out about
# 500 rows as hardware, mechanical, civil or electrical, usually on the title alone, and
# the existing title rule could not reach them because `_TECHNICAL` treats any
# "engineer" as technical. Scored against those 30 days before adoption: every row this
# fires on that the model had kept contradicted rule 7 itself (a civil co-op, a hardware
# verification role). Firmware, embedded and design-verification titles are left to
# the model; they sit too close to software to settle with a regex.
_NON_SOFTWARE_ENGINEERING = re.compile(
    r"\b(?:mechanical|electrical|civil|chemical|aerospace|manufacturing|hardware|structural"
    r"|industrial\s+engineer\w*|mechatronics|materials\s+(?:science|engineer\w*)|metallurg\w*"
    r"|asic|fpga|pcb|rf|analog|mixed[\s-]signal|power\s+electronics|thermal|photonics|nuclear"
    r"|environmental|biomedical|petroleum|mining|geotechnical|construction|packaging|propulsion"
    r"|avionics|composites|cryogenics|electromechanical|hvac|welding|tooling"
    r"|process\s+engineer\w*|quality\s+engineer\w*|test\s+engineer\w*|validation\s+engineer\w*"
    r"|vlsi|digital\s+(?:ic|circuit)|circuit\s+design|ic\s+design|design\s+for\s+test"
    r"|transportation\s+engineer\w*|traffic|roadway|surveying)\b",
    re.IGNORECASE,
)
# Anything software-shaped in the same title keeps it for the model: "Software Engineer,
# Manufacturing Systems", "Site Reliability Engineer", "ASIC EDA Infrastructure".
_SOFTWARE_GUARD = re.compile(
    r"\b(?:software|data|machine\s+learning|ml|ai|algorithm\w*|quant\w*|computer\s+science"
    r"|computer\s+engineer\w*|computing|developer|full[\s-]?stack|back[\s-]?end|front[\s-]?end"
    r"|web|cloud|devops|cyber\w*|security|research\s+scien\w*|scientist|applications?|app"
    r"|platform|automation|perception|autonomy|simulation|analytics|product\s+manag\w*|it"
    r"|information\s+technology|site\s+reliability|sre|eda|infrastructure|career\s+fair)\b"
    r"|\(\+\d+\s+more\)",
    re.IGNORECASE,
)

# A row naming several places, or remote work, may include the United States; only a
# single clearly foreign location settles it.
_SEVERAL_PLACES = re.compile(r"\+\s*\d+\s+more|\d+\s+locations|\bremote\b", re.IGNORECASE)


def _role_and_location(key: str) -> tuple[str, str]:
    """"Firm / Role @ Place" -> ("Role", "Place"). The firm is dropped so a company
    called "GE Aerospace" cannot make its data-analyst posting read as aerospace."""
    role = key.split(" / ", 1)[1] if " / " in key else key
    head, sep, place = role.rpartition(" @ ")
    return (head, place.strip()) if sep else (role, "")


def screen_location(key: str) -> Verdict | None:
    """The profile's one location test: a role recruiting only outside the United States.

    Uses the job boards' own location patterns, so an aggregator row is judged exactly
    as an ATS row already is before it is ever diffed. A US match wins, as it does there.
    Added 2026-11-13: the model ruled out 173 rows as "Canada only" in the preceding 30
    days, each a call spent reading a location the row already stated.
    """
    _role, place = _role_and_location(key)
    if not place or _SEVERAL_PLACES.search(place):
        return None
    if parse_ats.US_LOCATION.search(place) or not parse_ats.NON_US_LOCATION.search(place):
        return None
    return Verdict(
        why=f'Location is outside the United States ("{place}"), and this owner can only '
            f"take a US role: {_quote(key)}",
        rule="non-us-location",
    )


def screen_title(title: str) -> Verdict | None:
    """Rules 6 and 7, on the title alone. Safe without any posting text."""
    sophomore = sophomore_only(title)
    if sophomore:
        return Verdict(
            why=f'Title is for sophomores ("{sophomore}"), and this owner is a first-year: '
                f"{_quote(title)}",
            rule="sophomore-title",
        )
    hit = _OUT_OF_FIELD.search(title)
    if hit and not _TECHNICAL.search(title):
        return Verdict(
            why=f'Title names a field outside quant/maths/software ("{hit.group(0)}") '
                f"and nothing technical: {_quote(title)}",
            rule="out-of-field-title",
        )
    role, _place = _role_and_location(title)
    hit = _NON_SOFTWARE_ENGINEERING.search(role)
    if hit and not _SOFTWARE_GUARD.search(role):
        return Verdict(
            why=f'Title names engineering that is not software ("{hit.group(0)}"): '
                f"{_quote(title)}",
            rule="engineering-field-title",
        )
    return None


def screen_posting(text: str) -> Verdict | None:
    """Rule 6, on the posting text. Every rule here quotes the sentence it fired on."""
    if not text or not text.strip():
        return None  # absence of evidence is never evidence

    hit = _excluded_by(_GRAD_YEAR, text, guards=(_OPEN_ENDED, _INCLUDING_YEAR))
    if hit:
        return Verdict(
            why=f'Posting states a graduation window that closes before June 2030: '
                f'"{_quote(hit)}"',
            rule="graduation-window",
        )

    hit = _excluded_by(_ALREADY_GRADUATED, text)
    if hit:
        return Verdict(
            why=f'Posting requires an already-completed degree: "{_quote(hit)}"',
            rule="already-graduated",
        )

    hit = _excluded_by(_ADVANCED_STANDING, text, guards=(_STANDING_GUARD,))
    if hit:
        return Verdict(
            why=f'Posting requires junior standing or above; this owner is a rising '
                f'sophomore for Summer 2027: "{_quote(hit)}"',
            rule="advanced-standing",
        )

    hit = _excluded_by(_GRADUATE_ONLY, text, guards=(_BACHELOR_GUARD,))
    if hit:
        return Verdict(
            why=f'Posting is for graduate students only: "{_quote(hit)}"',
            rule="graduate-only",
        )

    return None


def screen(title: str, posting_text: str = "") -> Verdict | None:
    """The whole screen. None means "no opinion" and the change goes to the model."""
    return screen_title(title) or screen_location(title) or screen_posting(posting_text)
