"""The tier grid: SS, S, A, B, or ruled out, from labels the classifier reports.

Pure, so the classify stage (which applies the rule-outs before the digest is built) and
`deliver.urgency` (which prints the tier) read the same table.

Until 2026-11-20 the model made the value call itself (prompt rule 8) and the digest had
two tiers keyed on employer size. That ruled out D. E. Shaw as a "large finance
employer", kept Little Caesars, and could not express the user's priorities: quant and
ML/AI above software engineering, and the firm's field as well as its size. Now the model
labels each posting -- what field the firm is in, what field the role is in, how big the
firm is -- and this table decides. A label is checkable on the row; a value judgment
made fresh on every posting was not, and could differ between two postings at the same
employer.

    firm \\ role               quant / ML-AI    SWE-group    adjacent    out of field
    target (quant, ML/AI)     SS small, S      S            as target   ruled out
    near-target (big tech)    S                S            ruled out   ruled out
    other tech or finance     A small, B       A small, B   ruled out   ruled out
    non-tech                  B (ML-AI, data)  ruled out    ruled out   ruled out

The SWE group includes data science and, for now, cybersecurity. Cybersecurity is a
target field for the user but not one a first-year can apply to yet, so it is tiered
with SWE until the rules are rewritten for a sophomore (see `profile.RULES_CYCLE`); moving
it is a one-line change to the groups below. Postings built for first- or second-years
are SS, after the same field check.
"""
from __future__ import annotations

import re

from core import models

SS, S, A, B = "SS", "S", "A", "B"
TIER_ORDER = (SS, S, A, B)

# Firm field labels, as the classifier returns them.
# Named for what they mean rather than "target"/"near_target": a company called Target
# was labelled with the wrong field when the label shared its name (2026-11-20).
FIRM_TARGET = "quant_or_ai"       # quant trading/research, or ML/AI as the main business
FIRM_NEAR_TARGET = "big_tech"     # big tech and AI-heavy large companies (a closed list)
FIRM_TECH_FINANCE = "tech_or_finance"
# "other_industry", not "non_tech": the model read "non_tech" as "not a technology
# company" and filed banks and asset managers there (2026-11-20).
FIRM_NON_TECH = "other_industry"
FIRM_FIELDS = (FIRM_TARGET, FIRM_NEAR_TARGET, FIRM_TECH_FINANCE, FIRM_NON_TECH)

# Role field labels.
ROLE_QUANT = "quant"
ROLE_ML_AI = "ml_ai"
ROLE_SWE = "swe"
ROLE_DATA_SCIENCE = "data_science"
ROLE_CYBERSECURITY = "cybersecurity"
ROLE_ADJACENT = "adjacent"        # QA, operations, product, solutions: in-field only at a target firm
ROLE_OUT_OF_FIELD = "out_of_field"  # analytics/BI, HR, marketing, sales, hardware, non-software engineering
ROLE_FIELDS = (ROLE_QUANT, ROLE_ML_AI, ROLE_SWE, ROLE_DATA_SCIENCE, ROLE_CYBERSECURITY,
               ROLE_ADJACENT, ROLE_OUT_OF_FIELD)

TARGET_ROLES = frozenset({ROLE_QUANT, ROLE_ML_AI})
# Tiered with software engineering. Cybersecurity sits here until the rules are rewritten
# for a sophomore, when it moves to TARGET_ROLES.
SWE_GROUP = frozenset({ROLE_SWE, ROLE_DATA_SCIENCE, ROLE_CYBERSECURITY})
# Roles a non-tech employer is kept for, at B: AI/ML, quant and data work.
NON_TECH_KEPT = frozenset({ROLE_QUANT, ROLE_ML_AI, ROLE_DATA_SCIENCE})

SIZE_BANDS = ("<100", "100-1000", "1000-10000", ">10000")
SMALL_BANDS = frozenset({"<100", "100-1000"})

# Reasons printed beside the tier, best first. A row's place in this list is its sort
# key within its tier.
FRESHMAN = "freshman programme"
DISCOVERY = "discovery programme"
TARGET_SMALL = "quant/ML role, small firm"
TARGET_LARGE = "quant/ML firm"
TARGET_FIRM_SWE = "SWE at a quant/ML firm"
NEAR_TARGET = "near-target firm"
SMALL_FIRM = "small firm"
LARGE_FIRM = "large firm"
NON_TECH_ROLE = "AI/data role at a non-tech firm"
UNVERIFIED = "unverified"
REASON_ORDER = (FRESHMAN, DISCOVERY, TARGET_SMALL, TARGET_LARGE, TARGET_FIRM_SWE,
                NEAR_TARGET, SMALL_FIRM, LARGE_FIRM, NON_TECH_ROLE, UNVERIFIED)

# RULED OUT reasons the grid prints, a few words each.
OUT_NON_TECH = "non-tech employer"
OUT_FIELD = "role outside target fields"


def is_small(judgment: models.Judgment) -> bool:
    """Startup or growth stage. An employer the classifier does not know is small: an
    unfamiliar name is usually not a big company, so only a well-known firm of 1,000+
    staff is large. A company on a VC portfolio board is small."""
    if judgment.change.backers:
        return True
    if judgment.employer_size in ("small", "large"):
        return judgment.employer_size == "small"
    return size_label(judgment.size_band, judgment.well_known) == "small"


LARGE_BANDS = frozenset({"1000-10000", ">10000"})


def size_label(band: str, well_known: bool) -> str:
    """`small` or `large`. Large needs both: an established company and a stated
    headcount of 1,000 or more. An unknown headcount counts as small, like an unfamiliar
    name -- a small trading firm the model has heard of is still small."""
    return "large" if (well_known and band in LARGE_BANDS) else "small"


def place(judgment: models.Judgment) -> tuple[str, str] | tuple[None, str]:
    """(tier, reason) for a posting, or (None, rule-out reason).

    Missing labels never rule a posting out: an unknown firm field reads as other tech or
    finance, and an unknown role field as software engineering, so a label the model left
    blank costs a lower tier rather than the row.
    """
    firm = judgment.firm_field if judgment.firm_field in FIRM_FIELDS else FIRM_TECH_FINANCE
    role = judgment.role_field if judgment.role_field in ROLE_FIELDS else ROLE_SWE
    small = is_small(judgment)

    if role == ROLE_OUT_OF_FIELD:
        return None, OUT_FIELD
    if firm == FIRM_TARGET:
        if role in TARGET_ROLES or role == ROLE_ADJACENT:
            return (SS, TARGET_SMALL) if small else (S, TARGET_LARGE)
        return S, TARGET_FIRM_SWE
    if role == ROLE_ADJACENT:
        return None, OUT_FIELD
    if firm == FIRM_NEAR_TARGET:
        return S, NEAR_TARGET
    if firm == FIRM_NON_TECH:
        return (B, NON_TECH_ROLE) if role in NON_TECH_KEPT else (None, OUT_NON_TECH)
    return (A, SMALL_FIRM) if small else (B, LARGE_FIRM)


# A title that names analyst or analytics work is reporting, not data science, unless
# it also names science, quant or security work. Measured 2026-11-20: the model read a
# "Data Analyst, Intern" posting as data science twice running, while "Data Scientist -
# Product Analytics" and "Data Science & Analytics" are data science.
_ANALYST_TITLE = re.compile(r"\banalyst\b|\banalytics\b", re.IGNORECASE)
_NOT_REPORTING = re.compile(
    r"scien|quant|secur|cyber|machine learning|\bml\b|\bai\b|engineer", re.IGNORECASE)


# The big-tech firms. Kept here rather than trusted to the prompt: on held-out checks on
# 2026-11-20 the model labelled Uber big tech and Tesla and Samsung not, with the list in
# the prompt each time. An employer is big tech when its whole name is one of these, or
# when it starts with one and every other word is a subsidiary word ("Samsung Research
# America", "Amazon Web Services") -- so "Apple Bank" is not Apple.
BIG_TECH = frozenset({
    "microsoft", "linkedin", "github",
    "google", "alphabet", "googledeepmind", "deepmind", "waymo", "youtube",
    "meta", "metaplatforms",
    "amazon", "amazonwebservices", "aws", "zoox",
    "apple",
    "nvidia",
    "samsung",
    "tesla",
    "toyotaresearchinstitute", "tri",
    "anduril",
    "palantir",
})
_BIG_TECH_FIRST = frozenset({
    "microsoft", "linkedin", "github", "google", "alphabet", "deepmind", "waymo",
    "youtube", "meta", "amazon", "zoox", "apple", "nvidia", "samsung", "tesla",
    "anduril", "palantir",
})
_SUBSIDIARY_WORDS = frozenset({
    "research", "america", "americas", "electronics", "semiconductor", "semiconductors",
    "labs", "lab", "ai", "cloud", "web", "services", "platforms", "reality", "devices",
    "robotics", "deepmind", "technologies", "industries", "us", "usa", "energy", "motors",
    "inc", "llc", "corp", "corporation", "co", "company", "the",
})


def is_big_tech(employer: str) -> bool:
    words = re.findall(r"[a-z0-9]+", employer.lower())
    if "".join(w for w in words if w not in ("inc", "llc", "corp", "corporation", "the")) \
            in BIG_TECH:
        return True
    return bool(words) and words[0] in _BIG_TECH_FIRST \
        and all(w in _SUBSIDIARY_WORDS for w in words[1:])


# A name that says it is a bank or an investment firm is finance. The model filed two
# banks under other_industry on 2026-11-20 even after the label was renamed.
_FINANCE_NAME = re.compile(
    r"\b(?:bank|banks|bancorp|bancshares|securities|investments|asset management"
    r"|credit union|capital markets)\b", re.IGNORECASE)


def guard_firm(firm: str, employer: str) -> str:
    """Big tech is decided by name: a listed employer is big tech whatever the model
    said, and an unlisted one the model called big tech is other tech. A bank or
    investment firm by name is never other_industry."""
    if is_big_tech(employer):
        return FIRM_NEAR_TARGET
    if firm == FIRM_NEAR_TARGET:
        return FIRM_TECH_FINANCE
    if firm == FIRM_NON_TECH and _FINANCE_NAME.search(employer):
        return FIRM_TECH_FINANCE
    return firm


def guard_role(role: str, title: str) -> str:
    """`out_of_field` for an analyst title the model labelled data science."""
    if role == ROLE_DATA_SCIENCE and _ANALYST_TITLE.search(title) \
            and not _NOT_REPORTING.search(title):
        return ROLE_OUT_OF_FIELD
    return role


def order(reason: str) -> int:
    return REASON_ORDER.index(reason) if reason in REASON_ORDER else len(REASON_ORDER)


def is_page_change(change: models.Change, source: dict[str, str]) -> bool:
    """A change with nothing to apply to: a new section heading, a collapse notice, or a
    programme page that changed. These go in the digest's page-changes list rather than
    the table, because the firm's tier would otherwise put a link-less row at the top.
    A job list watched as page text (`job_list=true`) is keyed on postings, so its rows
    are tiered like any posting."""
    if change.structural or change.key.startswith("new section:"):
        return True
    method = (source.get("method") or "").strip()
    return method == "page_text" and (source.get("job_list") or "").strip() != "true"
