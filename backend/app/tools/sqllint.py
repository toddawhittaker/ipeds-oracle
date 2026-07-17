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

    return findings
