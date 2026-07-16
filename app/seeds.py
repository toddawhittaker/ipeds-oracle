"""Canonical seed data for the lesson library.

Kept in a dependency-free leaf module (imports nothing from the rest of the app)
so both `app.skills` — which inserts these on a fresh install — and `app.db` —
whose migration 6 rewrites the terse originals in an already-seeded database —
share ONE source of truth with no import cycle.

Each lesson is written as a full, plain-English rule an admin can read in the
Learned-lessons list, while retaining every table/column/code token so the LLM's
few-shot guidance is unchanged.
"""
from __future__ import annotations

# (question, worked-example SQL, human-readable lesson)
SEED_EXAMPLES: list[tuple[str, str, str]] = [
    (
        "Top 20 institutions granting Associate's degrees in Registered Nursing "
        "(CIP 51.3801) per year over the last 3 years",
        "WITH grads AS (\n"
        "  SELECT year, unitid, SUM(ctotalt) AS awards FROM c_a\n"
        "  WHERE cipcode='51.3801' AND awlevel=3 AND majornum=1\n"
        "    AND year > (SELECT MAX(year)-3 FROM _years)\n"
        "  GROUP BY year, unitid)\n"
        ",ranked AS (SELECT *, RANK() OVER (PARTITION BY year ORDER BY awards DESC)"
        " rk FROM grads)\n"
        "SELECT r.year, r.rk, ic.instnm, ic.stabbr, r.awards\n"
        "FROM ranked r JOIN institutions_current ic USING (unitid)\n"
        "WHERE r.rk<=20 ORDER BY r.year DESC, r.rk;",
        "Match an exact 6-digit CIP code (here 51.3801, Registered Nursing) so the "
        "2- and 4-digit rollup rows that also live in c_a aren't double-counted. "
        "Express \"the last N years\" as a constant bound — "
        "year > (SELECT MAX(year)-3 FROM _years) — instead of joining to a list of "
        "years, which would force a slow full scan. Rank within each year using "
        "RANK() OVER (PARTITION BY year ORDER BY awards DESC).",
    ),
    (
        "How many bachelor's degrees in Computer Science (11.0701) did California "
        "public universities award in the most recent year?",
        "SELECT SUM(c.ctotalt) AS cs_bachelors FROM c_a c\n"
        "JOIN hd h ON h.unitid=c.unitid AND h.year=c.year\n"
        "WHERE c.cipcode='11.0701' AND c.awlevel=5 AND c.majornum=1\n"
        "  AND c.year=(SELECT MAX(year) FROM _years) AND h.stabbr='CA' AND h.control=1;",
        "Bachelor's degrees are awlevel=5 and Computer Science is CIP 11.0701. To "
        "filter by state or by public vs. private, join each c_a completions row to "
        "the hd institution-directory table on BOTH unitid and year, then use "
        "control=1 for public institutions and stabbr for the state. Joining on year "
        "as well keeps each school's attributes aligned with the degree's collection "
        "year.",
    ),
    (
        "National total of associate's degrees per year, all programs",
        "SELECT year, SUM(ctotalt) AS associates FROM c_a\n"
        "WHERE awlevel=3 AND majornum=1 AND cipcode='99'\n"
        "GROUP BY year ORDER BY year;",
        "For a national or all-programs total, filter cipcode='99' — the "
        "pre-aggregated grand-total row — rather than summing across individual CIP "
        "codes. c_a stores 2-, 4-, and 6-digit CIP rollups that each re-sum to the "
        "same total, so adding them together overcounts by roughly 4x. Also keep "
        "majornum=1 so a student's second major isn't counted twice.",
    ),
]

# The terse original lesson text each seed shipped with, paired with its rewritten
# replacement. db migration 6 uses this to upgrade rows in a database seeded before
# the readability rewrite, matching on created_by='seed' AND the exact old text so
# an admin-edited lesson is never clobbered. These OLD strings are historical and
# frozen — never change them, or the migration will stop matching live rows.
SEED_LESSON_REWRITES: list[tuple[str, str]] = [
    ("Exact 6-digit CIP; constant year bound; RANK per year.", SEED_EXAMPLES[0][2]),
    ("Year-matched hd join; control=1 public; awlevel=5 bachelor's.", SEED_EXAMPLES[1][2]),
    ("Use grand-total CIP '99' — never sum all cipcodes (overcounts ~4x).",
     SEED_EXAMPLES[2][2]),
]
