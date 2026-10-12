"""Relevance judgment for observed changes (spec section 8).

One model call per change that survives triage. The owner profile is passed verbatim
because the class-year and identity gates are the whole point: most of what these
sources surface is aimed at juniors or at groups this user is not part of, and reading
that off a posting is exactly what an LLM is good at and a regex is not.

Two providers are supported, and the prompt, the schema and the `Judgment` contract are
identical across both -- only the transport differs:

* **Anthropic** (`ANTHROPIC_API_KEY`), running Claude. Preferred whenever it is
  configured, because the system prompt above was written and tuned against it.
* **Azure OpenAI** (`AZURE_OPENAI_API_KEY`), running a `gpt-5-mini` deployment on a
  Microsoft Foundry resource. This exists because Azure for Students grants $100 of
  credit that covers first-party OpenAI models but grants *zero* deployment quota for
  Claude on Azure -- Claude there is Marketplace-billed, and Marketplace purchases are
  excluded from student credit. gpt-5-mini is a small-tier model, weaker than Sonnet;
  it is accepted here because reading a graduation window or an identity gate off
  posting text is a far easier task than the benchmarks that separate the tiers.

Three deliberate safety properties:

* Without any API key, nothing is dropped. Every change is emitted unclassified into
  WORTH A LOOK. Spec section 8 rule 3 is explicit that a false negative -- FTTP opens
  and the tool calls it noise -- is the failure that costs real money, so the
  degraded mode surfaces more, not less. Adding a second provider must not weaken
  this: an unreachable provider degrades exactly like a missing key.
* The `eligible` column is never rewritten. A proposed change is logged and reported
  (rule 5); hand-verified research is not overwritten by a model.
* A provider failure is never silently swallowed. Every failure path returns a
  `Judgment` carrying `error`, so the digest reports it rather than showing a change
  as benignly unclassified (spec 10.1: a failure must never look like a quiet day).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import threading

import screen
from core import models, paths, profile, text as coretext
from core.models import Judgment, Usage  # re-exported: the shapes moved to core
from enrich import bodies as enrich_bodies
from persist import store

MODEL = "claude-opus-5"

# The Azure side is pinned to the Foundry deployment created for this project. Both are
# overridable by env so the repo is not welded to one resource, but defaulting them here
# means the workflow needs exactly one new secret (the key) rather than three.
AZURE_DEFAULT_DEPLOYMENT = "gpt-5-mini"
AZURE_DEFAULT_ENDPOINT = "https://opptracker-ai-jshi.openai.azure.com/"
AZURE_DEFAULT_API_VERSION = "2024-12-01-preview"

# Reasoning effort, split by task because the two tasks are not the same difficulty.
#
# Measured on the 2026-09-12 run (80 calls, $0.34): input was 245K tokens costing
# $0.064, so 81% of the bill was output, and at effort="low" the implied output was
# ~1,727 tokens per call against a ~150-token JSON answer. Almost all of the money is
# reasoning tokens, which is why this is the lever that matters and the 12K posting-text
# budget is not -- trimming that is worth 8%.
#
# The daily read is a narrow question against text that states the answer outright:
# does this posting name a graduation window or an identity gate this person fails.
# "minimal" is the bet that this needs recognition rather than deliberation, and the
# 2026-09-15 digest is the test of it.
# Raised from "minimal" on 2026-09-15. Minimal was the right bet while every question
# this prompt asked was a lookup -- does the posting state a graduation window, does it
# name an identity gate -- and the comment below still describes that case. Rule 8 asks
# something else: whether an employer has standing in software, which is recall and
# judgement rather than recognition in the text. On minimal it got Figma, Robinhood and
# Datadog wrong on the first morning it ran, each time by substituting the easier
# question "is this firm quant?" for the one asked. Today's run cost $0.0967 across 136
# calls at minimal, so there is room; if this proves expensive, the better fix is to ask
# the employer question once per employer rather than once per posting, since Figma
# appeared four times in one digest and the answer cannot differ between them.
CLASSIFY_REASONING_EFFORT = "low"


# Anthropic and Azure do not share an effort vocabulary; Anthropic has no "minimal".
ANTHROPIC_EFFORT = {"minimal": "low", "low": "low", "medium": "medium", "high": "high"}

# Bounds the daily bill. Any overflow still reaches the digest, just unclassified.
# Raised from 60 when the backend moved to gpt-5-mini and postings started being
# fetched: a classification costs roughly $0.0015 including ~6K tokens of posting text,
# so even the worst day observed (162 changes) is about $0.25.
MAX_CLASSIFICATIONS_PER_RUN = 250

# The work is network-bound, so a modest pool collapses the wall clock without
# troubling the deployment's 500K tokens/minute quota.
MAX_CONCURRENT_CLASSIFICATIONS = 6

SYSTEM_PROMPT = f"""You judge whether a change on an opportunity-tracking source matters to one specific person.

The person:
{profile.OWNER_PROFILE}

Rules, in priority order:

1. Class-year gates are the primary filter. This person is a freshman graduating in
   June 2030. A posting whose graduation window does not include June 2030 is NOT
   relevant: "Dec 2027 to Aug 2028" is a junior window, "winter 2028 or spring 2029" is a
   sophomore window, and "graduating by December 2029" ends too early. A window that is
   open-ended upward ("December 2027 or later") or that reaches spring 2030 includes
   this person. Most US schools graduate in May and Stanford in June, so "May 2030", "Spring
   2030" and "class of 2030" all mean this person's graduation: never rule a posting out for
   saying May rather than June.
   Graduation-window phrasing shifts every cycle, so read the actual dates rather than
   pattern-matching a job title or a programme label.
2. Identity gates disqualify. Programs for women, underrepresented minorities, LGBTQIA+
   students, or students with barriers to access are relevant: false. Specifically:
   Jane Street INSIGHT and WiSE; Jane Street JSIP, FOCUS and IN FOCUS; Bridgewater
   Rising Fellows; Two Sigma College Mentor Connect; D. E. Shaw Discovery, Latitude and
   Momentum; IMC WiT; Virtu Women's Winternship; SIG "for Women" Discovery Days.
3. Bias toward false positives ON ELIGIBILITY. When the question is whether this person
   *can* apply -- the class year, an identity gate, a requirement you cannot see because
   the posting text is missing -- surfacing a borderline item is nearly free and missing
   a real opening is expensive, so set relevant: true, say so in `why`, and use
   confidence "low". This does not extend to rule 8, which is about whether a role this person
   can plainly apply to is worth their morning.
4. Rolling deadlines are urgent. Jane Street, NVIDIA, D. E. Shaw and Point72 review on
   a rolling basis and close when full; Jane Street slots have reportedly filled by late
   October. Any change on one of those is high priority regardless of stated deadline.
5. Never assert a new value for the owner's hand-maintained `eligible` column. If the
   change implies one, put it in `eligible_proposal` as a suggestion with reasoning.

6. Class-year gates stated on the posting are decisive. When the POSTING TEXT below
   states a requirement this person cannot meet -- "rising junior", "rising senior",
   "penultimate year", "third or fourth year", "junior or senior standing", "completed
   sophomore year", a programme for sophomores or second-years that does not also take
   first-years ("Sophomore Intern", "current sophomores"; "rising sophomore" for a Summer
   2027 role is this person and is NOT a rule-out), a Master's or PhD program, or a
   graduation window that closes before June 2030 (2027, 2028 or 2029) -- set
   relevant: false and
   name the gate in `why` in a few words: "rising juniors only", "master's program". Measured: about a third of these postings carry
   such a gate, so this is the filter that does the most work.
   Absence of a gate is NOT a reason to rule out. "Currently enrolled in a Bachelor's
   degree program" includes a first-year: that is relevant: true.
7. Rule out roles that are clearly outside this person's field. In scope: software
   engineering, quantitative research, trading, mathematics, data/ML, and
   finance-adjacent work. Out of scope, as examples: public relations, human
   resources, marketing, recruiting, change management, IT document automation, and
   general business operations. Also out of scope: engineering that is not software --
   mechanical, electrical, civil, aerospace, chemical, manufacturing, firmware and
   hardware -- because this person studies maths and CS.
8. Weigh the employer by this person's odds, not its fame. Large, established employers
   overwhelmingly fill internships with juniors and seniors, so for this first-year the
   most worthwhile rows are startups, small and mid-size companies, and small trading
   firms -- places where a freshman is a realistic hire -- plus any programme aimed at
   first- and second-year students. Revised 2026-09-30; this
   rule used to do the opposite, keeping famous firms and ruling out small ones.

   Keep:
   * anything quantitative -- trading, quant research, quant development, systematic
     strategy, mathematics, statistics. This is this person's first field and the bar is low.
   * software, data or ML at a startup or a small or mid-size company, whether or not
     you have heard of it. An unfamiliar name is a reason to keep, not to rule out.
   * any role or programme that says it is for first- or second-year students, or open
     to all undergraduate years, at an employer of any size.
   * software, data or ML at a technology company of genuine standing -- Google, Meta,
     Nvidia, OpenAI, Anthropic, Figma, Stripe, Databricks, Datadog, Robinhood, Palantir
     and comparable firms. Do not reason that such a firm is "not quant"; that is not
     the test.
   * a role whose substance is notable even if the employer is not -- compilers,
     operating systems, distributed systems, cryptography, ML infrastructure, research.
   * anything at a firm the owner already tracks as a priority.

   Rule out: a generic "Software Engineer Intern", "IT Intern", "Technology Intern",
   "Co-op" or "Web Developer Intern" at a LARGE non-technology enterprise -- a defence
   contractor, an aerospace or industrial conglomerate, a utility, an insurer, a
   government-services contractor, a retail or manufacturing corporation -- when the
   posting does not say it is open to first- or second-years. Those programmes run
   through junior-year recruiting pipelines. Say what kind of employer it is in `why`,
   in a few words -- "large defence contractor", "large insurer" -- so the call can be
   audited and argued with.

   This is a judgement of value and not of eligibility, so it is the one rule where you
   should NOT err toward keeping. It is safe to be decisive because a ruled-out row is
   still printed, with this reason, in the digest's RULED OUT block. If you genuinely
   cannot tell how large the employer is, keep it.

If the POSTING TEXT is missing or a fetch error is noted, you are judging on a job
title alone. Say so in `why`, use confidence "low", and do NOT rule the item out on
class-year grounds -- you have not seen the requirements.

`why` must name the specific reason -- the class year, the identity gate, the
deadline -- not a generic statement of interest. When relevant is false it is printed
verbatim in the digest's RULED OUT list, so it must be a phrase of two to five words and
never a sentence: "master's program", "rising juniors only", "graduating 2028", "civil
engineering role", "Canada only", "large insurer", "hardware role (title only)". The
phrase only reports the reason; it does not change which items are ruled out. When
relevant is true, `why` is one sentence.

The fields below are read straight off the POSTING TEXT and rendered as their own
bullets in the digest, so each must stand alone without `why` for context. Report what
the posting says, never what you infer, and leave a field empty rather than guessing --
an empty bullet is simply not shown, whereas a wrong one is read as fact:

* `class_year` -- who the posting says may apply, in two to six words and never a
  sentence: "any undergraduate", "grads Dec 2027-May 2030", "rising juniors",
  "first-years only", "Bachelor's or Master's", "Bachelor's, no year stated". A major or
  enrolment requirement is not a class year: "currently enrolled in a CS-related degree"
  is "any undergraduate". This is the field the owner scans first.
* `location` -- where the role sits, as written: "New York, NY", "London, UK",
  "remote", "Chicago or NYC". Several locations is fine; list them.
* `employer_size` -- `small` for a startup or a company of roughly under 1,000
  employees, including a small trading firm: the employers that take first- and
  second-year interns. `large` for a big established employer, and for any firm whose
  internships run through a structured junior-year campus pipeline whatever its
  headcount (Jane Street, Citadel, Hudson River Trading, Google). The empty string when
  you cannot tell. `small` puts a row in the digest's High tier, so do not guess.
* `deadline` -- when applications close. Exactly one of: the word `rolling` when the
  posting says it reviews on a rolling basis or closes when full; an ISO `YYYY-MM-DD`
  date when it states or clearly implies one; or the empty string when it states none.
  Do NOT invent a date, and do not put prose here -- a phrase like "apply early" goes
  in `why`. The deadline is printed for the owner to read, so a guess here is a false
  fact on the row.

Do NOT suggest a next step, an action, or advice. The digest is a list of things to go
and look at, not a plan; "consider applying; seek referrals" is a sentence that
changes nothing on any row, and as its own bullet it cost 231 characters a row on a morning when
nine real opportunities were withheld for want of room.

`ruled_out_by` says which kind of rule fired. It matters only when relevant is false:

* `eligibility` -- rules 1, 2, 6 and 7. This person cannot apply, or the role is not
  in their field. These are checkable against the posting text and have proven reliable, so the
  digest prints them compactly.
* `value` -- rule 8 alone. This person could apply and it is in their field, but the
  employer is a large enterprise whose internship a first-year is unlikely to land. This is a judgement rather than a fact, it is the one that has been
  wrong, and the digest prints these in full so they can be argued with.
* the empty string when relevant is true."""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "program_name": {"type": "string"},
        "new_status": {"type": "string", "enum": list(paths.PROGRAM_STATUSES)},
        "why": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "eligible_proposal": {"type": "string"},
        "ruled_out_by": {"type": "string", "enum": ["", "eligibility", "value"]},
        "class_year": {"type": "string"},
        "location": {"type": "string"},
        "deadline": {"type": "string"},
        "employer_size": {"type": "string", "enum": ["", "small", "large"]},
    },
    "required": [
        "relevant",
        "program_name",
        "new_status",
        "why",
        "confidence",
        "ruled_out_by",
        "class_year",
        "location",
        "deadline",
        "employer_size",
    ],
    "additionalProperties": False,
}

# OpenAI's strict json_schema mode requires every declared property to appear in
# `required`; Anthropic's does not. `eligible_proposal` is genuinely optional -- rule 5
# means it is present only when the model wants to propose a value -- so the strict
# variant lists it and lets the empty string carry "no proposal". Derived rather than
# copied so the two schemas cannot drift apart.
STRICT_RESULT_SCHEMA = {
    **RESULT_SCHEMA,
    "required": list(RESULT_SCHEMA["properties"]),
}


def triage(changes: list[models.Change], sources: dict[str, dict[str, str]]) -> tuple[list[models.Change], list[models.Change]]:
    """Split changes into (classify, summarise-only).

    Everything is classified now. This used to bypass low-signal sources -- Simplify's
    ~600 untagged roles -- because an Opus call per row cost real money to produce
    noise. Two things changed. The backend is gpt-5-mini, so a row costs ~$0.0015
    instead of ~$0.025; and `sources.postings` now fetches the actual posting, so there
    is finally something worth judging. A Simplify row on its own is a title and a
    location: measured, 7 of 592 rows mention any class-year word, and all 7 are
    incidental matches on "Graduate Researcher". The bypass was skipping rows that
    could not have been judged anyway.

    Skipping them was also what filled WORTH A LOOK with 20-35 unreadable items a day,
    since an unclassified change defaults to `relevant=True`.

    The `signal` column stays in sources.csv: it no longer gates classification, but it
    still marks which sources are noisy, and the overflow sort below prefers rolling
    and discovery rows when a day exceeds the cap.
    """
    to_classify: list[models.Change] = list(changes)
    summarise_only: list[models.Change] = []

    if len(to_classify) > MAX_CLASSIFICATIONS_PER_RUN:
        # Keep the most interesting ones; the rest still get reported, unclassified.
        to_classify.sort(key=lambda c: (not c.rolling, not c.is_discovery_candidate))
        summarise_only.extend(to_classify[MAX_CLASSIFICATIONS_PER_RUN:])
        to_classify = to_classify[:MAX_CLASSIFICATIONS_PER_RUN]
    return to_classify, summarise_only


def _render_change(change: models.Change, posting: tuple[str, str] | None = None) -> str:
    """Render one change for the model. `posting` is the (text, error) from a fetch.

    The distinction between "no posting text" and "the fetch failed" is load-bearing:
    rule 6 tells the model not to rule anything out on class-year grounds when it has
    not seen the requirements, so the failure has to be stated rather than left as a
    silent absence.
    """
    lines = [
        f"Source: {change.source_id}",
        f"Change type: {change.kind}",
        f"Row: {change.key}",
    ]
    if change.program_name:
        lines.append(f"Tracked program this source informs: {change.program_name}")
    if change.url:
        lines.append(f"Link: {change.url}")
    if change.rolling:
        lines.append("NOTE: mentions a firm that reviews on a rolling basis.")
    lines.append("")
    lines.append(change.detail[:4000])

    text, error = posting if posting else ("", "")
    if text:
        lines.append("")
        lines.append("--- POSTING TEXT (fetched from the live listing) ---")
        lines.append(text[:coretext.MAX_TEXT_CHARS])
    elif error:
        lines.append("")
        lines.append(f"--- POSTING TEXT UNAVAILABLE: {error} ---")
        lines.append("Judge on the row above alone. Do not rule this out on class-year")
        lines.append("grounds; you have not seen the requirements.")
    return "\n".join(lines)


USAGE = models.Usage()
_USAGE_LOCK = threading.Lock()


def reset_usage() -> None:
    """Start a fresh tally. Called once per run, and by tests between cases."""
    global USAGE
    with _USAGE_LOCK:
        USAGE = models.Usage()


def _record_usage(
    provider: str, model: str, effort: str, usage, *,
    input_key: str, output_key: str, reasoning: int = 0, cached: int = 0,
) -> None:
    """Fold one response's usage into the run total.

    Missing usage is counted, not assumed to be zero: a provider that stops reporting
    would otherwise make the run look free, which is the same failure shape as a source
    that goes quiet instead of failing (spec 10.1).
    """
    with _USAGE_LOCK:
        USAGE.provider, USAGE.model, USAGE.effort = provider, model, effort
        USAGE.calls += 1
        if usage is None:
            USAGE.unreported += 1
            return
        USAGE.input_tokens += getattr(usage, input_key, 0) or 0
        USAGE.output_tokens += getattr(usage, output_key, 0) or 0
        USAGE.reasoning_tokens += reasoning or 0
        USAGE.cached_input_tokens += cached or 0


ANTHROPIC = "anthropic"
AZURE = "azure"


def select_provider() -> str | None:
    """Which backend to use, or None for degraded (unclassified) mode.

    Anthropic wins when both are configured: the system prompt was written against
    Claude, and gpt-5-mini is a deliberate cost substitution rather than an equal.
    `CLASSIFIER_PROVIDER` forces one either way, which is what the tests use to
    exercise a specific path without unsetting real credentials.
    """
    forced = (os.environ.get("CLASSIFIER_PROVIDER") or "").strip().lower()
    if forced in (ANTHROPIC, AZURE):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ANTHROPIC
    if os.environ.get("AZURE_OPENAI_API_KEY"):
        return AZURE
    return None


def build_client(provider: str):
    """Return (client, deployment). Raises; the caller degrades on any exception."""
    if provider == ANTHROPIC:
        import anthropic

        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("CLASSIFIER_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset")
        return anthropic.Anthropic(api_key=key), MODEL

    from openai import AzureOpenAI

    key = os.environ.get("AZURE_OPENAI_API_KEY")
    if not key:
        raise RuntimeError("CLASSIFIER_PROVIDER=azure but AZURE_OPENAI_API_KEY is unset")
    client = AzureOpenAI(
        api_key=key,
        azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT", AZURE_DEFAULT_ENDPOINT),
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", AZURE_DEFAULT_API_VERSION),
    )
    return client, os.environ.get("AZURE_OPENAI_DEPLOYMENT", AZURE_DEFAULT_DEPLOYMENT)


# Values the model uses to mean "the posting does not say". Left out of the digest
# rather than printed, because "Deadline: not specified" is a bullet that costs a line
# and tells the reader nothing its absence did not already say.
_UNSTATED = {"", "-", "n/a", "na", "none", "null", "unknown", "not specified",
             "not stated", "unspecified", "not listed", "tbd"}


def _clean(value) -> str:
    """One short field off the model, with the not-stated synonyms flattened to "".

    A small model asked for an optional string will sometimes answer the question
    rather than leave it blank, and each of those answers renders as a bullet claiming
    to be information. Normalising them here keeps that out of every consumer.
    """
    text = " ".join(str(value or "").split())
    return "" if text.lower().strip(" .") in _UNSTATED else text


def _size(value) -> str:
    """`small`, `large` or empty. Anything else is treated as unknown rather than guessed."""
    value = _clean(value).lower()
    return value if value in ("small", "large") else ""


def _judgment_from_text(change: models.Change, text: str) -> Judgment:
    """Parse a provider's JSON body into a Judgment. Shared by both backends."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return Judgment.failed(change, "could not parse the model's JSON response")

    return Judgment.from_model(
        change,
        relevant=bool(payload.get("relevant", True)),
        program_name=payload.get("program_name", "") or change.program_name,
        new_status=payload.get("new_status", "unknown"),
        why=payload.get("why", ""),
        confidence=payload.get("confidence", "low"),
        ruled_out_by=payload.get("ruled_out_by", ""),
        eligible_proposal=payload.get("eligible_proposal", ""),
        class_year=_clean(payload.get("class_year")),
        location=_clean(payload.get("location")),
        deadline=_clean(payload.get("deadline")),
        employer_size=_size(payload.get("employer_size")),
    )


def _classify_anthropic(
    client, model: str, change: models.Change, posting=None,
    effort: str = CLASSIFY_REASONING_EFFORT,
) -> Judgment:
    try:
        response = client.messages.create(
            model=model,
            # Must cover adaptive thinking AND the JSON response. Billing is per token
            # generated, not per token allowed, so a generous ceiling costs nothing and
            # avoids truncating the response on a long diff.
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={
                "effort": ANTHROPIC_EFFORT.get(effort, "low"),
                "format": {"type": "json_schema", "schema": RESULT_SCHEMA},
            },
            messages=[{"role": "user", "content": _render_change(change, posting)}],
        )
    except Exception as exc:
        return Judgment.failed(change, f"{type(exc).__name__}: {exc}")

    anthropic_usage = getattr(response, "usage", None)
    _record_usage(
        ANTHROPIC, model, effort, anthropic_usage,
        input_key="input_tokens", output_key="output_tokens",
        cached=getattr(anthropic_usage, "cache_read_input_tokens", 0) or 0,
    )

    if getattr(response, "stop_reason", None) == "refusal":
        # Surface it rather than drop it (rule 3).
        return Judgment.failed(change, "model declined to classify this change")

    text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    return _judgment_from_text(change, text)


def _classify_azure(
    client, deployment: str, change: models.Change, posting=None,
    effort: str = CLASSIFY_REASONING_EFFORT,
) -> Judgment:
    try:
        response = client.chat.completions.create(
            model=deployment,
            # gpt-5-mini is a reasoning model: the ceiling is `max_completion_tokens`
            # (`max_tokens` is rejected) and it must cover the hidden reasoning tokens
            # as well as the JSON, so the same generous 4096 applies here.
            max_completion_tokens=4096,
            reasoning_effort=effort,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "judgment",
                    "strict": True,
                    "schema": STRICT_RESULT_SCHEMA,
                },
            },
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _render_change(change, posting)},
            ],
        )
    except Exception as exc:
        return Judgment.failed(change, f"{type(exc).__name__}: {exc}")

    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    _record_usage(
        AZURE, deployment, effort, usage,
        input_key="prompt_tokens", output_key="completion_tokens",
        reasoning=getattr(details, "reasoning_tokens", 0) or 0,
        cached=getattr(prompt_details, "cached_tokens", 0) or 0,
    )

    choice = response.choices[0] if response.choices else None
    if choice is None:
        return Judgment.failed(change, "Azure returned no choices")
    # Azure's content filter and the token ceiling both produce a usable HTTP 200 with
    # an empty or partial body. Neither is a quiet day (spec 10.1), so both become an
    # error on the Judgment rather than a silently unclassified change.
    if choice.finish_reason == "content_filter":
        return Judgment.failed(change, "Azure content filter declined this change")
    if choice.finish_reason == "length":
        return Judgment.failed(change, "response hit max_completion_tokens before completing")

    return _judgment_from_text(change, choice.message.content or "")


def call_json(
    provider: str, client, deployment: str, *,
    system: str, user: str, schema: dict, schema_name: str,
    effort: str = CLASSIFY_REASONING_EFFORT,
) -> tuple[dict | None, str]:
    """One schema-constrained model call. Returns (parsed, error); never raises.

    Exists so that a caller which is not classifying a `Change` -- the weekly discovery
    pass, which judges whether a newly found *source* is worth watching -- does not have
    to open its own client, duplicate the two provider dialects, or keep its own token
    tally. Every model call in this project records into the same `USAGE`, because the
    spend line in HEALTH is only honest if nothing bills off to the side.

    `schema` must satisfy both dialects at once: all properties required and
    `additionalProperties: false`, which is what Azure's strict mode demands and
    Anthropic accepts.
    """
    try:
        if provider == ANTHROPIC:
            response = client.messages.create(
                model=deployment,
                max_tokens=4096,
                system=system,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": ANTHROPIC_EFFORT.get(effort, "low"),
                    "format": {"type": "json_schema", "schema": schema},
                },
                messages=[{"role": "user", "content": user}],
            )
        else:
            response = client.chat.completions.create(
                model=deployment,
                max_completion_tokens=4096,
                reasoning_effort=effort,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name, "strict": True, "schema": schema,
                    },
                },
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    if provider == ANTHROPIC:
        usage = getattr(response, "usage", None)
        _record_usage(
            ANTHROPIC, deployment, effort, usage,
            input_key="input_tokens", output_key="output_tokens",
            cached=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
        if getattr(response, "stop_reason", None) == "refusal":
            return None, "model declined to answer"
        text = "".join(
            b.text for b in response.content if getattr(b, "type", "") == "text"
        )
    else:
        usage = getattr(response, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        _record_usage(
            AZURE, deployment, effort, usage,
            input_key="prompt_tokens", output_key="completion_tokens",
            reasoning=getattr(details, "reasoning_tokens", 0) or 0,
            cached=getattr(prompt_details, "cached_tokens", 0) or 0,
        )
        choice = response.choices[0] if response.choices else None
        if choice is None:
            return None, "the provider returned no choices"
        # An HTTP 200 whose body is empty or truncated is not an answer. Same rule as
        # everywhere else: it has to read as a failure, not as a verdict.
        if choice.finish_reason == "content_filter":
            return None, "the content filter declined this request"
        if choice.finish_reason == "length":
            return None, "response hit max_completion_tokens before completing"
        text = choice.message.content or ""

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        return None, f"unparseable response: {type(exc).__name__}"
    if not isinstance(parsed, dict):
        return None, "response was not a JSON object"
    return parsed, ""


def classify_one(
    provider: str, client, deployment: str, change: models.Change, posting=None,
    effort: str = CLASSIFY_REASONING_EFFORT,
) -> Judgment:
    """Dispatch one change to whichever backend is configured."""
    if provider == ANTHROPIC:
        return _classify_anthropic(client, deployment, change, posting, effort)
    return _classify_azure(client, deployment, change, posting, effort)


def classify(
    changes: list[models.Change],
    sources: dict[str, dict[str, str]],
    bodies: dict[str, enrich_bodies.PostingBody] | None = None,
    verdicts: dict[str, screen.Verdict] | None = None,
) -> models.JudgedSet:
    """Classify what is worth classifying. Never raises, never drops a change.

    Returns the judgments *and* what they cost, together, because the spend is part of
    what the run did and reading it off a module global is what stopped `render` from
    being runnable on its own.

    `bodies` is what `enrich` fetched, keyed on change_id. It is an argument rather
    than something fetched here: this stage used to make network requests in the middle
    of itself, which meant a model-call stage was also an I/O stage and there was no way
    to see what it had fetched or to re-run it without paying for the fetches again.

    None means "this caller has not run enrich", and every change is then judged on its
    row alone -- which is a weaker judgment, not a wrong one, and the prompt is told so.

    `verdicts` is what the deterministic screen settled, keyed on change_id. It is an
    argument for the same reason: the loop and its tally used to be a module global
    here, which is concretely why `run.py render` could not run on its own.
    """
    reset_usage()

    # The posting text is read by the deterministic screen as well as by the model, and
    # the screen runs whether or not a provider is configured. That makes degraded mode
    # sharper rather than weaker: a change the screen rules out carries the employer's
    # own quoted sentence, which is the standard prompt rule 6 sets for the model
    # itself. Spec 8 rule 3 objects to dropping things on a *guess*; this is evidence.
    table = bodies or {}

    def posting_for(change: models.Change):
        return enrich_bodies.for_change(table, change)

    # The screen runs as its own stage. If this caller did not run it, run it here
    # rather than skipping it: it costs nothing, it works without a provider, and
    # skipping it would send changes to the model that a quoted phrase already settles.
    settled = screen.apply(changes, table) if verdicts is None else verdicts

    judgments: list[Judgment] = []
    unsettled: list[models.Change] = []
    for change in changes:
        verdict = settled.get(change.change_id)
        if verdict is None:
            unsettled.append(change)
            continue
        judgments.append(Judgment.from_screen(
            change, why=verdict.why, rule=verdict.rule, version=verdict.version))

    # The cap counts model calls, so it is applied after the screen and not before.
    # On 2026-10-03 it was applied to all 304 changes: 61 of the 250 slots went to rows
    # the screen then settled for free, and 54 rows reached the digest unverified while
    # only 197 calls were made. This list is only ever the overflow past
    # MAX_CLASSIFICATIONS_PER_RUN, so that is what its `why` says.
    to_judge, summarise_only = triage(unsettled, sources)
    judgments += [
        Judgment.over_budget(change, MAX_CLASSIFICATIONS_PER_RUN)
        for change in summarise_only
    ]

    provider = select_provider()
    if provider is None:
        for change in to_judge:
            judgments.append(Judgment.unjudged(
                change,
                "Not classified: no classifier credentials are configured (neither "
                "ANTHROPIC_API_KEY nor AZURE_OPENAI_API_KEY), so this is surfaced "
                "unjudged rather than dropped."))
        return models.JudgedSet(judgments=judgments, usage=USAGE)

    try:
        client, deployment = build_client(provider)
    except Exception as exc:
        for change in to_judge:
            judgments.append(Judgment.failed(change, str(exc)))
        return models.JudgedSet(judgments=judgments, usage=USAGE)

    def judge(change: models.Change) -> Judgment:
        return classify_one(
            provider, client, deployment, change, posting_for(change)
        )

    # Concurrent because the loop got long. Classifying every source instead of only
    # the high-signal ones took a run from a handful of calls to ~35 on a normal day
    # and 162 on the worst observed; serially at ~3s each that is minutes of runner
    # time for work that is almost entirely waiting on the network.
    #
    # `pool.map` yields results in input order, which matters: proposals.log is an
    # audit trail and must not reorder run to run. Writes happen below, on one thread,
    # after every judgment is in -- `store.append_proposal` appends to a file and is
    # not safe to call from the pool.
    with concurrent.futures.ThreadPoolExecutor(MAX_CONCURRENT_CLASSIFICATIONS) as pool:
        judged = list(pool.map(judge, to_judge))

    for change, judgment in zip(to_judge, judged):
        if judgment.eligible_proposal:
            store.append_proposal(
                f"{change.source_id}\t{change.key}\tproposed eligible="
                f"{judgment.eligible_proposal}\t{judgment.why}"
            )
        judgments.append(judgment)
    return models.JudgedSet(judgments=judgments, usage=USAGE)
