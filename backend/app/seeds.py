"""Canonical seed data for the lesson library.

Kept in a dependency-free leaf module (imports nothing from the rest of the app)
so both `app.skills` — which inserts these on a fresh install — and `app.db` —
whose migration 6 rewrites the terse originals in an already-seeded database —
share ONE source of truth with no import cycle.

Each lesson is a short generalized HEADLINE (the rule, one line), a longer
generalized DESCRIPTION (the technique, explained in plain prose so it applies
beyond the one worked example), and a commented_sql worked example that keeps a
concrete, runnable query but explains each key field inline.
"""
from __future__ import annotations

from typing import NamedTuple


class SeedLesson(NamedTuple):
    question: str
    headline: str
    description: str
    commented_sql: str


SEED_EXAMPLES: list[SeedLesson] = [
    SeedLesson(
        question=(
            "Top 20 institutions granting Associate's degrees in Registered Nursing "
            "(CIP 51.3801) per year over the last 3 years"
        ),
        headline=(
            "Count a specific program with an exact 6-digit CIP code — never a "
            "prefix or rollup row."
        ),
        description=(
            "Completions tables like c_a store CIP codes at multiple rollup levels "
            "— 2-digit, 4-digit, 6-digit, and a '99' grand-total row — that all "
            "re-sum to the same total, so matching a prefix or omitting the CIP "
            "filter overcounts by roughly 4x. Always match the exact 6-digit leaf "
            "code for the specific program you mean, keep majornum=1 so a "
            "student's second major isn't counted twice, express \"the last N "
            "years\" as a constant bound (year > (SELECT MAX(year)-N FROM _years)) "
            "instead of joining to a year list, and rank within each year with "
            "RANK() OVER (PARTITION BY year ORDER BY awards DESC)."
        ),
        commented_sql=(
            "WITH grads AS (\n"
            "  SELECT year, unitid, SUM(ctotalt) AS awards FROM c_a\n"
            "  WHERE cipcode='51.3801'  -- exact 6-digit leaf CIP, not a 2-/4-digit rollup\n"
            "    AND awlevel=3          -- award level: 3 = Associate's\n"
            "    AND majornum=1         -- first major only, no double-counted second major\n"
            "    AND year > (SELECT MAX(year)-3 FROM _years)  -- constant bound, last 3 years\n"
            "  GROUP BY year, unitid)\n"
            ",ranked AS (SELECT *, RANK() OVER (PARTITION BY year ORDER BY awards DESC)"
            " rk FROM grads)\n"
            "SELECT r.year, r.rk, ic.instnm, ic.stabbr, r.awards\n"
            "FROM ranked r JOIN institutions_current ic USING (unitid)\n"
            "WHERE r.rk<=20 ORDER BY r.year DESC, r.rk;  -- top 20 per year"
        ),
    ),
    SeedLesson(
        question=(
            "How many bachelor's degrees in Computer Science (11.0701) did California "
            "public universities award in the most recent year?"
        ),
        headline=(
            "Join completions to hd on unitid AND year to filter by state or "
            "public/private."
        ),
        description=(
            "Filtering a completions total by state or by public-vs-private control "
            "requires joining the completions row to the hd institution-directory "
            "table on BOTH unitid and year — never unitid alone — so each school's "
            "attributes (stabbr for state, control for public/private/for-profit) "
            "stay aligned with the exact collection year the degree count came "
            "from. awlevel selects the award level (e.g. 5 for Bachelor's), and "
            "control=1 identifies public institutions."
        ),
        commented_sql=(
            "SELECT SUM(c.ctotalt) AS cs_bachelors FROM c_a c\n"
            "JOIN hd h ON h.unitid=c.unitid AND h.year=c.year\n"
            "  -- join on BOTH unitid AND year so h's attributes match c's collection year\n"
            "WHERE c.cipcode='11.0701'   -- exact 6-digit leaf CIP (swap for any program)\n"
            "  AND c.awlevel=5           -- award level: 5 = Bachelor's\n"
            "  AND c.majornum=1          -- first major only\n"
            "  AND c.year=(SELECT MAX(year) FROM _years)  -- most recent collection year\n"
            "  AND h.stabbr='CA'        -- state filter (swap for any state)\n"
            "  AND h.control=1;         -- 1 = public (swap for private/for-profit control codes)"
        ),
    ),
    SeedLesson(
        question="National total of associate's degrees per year, all programs",
        headline=(
            "For a national or all-programs total, use the grand-total row "
            "cipcode='99', never SUM across CIP codes."
        ),
        description=(
            "Completions tables carry a pre-aggregated grand-total row where "
            "cipcode='99' already equals the sum of every individual program, so a "
            "national or all-programs total should filter on that row rather than "
            "summing across CIP codes — doing the latter overcounts by roughly 4x "
            "because 2-, 4-, and 6-digit rollups all re-sum to the same total. Also "
            "keep majornum=1 so a student's second major isn't counted twice."
        ),
        commented_sql=(
            "SELECT year, SUM(ctotalt) AS associates FROM c_a\n"
            "WHERE awlevel=3     -- award level: 3 = Associate's\n"
            "  AND majornum=1    -- first major only, avoids double-counting a second major\n"
            "  AND cipcode='99'  -- pre-aggregated grand total -- never SUM individual CIPs\n"
            "GROUP BY year ORDER BY year;"
        ),
    ),
    # --- Lessons distilled from live use, then reviewed + verified against the
    # real ipeds.db schema, and promoted to shipped seeds. Unlike the three above
    # they never had a terse v1, so they carry NO SEED_LESSON_UPGRADES entry
    # (migration 6 only rewrites the originals). ---
    SeedLesson(
        question="Which states awarded the most Master's degrees in Education?",
        headline=(
            "Total a CIP family with its 6-digit leaf codes, never a LIKE-prefix "
            "(it double-counts the rollup rows)."
        ),
        description=(
            "To total every program within a CIP family (e.g. all of Education = "
            "family 13), do NOT filter with cipcode LIKE '13%' or "
            "SUBSTR(cipcode,1,2)='13'. The completions table c_a carries "
            "pre-aggregated rollup rows at the 2-digit ('13'), 4-digit ('13.01') "
            "and 6-digit ('13.0101') levels plus a '99' grand total, and a prefix "
            "match sums those rollups together with the leaf rows — overcounting by "
            "roughly 4x. Instead match only the 6-digit leaf codes of the family: "
            "SUBSTR(cipcode,1,3)='13.' AND length(cipcode)=7. Keep majornum=1 so a "
            "student's second major isn't counted twice. For a single specific "
            "program, match its exact 6-digit code; for a national all-programs "
            "total, use the cipcode='99' grand-total row."
        ),
        commented_sql=(
            "SELECT h.stabbr, SUM(c.ctotalt) AS total_masters\n"
            "FROM c_a c\n"
            "JOIN hd h ON h.unitid=c.unitid AND h.year=c.year\n"
            "WHERE SUBSTR(c.cipcode,1,3)='13.'  -- CIP family 13 = Education\n"
            "  AND length(c.cipcode)=7          -- 6-digit LEAF rows only, no rollups\n"
            "  AND c.awlevel=7                  -- award level: 7 = Master's\n"
            "  AND c.majornum=1                 -- first major only\n"
            "  AND c.year=(SELECT MAX(year) FROM _years)  -- latest loaded collection year\n"
            "GROUP BY h.stabbr\n"
            "ORDER BY total_masters DESC\n"
            "LIMIT 15;"
        ),
    ),
    SeedLesson(
        question=(
            "How many Computer Science bachelor's degrees did California public "
            "universities award last year?"
        ),
        headline=(
            "Filter to the latest year via (SELECT MAX(year) FROM _years), never a "
            "hardcoded literal year."
        ),
        description=(
            "IPEDS completions data typically lags by two years, so hardcoding a "
            "literal like year=2025 can return rows from a year that does not exist "
            "(zero results) or an incorrect future year. Filter with the authoritative "
            "loaded-year list instead: c.year=(SELECT MAX(year) FROM _years) for the "
            "most recent year, or year > (SELECT MAX(year)-N FROM _years) for the last "
            "N years — never a literal, and confirm it matches the question's "
            "definition of \"last year\"."
        ),
        commented_sql=(
            "SELECT SUM(c.ctotalt) AS cs_bachelors\n"
            "FROM c_a c\n"
            "JOIN hd h ON h.unitid=c.unitid AND h.year=c.year\n"
            "WHERE c.cipcode='11.0701'   -- exact 6-digit leaf CIP (Computer Science)\n"
            "  AND c.awlevel=5           -- award level: 5 = Bachelor's\n"
            "  AND c.majornum=1          -- first major only\n"
            "  AND c.year=(SELECT MAX(year) FROM _years)  -- latest loaded year, not a literal\n"
            "  AND h.stabbr='CA'         -- state filter\n"
            "  AND h.control=1;          -- 1 = public"
        ),
    ),
    SeedLesson(
        question="Is community college undergraduate enrollment rising or falling?",
        headline=(
            "IPEDS data does not include future years; verify the latest available "
            "year and filter appropriately."
        ),
        description=(
            "Do not report figures for a year that is not in the dataset yet (e.g. "
            "2025 when the latest loaded year is 2023 or 2024). IPEDS does not include "
            "future years, and a bound like year > (SELECT MAX(year)-6 FROM _years) "
            "silently returns nothing for the missing years. Always check the latest "
            "available year in the _years table (the authoritative list of loaded "
            "collection years) before reporting a trend, and never fabricate or imply "
            "data for a year the dataset does not contain."
        ),
        commented_sql=(
            "SELECT year FROM _years ORDER BY year;"
            "  -- authoritative loaded-year list — verify the latest before trending"
        ),
    ),
    SeedLesson(
        question="Which five states award the most nursing bachelor's degrees?",
        headline=(
            "Clarify temporal scope when a user switches topics without specifying a "
            "year."
        ),
        description=(
            "When a user shifts to a new subject area (e.g. from computer science to "
            "nursing) and asks a question without explicitly stating a year, ask which "
            "year or time period they want before answering. Do not assume they mean "
            "the same year as the previous topic, since the context may have changed "
            "and their intent may differ."
        ),
        commented_sql="",  # a conversational lesson — no worked query
    ),
    SeedLesson(
        question=(
            "How is enrollment for AS-level allied healthcare fields trending across "
            "the nation for the last 5 years? Group by public, private, and "
            "for-profit institutions."
        ),
        headline=(
            "Enrollment by program is in efcp, not completions; where efcp omits a "
            "field, completions is the proxy."
        ),
        description=(
            "IPEDS keeps enrollment and completions in different tables: completions "
            "(degrees awarded) in c_a, and enrollment headcounts by program field in "
            "efcp (Fall Enrollment by CIP) — NOT in c_a, and NOT in the level-only ef "
            "table. So a program-level enrollment question should use efcp. But efcp "
            "reports only a limited set of broad CIP families; many 4-/6-digit fields "
            "(e.g. 51.08 and 51.09, allied health) have no efcp rows at all. When efcp "
            "does not cover the requested field, completions (c_a) is the best "
            "available proxy for program activity — use it, and state plainly that the "
            "figure is degrees awarded, not enrolled headcount. Either way, only "
            "report numbers that appear in, or derive directly from, the query results."
        ),
        commented_sql=(
            "-- efcp has no 51.08/51.09 rows, so completions (c_a) is the honest proxy:\n"
            "SELECT\n"
            "  c.year,\n"
            "  CASE h.control\n"
            "    WHEN 1 THEN 'Public'\n"
            "    WHEN 2 THEN 'Private non-profit'\n"
            "    WHEN 3 THEN 'Private for-profit'\n"
            "  END AS sector,\n"
            "  SUM(c.ctotalt) AS completions\n"
            "FROM c_a c\n"
            "JOIN hd h ON h.unitid=c.unitid AND h.year=c.year\n"
            "WHERE c.awlevel=3                 -- award level: 3 = Associate's\n"
            "  AND c.majornum=1                -- first major only\n"
            "  AND length(c.cipcode)=7         -- 6-digit leaf rows only, no rollups\n"
            "  AND SUBSTR(c.cipcode,1,5) IN ('51.00','51.08','51.09')  -- allied-health families\n"
            "  AND c.year > (SELECT MAX(year)-5 FROM _years)  -- constant bound, last 5 years\n"
            "GROUP BY c.year, h.control\n"
            "ORDER BY c.year, h.control"
        ),
    ),
]

# The terse original lesson text each seed shipped with, paired with the v1
# (post-migration-6, readable-but-not-yet-generalized) description it was
# rewritten to. db migration 6 uses this to upgrade rows in a database seeded
# before the readability rewrite, matching on created_by='seed' AND the exact
# old text so an admin-edited lesson is never clobbered. These strings are
# historical and frozen — never change them, or migration 6 will stop matching
# live rows. Hard-coded as literals (not derived from SEED_EXAMPLES) so a
# future edit to the generalized seed content above can never drift migration
# 6's frozen match keys.
SEED_LESSON_REWRITES: list[tuple[str, str]] = [
    (
        "Exact 6-digit CIP; constant year bound; RANK per year.",
        "Match an exact 6-digit CIP code (here 51.3801, Registered Nursing) so the "
        "2- and 4-digit rollup rows that also live in c_a aren't double-counted. "
        "Express \"the last N years\" as a constant bound — "
        "year > (SELECT MAX(year)-3 FROM _years) — instead of joining to a list of "
        "years, which would force a slow full scan. Rank within each year using "
        "RANK() OVER (PARTITION BY year ORDER BY awards DESC).",
    ),
    (
        "Year-matched hd join; control=1 public; awlevel=5 bachelor's.",
        "Bachelor's degrees are awlevel=5 and Computer Science is CIP 11.0701. To "
        "filter by state or by public vs. private, join each c_a completions row to "
        "the hd institution-directory table on BOTH unitid and year, then use "
        "control=1 for public institutions and stabbr for the state. Joining on year "
        "as well keeps each school's attributes aligned with the degree's collection "
        "year.",
    ),
    (
        "Use grand-total CIP '99' — never sum all cipcodes (overcounts ~4x).",
        "For a national or all-programs total, filter cipcode='99' — the "
        "pre-aggregated grand-total row — rather than summing across individual CIP "
        "codes. c_a stores 2-, 4-, and 6-digit CIP rollups that each re-sum to the "
        "same total, so adding them together overcounts by roughly 4x. Also keep "
        "majornum=1 so a student's second major isn't counted twice.",
    ),
]

# Upgrade map for `skills.upgrade_seed_lessons()`: the frozen v1 description
# (the exact text migration 6 rewrote a terse row INTO — the second element of
# each SEED_LESSON_REWRITES tuple above) paired with the new generalized
# SeedLesson to upgrade it to. The v2 SeedLesson MUST be the same object shipped
# in SEED_EXAMPLES (enforced by a drift-guard test) so a fresh install and an
# upgraded live db always converge on identical seed text.
SEED_LESSON_UPGRADES: list[tuple[str, SeedLesson]] = [
    (SEED_LESSON_REWRITES[0][1], SEED_EXAMPLES[0]),
    (SEED_LESSON_REWRITES[1][1], SEED_EXAMPLES[1]),
    (SEED_LESSON_REWRITES[2][1], SEED_EXAMPLES[2]),
]
