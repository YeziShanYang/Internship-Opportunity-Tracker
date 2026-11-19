"""Who the digest is for. The single place the owner's interests are encoded.

This is perishable input, not a constant, and that is the whole reason it has a module
of its own with a review date beside it. A stale profile does not fail loudly -- it
quietly mis-sorts every item in every digest, and the output still looks perfectly
well-formed while doing it. `PROFILE_LAST_REVIEWED` drives a reminder in the CALENDAR
block once it goes stale; bump it by hand whenever the text changes.

The date is a backstop, not a substitute for asking. Anyone working on code that reads
this, or having a conversation that touches what the owner is looking for, should ask
whether it still holds.

It lives in `core` because `classify` passes it verbatim to the model and `deliver`
renders the staleness nag, and neither may import the other.
"""
from __future__ import annotations

PROFILE_LAST_REVIEWED = "2026-11-20"
PROFILE_REVIEW_AFTER_DAYS = 183  # ~6 months

# The hiring season (`clock.recruiting_cycle`) the class-standing rules were written for.
# Several are only true of one year: the profile below, prompt rules 1 and 6, and the
# screen's `sophomore-title` and `advanced-standing` rules all describe a first-year
# applying for Summer 2027, and the aggregator sources are named for that season. Next
# season the owner is a sophomore and those rules rule out exactly the right postings,
# silently. The digest warns from `ROLLOVER_WARN_DAYS` before the cycle passes this.
# At the same rollover, move cybersecurity from `process.tiers.SWE_GROUP` to
# `TARGET_ROLES`: it is a target field held back only because a first-year cannot yet
# apply to those roles.
RULES_CYCLE = 2027
ROLLOVER_WARN_DAYS = 45

# The identity-gate sentence is deliberately general rather than an enumeration of
# demographic attributes. This repository is public, and a profile that itemised the
# owner's gender, race, orientation and financial-aid status would publish all of it to
# anyone who opened the file -- for no gain, because the classifier prompt's rule 2
# already names the specific programmes the gate has to catch. The rule the model needs
# is "the student does not clear these criteria, so rule those programmes out", and that
# is what is stated. See classify.SYSTEM_PROMPT rule 2 for the list it works against.
OWNER_PROFILE = (
    "Freshman (first-year) undergraduate at Stanford in the 2026-27 academic year, "
    "expected graduation June 2030, studying math and/or CS. Not a sophomore: "
    "for a Summer 2027 role the student will have finished only the first year, so a posting "
    "that needs graduation before spring 2030 (May or June 2030 both match this student), "
    "current sophomore-or-higher "
    "standing, or more than one completed year of college does not fit. "
    "US citizen, US-based. Does not meet the eligibility criteria for "
    "identity-restricted programmes -- those reserved for women, transgender or "
    "gender-expansive students, underrepresented racial minorities, or LGBTQIA+ "
    "students -- nor for \"barriers to access and opportunity\" criteria, so "
    "programmes gated on any of those are not relevant. Willing to "
    "relocate for a summer, so location is not a filter and no city or region is "
    "preferred; only whether the role is in the United States matters, because the student "
    "cannot take one that recruits solely abroad. "
    "Interests: quantitative finance and machine learning/AI first, equally (trading, "
    "quant research, ML engineering, applied ML, ML research, AI products); software "
    "engineering second, with data science and, for now, cybersecurity alongside it. "
    "Would rather work at a startup or growth-stage company than a big established one, "
    "provided the startup is real: funded or otherwise vetted, with a product and a "
    "paid internship. Programmes aimed at first- and second-year students are the "
    "highest priority. Large established employers overwhelmingly hire juniors and "
    "seniors, so a first-year's best odds are at startups, small and mid-size firms and "
    "small trading shops."
)
