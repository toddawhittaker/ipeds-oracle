"""Check a hero FIGURE against the query results it claims to summarize.

The app's execution integrity is strong up to the moment run_sql returns rows
(app/tools/sql.py: read-only handle, single SELECT/WITH, watchdog timeout, row
cap). Past that point every number the user sees — the figure, the prose, the
Markdown table, the chart JSON — is re-typed by the LLM out of a Markdown table
in the conversation transcript. Nothing compared those characters back to the
rows SQLite actually returned, and app/llm.py's _extract_figure validates only
SHAPE (valid JSON carrying value + label). So the largest, most authoritative-
looking number on the screen was the least verified thing in the system.

This module is the missing comparison. It is the deterministic counterpart to
app/critic.py in the same way app/tools/sqllint.py is: no DB, no LLM, no
network — pure arithmetic over the QueryResults the turn already retained, so
it can run on every answer.

Two jobs, one kernel:
  * VERIFY (observe-only today) — is the figure's number present in the data, or
    derivable from it? `check_figure` searches the retained results and reports
    a status plus the derivation that matched.
  * COMPUTE — the same `compute` vocabulary is what a later change uses to
    derive the headline server-side from a model-declared provenance, instead of
    trusting the model's own arithmetic.

The operation vocabulary deliberately mirrors prompt.INSTRUCTIONS step 6(ii),
which tells the model exactly which statistics to derive: a net % change over a
range, a leader's share of the total, an average, a max/min. Keeping the two
lists in step is what lets a legitimately-derived figure verify instead of
reading as ungrounded.

KNOWN LIMITATION (why this starts observe-only): with several ops searched
across every numeric column of every result, a number can find a coincidental
match. `check_figure` therefore records WHICH derivation matched, so the
false-positive rate is inspectable before any policy hangs off the status. A
model-declared provenance removes the search entirely and is the real fix.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.tools.sql import QueryResult

# Statuses recorded on usage_log.figure_grounding (migration 21). NULL in the DB
# means the turn was never checked at all.
NO_FIGURE = "no_figure"      # the answer carried no figure — nothing to check
UNCHECKED = "unchecked"      # a figure, but no retained results to check it against
EXACT = "exact"              # the value appears verbatim as a cell in a result
ROUNDED = "rounded"          # matches a cell at the figure's own displayed precision
DERIVED = "derived"          # matches a computed derivation over a result column
UNGROUNDED = "ungrounded"    # no cell and no derivation produced this number

# Ops, matching prompt step 6(ii)'s menu. `share` is a percentage of a column
# total; `pct_change` is the net change across a column in row order.
OPS = ("value", "sum", "mean", "pct_change", "share", "max", "min")

# Relative tolerance for "these two numbers are the same". Generous enough to
# absorb the model's own display rounding (it is told to write thousands
# separators and typically gives a percentage to one decimal), tight enough that
# a genuinely different statistic doesn't slide under it.
_REL_TOL = 1e-3
_ABS_TOL = 1e-9

# Strip the decoration the model is asked to add: thousands separators, a
# currency mark, a percent sign, a leading +, and stray whitespace (including
# the non-breaking and narrow-no-break spaces some models emit as separators).
_DECORATION_RE = re.compile(r"[,\s  $£€%]")
# A leading label like "approx." or "~" occasionally rides along.
_LEADING_JUNK_RE = re.compile(r"^[~≈>≥<≤+]+")

# Columns that are numeric but are IDENTIFIERS/DIMENSIONS, not measures. Summing
# them, averaging them, or taking one row's "share" of their total is
# meaningless — but it still produces a number, and with enough columns in play
# one of those meaningless numbers eventually collides with a real statistic.
#
# This is not hypothetical: the first run of test_grounding.py had a genuine
# +25.0% awards trend "verified" as share(year) — 2021/(2021+2022+2023+2024) =
# 24.98%, inside the match tolerance. `year` is in essentially every IPEDS
# result (step 6(i)(b) mandates a recent-years table), so leaving it eligible
# would have made the whole measurement untrustworthy in the common case.
#
# These columns stay eligible for EXACT/ROUNDED cell matching — a headline may
# legitimately BE a year or an id — they are only barred from aggregation.
_DIMENSION_COL_RE = re.compile(
    r"^(year|.*_year|unitid|opeid|id|.*_id|cipcode|awlevel|majornum|control|"
    r"sector|fips|zip|rank|row_?num.*)$", re.IGNORECASE)


def is_dimension(column: str) -> bool:
    """True when a numeric column is an identifier/dimension rather than a
    measure, so aggregating it would produce a meaningless number."""
    return bool(_DIMENSION_COL_RE.match((column or "").strip()))


@dataclass(frozen=True)
class Derivation:
    """How a figure's number was reproduced from the data."""
    op: str
    result_index: int      # which retained QueryResult (0-based)
    column: str

    def describe(self) -> str:
        return f"{self.op}(q{self.result_index + 1}.{self.column})"


@dataclass(frozen=True)
class GroundingCheck:
    status: str
    derivation: Derivation | None = None
    value: float | None = None   # the parsed figure value, when parseable

    @property
    def grounded(self) -> bool:
        """True when the number was reproduced from the data by some route."""
        return self.status in (EXACT, ROUNDED, DERIVED)


def parse_number(raw) -> float | None:
    """A figure's display string → float, or None when it carries no number.

    Handles what prompt step 6 actually asks the model to write: thousands
    separators ("42,318"), a percentage ("+12.4%"), currency, and the "~"/">"
    hedges that occasionally ride along. Returns None rather than raising — an
    unparseable figure is a non-event for the caller, not an error.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):   # bool is an int subclass; never a figure value
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(raw) else None
    s = _LEADING_JUNK_RE.sub("", str(raw).strip())
    s = _DECORATION_RE.sub("", s)
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _close(a: float, b: float, rel_tol: float = _REL_TOL) -> bool:
    return math.isclose(a, b, rel_tol=rel_tol, abs_tol=_ABS_TOL)


def _displayed_precision_tol(raw) -> float:
    """An absolute tolerance derived from how precisely the figure was WRITTEN.

    "1.2M"-style rounding is legitimate: a model told to write a readable
    headline will round 42,318 to "42,300". The number of digits it chose tells
    us how much rounding it intended, so a value written to the hundreds place
    tolerates ±50. Without this, honest display rounding would read as
    ungrounded and swamp the signal.
    """
    s = str(raw or "")
    frac = re.search(r"\.(\d+)", s)
    if frac:
        return 0.5 * (10 ** -len(frac.group(1)))
    digits = _DECORATION_RE.sub("", _LEADING_JUNK_RE.sub("", s.strip()))
    digits = digits.lstrip("-")
    if not digits.isdigit():
        return 0.0
    trailing_zeros = len(digits) - len(digits.rstrip("0"))
    return 0.5 * (10 ** trailing_zeros) if trailing_zeros else 0.0


def _as_number(cell) -> float | None:
    """A result cell → float, or None when it isn't numeric. Numeric-looking
    TEXT counts (SQLite is loosely typed and IPEDS code columns are text), but a
    label like 'Ohio' does not."""
    if cell is None or isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        return float(cell) if math.isfinite(cell) else None
    if isinstance(cell, str):
        try:
            v = float(cell.strip().replace(",", ""))
        except ValueError:
            return None
        return v if math.isfinite(v) else None
    return None


def numeric_columns(result: QueryResult) -> dict[str, list[float]]:
    """Per-column numeric cells, in ROW ORDER (pct_change depends on it).

    A column is included only if EVERY non-null cell parses as a number, so a
    mixed label column ("2024", "provisional") never masquerades as a series.
    Nulls are skipped rather than disqualifying the column.
    """
    if not result or not result.columns:
        return {}
    out: dict[str, list[float]] = {}
    for idx, name in enumerate(result.columns):
        values: list[float] = []
        usable = True
        for row in result.rows:
            if idx >= len(row):
                continue
            cell = row[idx]
            if cell is None:
                continue
            v = _as_number(cell)
            if v is None:
                usable = False
                break
            values.append(v)
        if usable and values:
            out[name] = values
    return out


def compute(op: str, values: list[float], index: int | None = None) -> float | None:
    """Apply `op` to a column's values. Returns None when the op is unknown or
    the data can't support it (too few points, a zero denominator, an index out
    of range) — never raises, so a bad provenance degrades instead of breaking
    a turn.

    `index` selects the row for the row-scoped ops (`value`, `share`); it
    defaults to the first row.
    """
    if not values:
        return None
    i = 0 if index is None else index
    if op == "value":
        return values[i] if -len(values) <= i < len(values) else None
    if op == "sum":
        return math.fsum(values)
    if op == "mean":
        return math.fsum(values) / len(values)
    if op == "max":
        return max(values)
    if op == "min":
        return min(values)
    if op == "pct_change":
        # Net change across the range, in row order — the "trend" headline.
        if len(values) < 2 or values[0] == 0:
            return None
        return (values[-1] - values[0]) / abs(values[0]) * 100.0
    if op == "share":
        total = math.fsum(values)
        if total == 0 or not (-len(values) <= i < len(values)):
            return None
        return values[i] / total * 100.0
    return None


def _match_in_column(target: float, raw_value, column: str,
                     values: list[float]) -> tuple[str, str] | None:
    """Try to reproduce `target` from one column. Returns (status, op) — the op
    is the point, since it is what makes a coincidental match recognizable when
    reviewing recorded statuses — or None when nothing reproduced it.

    Ordered cheapest-and-most-certain first, so a verbatim cell is never
    reported as a coincidental derivation.
    """
    for v in values:
        if _close(target, v):
            return EXACT, "value"
    tol = _displayed_precision_tol(raw_value)
    if tol:
        for v in values:
            if abs(target - v) <= tol:
                return ROUNDED, "value"
    # Aggregations only make sense over a MEASURE. See _DIMENSION_COL_RE.
    if is_dimension(column):
        return None
    for op in ("sum", "pct_change", "mean", "max", "min"):
        got = compute(op, values)
        if got is not None and _close(target, got):
            return DERIVED, op
    # `share` is row-scoped: any row's share of the column total.
    for i in range(len(values)):
        got = compute("share", values, index=i)
        if got is not None and _close(target, got):
            return DERIVED, "share"
    return None


def check_figure(figure: dict | None,
                 results: list[QueryResult] | None) -> GroundingCheck:
    """Can this figure's number be reproduced from the retained results?

    Reports a status and, when it matched, the derivation that reproduced it —
    the derivation is the point: it is what makes a coincidental match
    recognizable when reviewing the recorded statuses.

    Purely observational. It changes no answer and blocks nothing.
    """
    if not figure or not isinstance(figure, dict):
        return GroundingCheck(NO_FIGURE)
    target = parse_number(figure.get("value"))
    if target is None:
        # A non-numeric headline ("Ohio State") is a legitimate figure; there is
        # simply no arithmetic to check.
        return GroundingCheck(NO_FIGURE)
    if not results:
        return GroundingCheck(UNCHECKED, value=target)

    raw_value = figure.get("value")
    best: tuple[str, Derivation] | None = None
    for r_idx, result in enumerate(results):
        for column, values in numeric_columns(result).items():
            match = _match_in_column(target, raw_value, column, values)
            if match is None:
                continue
            status, op = match
            derivation = Derivation(op=op, result_index=r_idx, column=column)
            if status == EXACT:
                return GroundingCheck(EXACT, derivation, target)
            # Keep looking for an exact match, but remember the weaker one.
            if best is None or (best[0] == DERIVED and status == ROUNDED):
                best = (status, derivation)
    if best:
        return GroundingCheck(best[0], best[1], target)
    return GroundingCheck(UNGROUNDED, value=target)
