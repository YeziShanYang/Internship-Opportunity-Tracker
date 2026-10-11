"""Standing calendar reminders (spec section 7).

The most schedule-critical items are the least automatable. Intercollegiate competition
announcements go out on Instagram, university listservs and club mailing lists *before*
the website changes, and Jane Street's ETC and Symposium arrive by email through campus
recruiting. Rather than pretend to watch those, the digest prints a reminder during the
window when each one historically opens.

Pure data, no network. Anything added here should be something a page diff genuinely
cannot catch.
"""
from __future__ import annotations

import datetime

# month number -> reminders printed in that month's digests
BY_MONTH: dict[int, list[str]] = {
    1: [
        "SULI deadline this month.",
        "CURIS opens late January.",
        "GSoC: start contributing to open source NOW. Pre-application contributions are "
        "the strongest predictor of selection.",
        "MIT Pokerbots runs this month, but it is marked NO in your sheet: the "
        "competition server needs a teammate with MIT certificates. Only worth revisiting "
        "if you have an MIT teammate.",
        "Caltech SURF and USC ICT's Summer Research Program refuse the Actions runner, so "
        "nothing watches them - check their pages for the Summer 2027 cycle this month: "
        "sfp.caltech.edu/undergraduate-research/programs/surf/application_information and "
        "ict.usc.edu/careers/internships/summer-research-program-application. Berkeley "
        "SUPERB-AI4E is unwatched for the same reason, but it takes rising juniors and "
        "seniors, so it is one for later.",
    ],
    2: [
        "SURIM (~Feb 26), CURIS (Jan 27-Feb 10), UChicago REU (~Feb 6), VPUE Faculty "
        "Grant (Feb 15). Draft one personal statement in early January and adapt it.",
    ],
    3: [
        "IMC Prosperity tutorial round.",
        "GSoC applications ~Mar 16-31.",
        "VPUE Major Grant mentor letter due Mar 8.",
    ],
    9: [
        "Cornell CTC opens early fall - check the club's Instagram and your "
        "Traders @ Stanford channels.",
    ],
    10: [
        # Corrected 2026-09-11 from the postings themselves. The programme called
        # "First Year Discovery Event" is SIG's SYDNEY listing, and it is consistent
        # with a first-year only because Australian bachelor's degrees run three
        # years. Every US Discovery Program reads "planning to graduate in the winter
        # of 2028 or the spring of 2029", which on a four-year US degree is a current
        # sophomore. Do not restore the old wording; a label is not an eligibility gate.
        "SIG Discovery Programs (New York, Bala Cynwyd) close Nov 16 - but this cycle "
        "is gated to winter 2028 / spring 2029 graduates, so it is NOT open to you "
        "(class of 2030). Your cycle should be posted around autumn 2027, feeding "
        "Summer 2029. sig-phenom now watches the board automatically.",
        "Group One Trading is at a STANFORD CAREER FAIR on Oct 6 - found on their "
        "careers page 2026-09-11. They post Trading Analyst Intern and Software "
        "Developer Intern roles; an on-campus fair is the cheapest referral you will get "
        "all year.",
        "Voloridge ASCEND PROGRAM 2027 (Jupiter, FL) is open to FIRST and second-year "
        "undergraduates - one of the few programmes you are eligible for right now. "
        "20 places per cohort, travel and hotel covered. Watched by voloridge-greenhouse.",
        "Jane Street FTTP is rolling and slots have reportedly filled by late October. "
        "If you have not applied, this is the week.",
        "Putnam: register through Stanford's local supervisor - the exam is the first "
        "Saturday of December. maa.org refuses this tool, so this reminder is the only "
        "coverage.",
    ],
    11: [
        "UChicago UTC applications open this month.",
        "Putnam registration closes before the early-December exam - last call through "
        "Stanford's local supervisor.",
        "Google Student Researcher closes Nov 27.",
    ],
    12: [
        "The Deck Game and similar invitational card/trading competitions announce "
        "winter rounds around now. The Deck Game's site is a JS shell with nothing to "
        "diff, so this reminder is the only coverage you get - check it directly.",
        "Optiver Ready Trader Go has historically announced registration on "
        "readytradergo.optiver.com. It has skipped years, so absence is not a bug.",
    ],
}

# Printed every month: sources that are verified un-pollable, so the only thing that
# will ever surface them is the owner opening the page.
ALWAYS: list[str] = [
    "Citadel and Citadel Securities refuse this tool's client, so their own pages are "
    "never read. Their internship postings now arrive through the speedyapply Quant "
    "lists, but Discover Citadel does not - it is freshman-eligible, so open the "
    "programs-and-events pages by hand this month.",
    "IAS/PCMI also blocks automation. Check it by hand if you are considering it.",
    "Stanford Pre-Collegiate Studies (SUMaC RC/TA) refuses the Actions runner. Its "
    "residential page offers an interest form for the Summer 2027 cycle - fill it in once "
    "and SPCS emails you when hiring opens: spcs.stanford.edu/residential-program-positions",
    "Stanford's SOLO portal and the Digital Economy Lab refuse the Actions runner, so "
    "nothing watches them. Check SOLO's freshman research list weekly - "
    "solo.stanford.edu/opportunities?class=16&type%5B%5D=2 - and the Lab's "
    "digitaleconomy.stanford.edu/about/careers-and-opportunities now and then.",
    "Kaggle competitions are always open and rolling - browse kaggle.com/competitions "
    "when you want a project, not on a deadline.",
]


def for_month(when: datetime.date | None = None) -> tuple[str, list[str]]:
    """Return (month name, reminders) for the given date."""
    when = when or datetime.date.today()
    return when.strftime("%B"), list(BY_MONTH.get(when.month, []))
