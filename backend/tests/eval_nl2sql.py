"""NL→SQL regression harness.

Runs the full agent against questions with known-good answers (drawn from
SCHEMA.md §8 + README). Use it to (a) sanity-check the pipeline and (b) gate
model swaps. Requires LLM_API_KEY in the environment/.env.

    .venv/bin/python backend/tests/eval_nl2sql.py

Learned-lessons A/B: run it twice against the real ipeds.db and compare the
pass rate to measure whether retrieved lessons actually help —

    SKILLS_ENABLED=1 .venv/bin/python backend/tests/eval_nl2sql.py   # lessons on
    SKILLS_ENABLED=0 .venv/bin/python backend/tests/eval_nl2sql.py   # lessons off
"""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.llm import run_agent
from app.tools.sql import ipeds_years


def _nums(text: str) -> set[int]:
    """All integers appearing in text, ignoring thousands separators."""
    return {int(m.replace(",", "")) for m in re.findall(r"\d[\d,]*", text)}


def contains_number(target: int, tol: float = 0.0):
    def check(res):
        if tol == 0:
            return target in _nums(res.answer)
        return any(abs(n - target) <= tol * target for n in _nums(res.answer))
    return check


# Matches an integer that is itself immediately (within a few filler words,
# e.g. "collection"/"of data") followed by "year"/"years" — i.e. the integer
# is being used to *count years*, not just any digit string that happens to
# appear near the word "year(s)" elsewhere in the sentence. That alone is
# NOT sufficient, though: a hyphenated academic-year span like "2020-21" (or
# the en-dash form "2020–21") can be followed by "academic years"/"reporting
# years" in ordinary prose, and the tail integer of that span ("21") would
# otherwise satisfy "<n> ... year(s)" just like a real count does. The
# invariant that actually distinguishes a count from a span tail: an integer
# that is immediately preceded by "<digit><hyphen-or-en-dash>" (no space) is
# the second half of a year span, never a standalone count — so such
# candidates are excluded via negative lookbehind, once per dash variant
# (Python's fixed-width-lookbehind requirement is why these are two
# assertions rather than one alternation).
_YEAR_COUNT_RE = re.compile(
    r"(?<!\d-)(?<!\d–)(\d+)\s*\*{0,2}\s*(?:[a-zA-Z]+\s+){0,3}years?\b",
    re.IGNORECASE,
)


def contains_year_count(target: int):
    """Check that `target` appears as the *count of years* in the answer,
    not merely as some integer that happens to appear in the text.

    Purpose-built for the "how many collection years" case: contains_number
    alone is not safe here because a hyphenated academic-year literal like
    "2020-21" tokenizes (via _nums) into a bare two-digit integer (21), which
    can collide with the derived count once the dataset grows past ~20 years
    (a full ~21-year NCES backfill is on the roadmap). That collision makes a
    wrong answer that merely spells out the year range score as a false pass
    the moment n reaches 20 or 21. Anchoring on "<n> ... year(s)" instead of
    "<n> appears anywhere" avoids that regardless of how large n gets.
    """
    def check(res):
        found = {int(m) for m in _YEAR_COUNT_RE.findall(res.answer)}
        return target in found
    return check


# (question, human-readable expectation, check function)
CASES = [
    (
        "How many bachelor's degrees in Computer Science (CIP 11.0701) did "
        "California public universities award in the most recent year?",
        "≈ 7,679",
        contains_number(7679, tol=0.02),
    ),
    (
        "What was the national total of associate's degrees awarded in the most "
        "recent year, across all programs?",
        "≈ 1.0M (0.9–1.1M)",
        lambda res: any(900_000 <= n <= 1_100_000 for n in _nums(res.answer)),
    ),
]


def year_count_case():
    """Build the "how many collection years" case with the expected count
    derived from the live ipeds.db at run time, instead of a frozen literal —
    so it stops drifting every time a new year gets integrated.

    Returns None (and the harness skips the case) when ipeds.db is missing or
    unreadable, mirroring the existing skip-cleanly-rather-than-crash habit
    instead of asserting a bogus count of 0.
    """
    years = ipeds_years(get_settings().ipeds_db_path)
    if not years:
        return None
    n = len(years)
    # contains_number(n) alone is NOT safe here: the answer text also
    # contains the year literals themselves (e.g. "2019, 2020, 25, 2024,
    # 2025" for "2019–20 through 2024–25"), and a hyphenated academic year
    # like "2020-21" tokenizes into a bare two-digit integer (21). Once n
    # reaches 20 or 21 (a full ~21-year NCES backfill is on the roadmap),
    # that bare year fragment can equal n and make a WRONG answer (one that
    # merely spells out the year range but cites the old count) pass. Use
    # contains_year_count instead: it requires n to sit directly in front of
    # "year(s)" AND excludes any candidate that is itself the tail of a
    # hyphenated/en-dash year span (e.g. the "21" in "2020-21 academic
    # years") — the latter alone isn't enough, since ordinary prose does put
    # a span's tail right next to the word "years".
    return (
        "How many collection years of data are in the database?",
        f"{n} years ({years[0]}–{years[-1]})",
        contains_year_count(n),
    )


async def main() -> int:
    s = get_settings()
    if not s.llm_api_key:
        print("SKIP: LLM_API_KEY not set — cannot run the LLM eval.")
        print("      Set it in .env, then re-run.")
        return 0

    cases = list(CASES)
    yc = year_count_case()
    if yc is not None:
        cases.append(yc)
    else:
        print("SKIP: year-count case — ipeds.db missing/unreadable, "
              "can't derive an expected value.")

    passed = 0
    for q, expect, check in cases:
        print(f"\nQ: {q}\n   expect: {expect}")
        res = await run_agent(q)
        if res.error:
            print(f"   ✗ ERROR: {res.error}")
            continue
        ok = check(res)
        passed += ok
        tag = "✓" if ok else "✗"
        print(f"   {tag} model={res.model_used} escalated={res.escalated} "
              f"iters={res.iterations} tokens={res.total_tokens}")
        print("   SQL: " + (res.sql_log[-1][:120] if res.sql_log else "(none)"))
        print("   answer: " + res.answer[:300].replace("\n", " "))

    print(f"\n{passed}/{len(cases)} cases passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
