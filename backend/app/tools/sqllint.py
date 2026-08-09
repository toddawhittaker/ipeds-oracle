"""Deterministic pre-flight lint for model-generated IPEDS SQL.

These are the project-specific aggregation foot-guns documented in SCHEMA.md /
CLAUDE.md that a general-purpose LLM gets wrong and that no amount of few-shot
priming reliably prevents:

  * CIP-rollup double counting — c_a stores 2-/4-/6-digit cipcode rows PLUS a
    '99' grand-total row that each sum to the same national total, so
    `cipcode LIKE '51.%'` (or summing with no CIP guard at all) over-counts,
    typically ~4x;
  * second-major double counting — c_a has majornum=1 and majornum=2 rows;
    summing both counts double-majors twice;
  * the DISTINCT-year join that makes SQLite full-scan the ~8M-row c_a and hang.

The checks are pure string/regex heuristics — no DB, no LLM — cheap enough to run
on every query. Their findings are fed back to the model (appended to the tool
result) so it can self-correct BEFORE a wrong number reaches the user; this is
the enforcement layer behind the prompt's "sanity-check magnitudes" instruction,
which a model can silently ignore.

Findings are ADVISORY — they never block execution. A heuristic false positive
must not stop a legitimate query, and even a flagged query's rows give the model
the context to reconsider. We deliberately bias toward *fewer* warnings (e.g. a
GROUP BY cipcode suppresses the rollup check) so the signal stays trustworthy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.tools.sql import _mask_string_literals, _strip_sql


@dataclass(frozen=True)
class LintFinding:
    code: str
    message: str


# c_a is the completions table where every rollup foot-gun lives. Match it as a
# whole word so it survives an alias (`FROM c_a c`) and a qualified column.
_C_A_RE = re.compile(r"\bc_a\b")
_SUM_RE = re.compile(r"\bsum\s*\(")
# A CIP "level guard" — any of these pins the query to a single aggregation
# level (or the '99' grand total), so summing counts is safe from rollup mixing.
_CIP_EQ_RE = re.compile(r"\bcipcode\b\s*(?:=|in\b)")
_CIP_LEN_RE = re.compile(r"\blength\s*\(\s*cipcode\s*\)")
_CIP_LIKE_RE = re.compile(r"\bcipcode\b\s*(?:not\s+)?like\b")
# GROUP BY cipcode makes each output row a single CIP level → no cross-level sum.
_GROUP_CIP_RE = re.compile(r"\bgroup\s+by\b.*\bcipcode\b", re.DOTALL)
# The classic hang: a distinct-year subquery joined/IN'd against c_a.
_DISTINCT_YEAR_JOIN_RE = re.compile(
    r"\b(?:join|in)\s*\(\s*select\s+distinct\s+year\b")
_MAJORNUM_RE = re.compile(r"\bmajornum\b")

# --- award-level nesting (see SCHEMA.md §5) ------------------------------------
# `awlevel` nests TWICE, and both traps are arithmetic identities rather than
# heuristics, so these two checks can be strict where the CIP ones are cautious:
#   * 20 (cert < 12 wks) + 21 (cert 12 wks-1 yr) == 1 (cert < 1 yr), EXACTLY;
#   * 13 == 1+2+4, 14 == 6+8, 12 == 3+5+7+17+18+19, and 15 == 12+13+14.
# SCHEMA.md used to call "1-8, 17-21" mutually exclusive. It is not, and the
# agent wrote precisely that list live: Ohio's all-level nursing awards came back
# as 10,592 against a true 10,574, counting one school's 18 short certificates
# under both 1 and 21. Grounding graded it `exact` and the reader saw the ✓ mark,
# because grounding attests reproduction from the query result and never that the
# query was right — which is exactly why this belongs in a deterministic lint.
_AWLEVEL_ROLLUPS = frozenset({12, 13, 14, 15})
_AWLEVEL_REAL = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 17, 18, 19, 20, 21})
# `[^)]*` is LINEAR and cannot backtrack; the captured text is validated as a
# bare code list afterwards, which also skips `awlevel IN (SELECT ...)`.
#
# The first version was `\s*([\d\s,]+?)\s*\)` — three quantifiers over classes
# that all match whitespace, adjacent. A long space run that never reaches a `)`
# made the engine enumerate the ways to split it: 0.055s at 500 spaces, 0.45s at
# 1,000, 3.4s at 2,000, ~26s at 4,000. That cost lands INSIDE `run_sql` but
# OUTSIDE every budget it sets — `lint_sql` runs before the interrupt watchdog is
# armed and before the 3s CSV probe timeout applies — and `validate_sql` accepts
# the query, since it is one read-only SELECT. It also persists into `sql_log`,
# so a CSV export replays it. Pinned by a timing test.
_AWLEVEL_IN_RE = re.compile(r"\bawlevel\b\s*in\s*\(([^)]*)\)")
_AWLEVEL_CODE_LIST_RE = re.compile(r"\A[\d\s,]+\Z")
# 2 digits is every real awlevel; the bound also keeps `int()` away from its
# 4,300-digit conversion limit, which raised ValueError out of an ADVISORY
# linter and let it veto a query the sandbox would have run.
_AWLEVEL_CODE_RE = re.compile(r"\d{1,2}\b")
_AWLEVEL_EQ_RE = re.compile(r"\bawlevel\b\s*=\s*(\d{1,2})\b")
# GROUP BY awlevel makes each output row a single level → nothing sums across
# levels, the same reasoning that lets GROUP BY cipcode suppress the rollup check.
_GROUP_AWLEVEL_RE = re.compile(r"\bgroup\s+by\b.*\bawlevel\b", re.DOTALL)


def _awlevel_code_lists(scan: str) -> list[set[int]]:
    """One code set PER `awlevel IN (...)` list — never a union across the
    statement.

    The union was the first draft, and it flagged correct SQL. The defect being
    detected is codes meeting inside ONE aggregate, and a statement naming 1 and
    21 in SEPARATE predicates is ordinary and right: two `SUM(CASE WHEN awlevel
    IN (...))` columns splitting real levels from short certificates, a share of
    a rollup written as two scalar subqueries, a per-year pivot with an
    `all_levels` column beside a `bachelors` one. The union flagged all three —
    including `_OHIO_RN_AWARDS` in `eval_nl2sql.py`, the reference query added in
    the same PR as the rules, which is as clear a signal as a false positive gets.

    A lint that fires on correct SQL is worse than no lint: prompt step 3 tells
    the model to treat the ⚠ as blocking and re-run, so the cost is a wasted
    iteration or a dropped breakdown column. This module's stated bias is toward
    fewer warnings, and per-list evaluation keeps the live defect
    (`awlevel IN (1,...,20,21)`) while dropping every case above.

    Bare `awlevel = N` equalities are deliberately NOT collected: a single
    equality cannot conflict with itself, and pairing equalities across a
    statement is exactly the union that was wrong."""
    out: list[set[int]] = []
    for group in _AWLEVEL_IN_RE.findall(scan):
        if not _AWLEVEL_CODE_LIST_RE.match(group):
            continue                      # a subquery or an expression, not codes
        codes = {int(n) for n in _AWLEVEL_CODE_RE.findall(group)}
        if codes:
            out.append(codes)
    return out


def _scan(sql: str) -> str:
    """Normalize SQL for pattern matching: strip comments + a trailing ';',
    blank out string-literal contents (so `LIKE '%like%'` can't trip a check),
    and lowercase. Never used to execute — only to inspect."""
    return _mask_string_literals(_strip_sql(sql)).lower()


def lint_sql(sql: str) -> list[LintFinding]:
    """Return advisory findings for known IPEDS aggregation foot-guns. Empty
    list means nothing suspicious was detected (not a correctness guarantee)."""
    scan = _scan(sql)
    findings: list[LintFinding] = []

    if _DISTINCT_YEAR_JOIN_RE.search(scan):
        findings.append(LintFinding(
            "distinct-year-join",
            "a DISTINCT-year subquery joined against c_a makes SQLite full-scan "
            "~8M rows and can hang. Use a constant bound instead: "
            "year > (SELECT MAX(year)-N FROM _years)."))

    if _CIP_LIKE_RE.search(scan):
        findings.append(LintFinding(
            "cip-like-rollup",
            "`cipcode LIKE ...` sums the nested 2-/4-/6-digit CIP rollup rows "
            "together (typically ~4x overcount). Match an exact 6-digit code, "
            "or use cipcode='99' / length(cipcode)=7 for grand totals."))

    # The rollup and second-major checks only make sense when actually summing
    # counts out of the completions table.
    if _C_A_RE.search(scan) and _SUM_RE.search(scan):
        has_cip_guard = (
            _CIP_EQ_RE.search(scan) or _CIP_LEN_RE.search(scan)
            or _CIP_LIKE_RE.search(scan) or _GROUP_CIP_RE.search(scan))
        if not has_cip_guard:
            findings.append(LintFinding(
                "cip-sum-no-guard",
                "SUM over c_a with no CIP filter and no GROUP BY cipcode sums "
                "the 2-/4-/6-digit rollups plus the '99' grand total together "
                "(~4x overcount). Pin an exact 6-digit cipcode, filter "
                "cipcode='99' for a national total, or GROUP BY cipcode."))
        if not _MAJORNUM_RE.search(scan):
            findings.append(LintFinding(
                "majornum-missing",
                "c_a has first-major (majornum=1) and second-major (majornum=2) "
                "rows; summing without a majornum filter double-counts "
                "double-majors. Add majornum=1 for a primary-major headcount."))

        if not _GROUP_AWLEVEL_RE.search(scan):
            lists = _awlevel_code_lists(scan)
            levels = next((s for s in lists if 1 in s and s & {20, 21}),
                          next((s for s in lists
                                if s & _AWLEVEL_ROLLUPS and s & _AWLEVEL_REAL), set()))
            if 1 in levels and levels & {20, 21}:
                findings.append(LintFinding(
                    "awlevel-cert-double-count",
                    "awlevel 20 and 21 are SUBDIVISIONS of awlevel 1 "
                    "(20+21 = 1, exactly), so listing them alongside 1 counts "
                    "every short certificate twice. Drop 20 and 21: the "
                    "mutually-exclusive real levels are 1,2,3,4,5,6,7,8,17,18,19. "
                    "For an all-levels total prefer the rollup awlevel=15 "
                    "(degrees + certificates) or awlevel=12 (degrees only)."))
            if levels & _AWLEVEL_ROLLUPS and levels & _AWLEVEL_REAL:
                findings.append(LintFinding(
                    "awlevel-rollup-mix",
                    "awlevel 12-15 are rollup totals over the real levels "
                    "(13=1+2+4, 14=6+8, 12=3+5+7+17+18+19, 15=12+13+14), so "
                    "summing a rollup together with a real level double-counts. "
                    "Pick one: a rollup on its own, or a list of real levels."))

    return findings
