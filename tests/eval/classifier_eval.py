"""Score the classifier's labels against hand-labelled real postings.

Not part of the unit suite: it makes one live model call per posting (about 110, roughly
ten cents on gpt-5-mini), so run it by hand before trusting a change to the prompt, the
schema or the model:

    AZURE_OPENAI_ENDPOINT=... .venv/bin/python tests/eval/classifier_eval.py [set ...]

Sets are `dev`, `test`, `fresh` and `final` (default: all). The cases live in
`classifier_cases.json`: real postings with their row, link and posting text frozen as
fetched on 2026-11-20, so every run gives the model the same input. Each case states the
firm field, role field and size it should get. `expect` was written before the first
run of its set, corrected only where it named the wrong company; `also_accept` was
widened after a run, so the strict score ignores it and the lenient score counts it.

Two scores, because they answer different questions. Labels: did the model describe the
posting correctly. Tiers: does the posting land where the grid says it should, which is
what reaches the digest -- a wrong label often costs nothing (a hardware role is ruled
out at any firm). Eligibility rule-outs are reported but not scored: they depend on the
posting's own requirements, not on the labels.

Measured 2026-11-20 while the prompt was being tuned (live, before the inputs were
frozen): `dev` was tuned on and reached 29-30/30 with all three labels right; `test`
scored 21/25 before the code guards in process.tiers; `final` was never tuned on and
scored 23/27, with 2 of 27 in a different tier. A set you tune on stops being an
estimate. Keep one untouched, or add fresh postings, before quoting a number.

Writes nothing: the eligibility proposals the classifier would append to
data/proposals.log are discarded, and the employer table is not used.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import classify  # noqa: E402
from core import models  # noqa: E402
from enrich import bodies as enrich_bodies  # noqa: E402
from persist import store  # noqa: E402
from process import tiers  # noqa: E402

CASES = pathlib.Path(__file__).with_name("classifier_cases.json")
SETS = ("dev", "test", "fresh", "final")
ANY_SIZE = {"small", "large"}


def _accepts(case: dict, field: str, got: str, lenient: bool) -> bool:
    allowed = set(case["expect"][field])
    if lenient:
        allowed |= set(case.get("also_accept", {}).get(field, []))
    return (field == "size" and allowed >= ANY_SIZE) or got in allowed


def _tier(judgment: models.Judgment) -> str:
    tier, _reason = tiers.place(judgment)
    return tier or "out"


def _expected_tiers(case: dict, judgment: models.Judgment, lenient: bool) -> set[str]:
    """Every tier the accepted label combinations would produce for this posting."""
    fields = {f: set(case["expect"][f]) | (
        set(case.get("also_accept", {}).get(f, [])) if lenient else set())
        for f in ("firm", "role", "size")}
    out = set()
    for firm in fields["firm"]:
        for role in fields["role"]:
            for size in fields["size"]:
                probe = models.Judgment(change=judgment.change, firm_field=firm,
                                        role_field=role, employer_size=size)
                out.add(_tier(probe))
    return out


def run(selected: tuple[str, ...]) -> int:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = [c for c in data["cases"] if c["set"] in selected]
    store.append_proposal = lambda line: None  # an evaluation must not write to data/
    sources = {s["source_id"]: s for s in store.read_sources()}

    changes, bodies, by_id = [], {}, {}
    for i, case in enumerate(cases):
        change = models.Change(
            source_id=case["source_id"], kind="added", key=case["key"],
            detail=case["detail"], change_id=f"eval-{i}", url=case["url"] or "",
            posting_url=case["url"] or "")
        changes.append(change)
        by_id[change.change_id] = case
        bodies[change.change_id] = enrich_bodies.PostingBody(
            change_id=change.change_id, text=case["posting_text"],
            error=case["posting_error"], url=case["url"] or "",
            origin=enrich_bodies.FETCHED)

    # verdicts={} skips the deterministic screen, so every posting reaches the model;
    # employers=None keeps the stored employer table out of it.
    judged = classify.classify(changes, sources, bodies, {}, prior=None, employers=None)

    totals = {s: {"n": 0, "strict": 0, "lenient": 0, "tier": 0, "eligibility": 0}
              for s in selected}
    for judgment in judged.judgments:
        case = by_id[judgment.change.change_id]
        got = {"firm": judgment.firm_field, "role": judgment.role_field,
               "size": "small" if tiers.is_small(judgment) else "large"}
        strict = all(_accepts(case, f, got[f], False) for f in got)
        lenient = all(_accepts(case, f, got[f], True) for f in got)
        tier_ok = _tier(judgment) in _expected_tiers(case, judgment, True)
        row = totals[case["set"]]
        row["n"] += 1
        row["strict"] += strict
        row["lenient"] += lenient
        row["tier"] += tier_ok
        if not lenient or not tier_ok:
            wrong = [f"{f}={got[f] or '-'}" for f in got if not _accepts(case, f, got[f], True)]
            text = "" if case["posting_text"] else " (no posting text)"
            print(f"  [{case['set']}] {case['key'][:70]}{text}\n"
                  f"      got {', '.join(wrong) or 'labels ok'}; tier {_tier(judgment)}, "
                  f"expected {'/'.join(sorted(_expected_tiers(case, judgment, True)))}")
        row["eligibility"] += not judgment.relevant and judgment.ruled_out_by != "value"

    print()
    for name in selected:
        row = totals[name]
        if row["n"]:
            print(f"{name:5}  {row['n']:3} postings   labels strict {row['strict']}/{row['n']}"
                  f"   lenient {row['lenient']}/{row['n']}   tier {row['tier']}/{row['n']}"
                  f"   (eligibility rule-outs, unscored: {row['eligibility']})")
    spend = judged.usage
    print(f"\n{spend.calls} model calls")
    return 0


if __name__ == "__main__":
    chosen = tuple(sys.argv[1:]) or SETS
    unknown = set(chosen) - set(SETS)
    if unknown:
        sys.exit(f"unknown set(s): {', '.join(sorted(unknown))}; choose from {', '.join(SETS)}")
    sys.exit(run(chosen))
