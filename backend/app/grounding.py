"""Check the numbers an answer shows — the hero FIGURE and the results TABLE —
against the query results they claim to summarize.

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
MALFORMED = "malformed"      # a figure fence WAS emitted but didn't parse into one
UNCHECKED = "unchecked"      # a figure, but no retained results to check it against
EXACT = "exact"              # the value appears verbatim as a cell in a result
ROUNDED = "rounded"          # matches a cell at the figure's own displayed precision
DERIVED = "derived"          # matches a computed derivation over a result column
UNGROUNDED = "ungrounded"    # no cell and no derivation produced this number

# Ops, matching prompt step 6(ii)'s menu. `share` is a percentage of a column
# total; `pct_change` is the net change across a column in row order, and `diff`
# is that same change in ABSOLUTE terms.
#
# `diff` is here because its absence produced the first false `ungrounded` seen
# in production: the model led a trend with "217 — Net increase since 2021" off a
# 550→767 table. 767-550=217 is exactly right, but step 6(ii) asks for the net
# "% change", so nothing in the vocabulary could reproduce the absolute form the
# model actually chose. A kernel that cannot reproduce a CORRECT number
# manufactures evidence of model error, which is the most damaging way for this
# measurement to be wrong.
OPS = ("value", "sum", "mean", "pct_change", "diff", "share", "max", "min")

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
# Magnitude suffixes. The prompt asks for thousands separators, but models
# routinely write a headline as "1.2M" anyway. Without these such a figure fails
# to parse and is filed as `no_figure` — silently DROPPED from the measurement
# rather than checked, which biases the very rate this module exists to report.
_MAGNITUDE_RE = re.compile(
    r"^(-?[\d.]+)\s*(k|m|b|bn|thousand|million|billion)$", re.IGNORECASE)
_MAGNITUDES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6,
               "b": 1e9, "bn": 1e9, "billion": 1e9}

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
    mag = _MAGNITUDE_RE.match(s)
    if mag:
        try:
            v = float(mag.group(1)) * _MAGNITUDES[mag.group(2).lower()]
        except (ValueError, KeyError):
            return None
        return v if math.isfinite(v) else None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _close(a: float, b: float, rel_tol: float = _REL_TOL) -> bool:
    return math.isclose(a, b, rel_tol=rel_tol, abs_tol=_ABS_TOL)


# A display rounding may never move the number by more than this share of it.
# Trailing zeros alone are an unreliable signal of INTENDED precision: "1,000"
# has three of them, which would otherwise license a +/-500 window and let the
# figure "1,000" verify against a true value of 1,400. Honest headline rounding
# is small in relative terms (42,300 for 42,318 is 0.04%), so capping at 5%
# keeps every legitimate case while refusing to call a 40% miss a rounding.
_MAX_ROUNDING_SHARE = 0.05


def _displayed_precision_tol(raw, target: float) -> float:
    """An absolute tolerance derived from how precisely the figure was WRITTEN.

    Display rounding is legitimate: a model told to write a readable headline
    will round 42,318 to "42,300". The digits it chose tell us how much rounding
    it intended, so a value written to the hundreds place tolerates +/-50.
    Without this, honest rounding would read as ungrounded and swamp the signal
    — but see _MAX_ROUNDING_SHARE for why it is also capped.
    """
    s = str(raw or "")
    frac = re.search(r"\.(\d+)", s)
    if frac:
        tol = 0.5 * (10 ** -len(frac.group(1)))
    else:
        digits = _DECORATION_RE.sub("", _LEADING_JUNK_RE.sub("", s.strip()))
        digits = digits.lstrip("-")
        if not digits.isdigit():
            return 0.0
        trailing_zeros = len(digits) - len(digits.rstrip("0"))
        # No trailing zeros still implies rounding to the UNITS place: a model
        # that writes "39%" for a true 39.45% has rounded, and granting 0
        # tolerance there would read honest rounding as an invented number.
        tol = 0.5 * (10 ** trailing_zeros)
    return min(tol, abs(target) * _MAX_ROUNDING_SHARE)


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
    if op == "diff":
        # The same net change in ABSOLUTE terms. Deliberately no non-zero
        # baseline guard (unlike pct_change): starting from 0 makes a ratio
        # undefined but an absolute change perfectly well defined.
        if len(values) < 2:
            return None
        return values[-1] - values[0]
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
    tol = _displayed_precision_tol(raw_value, target)
    if tol:
        for v in values:
            if abs(target - v) <= tol:
                return ROUNDED, "value"
    # Aggregations only make sense over a MEASURE. See _DIMENSION_COL_RE.
    if is_dimension(column):
        return None

    def reproduces(got: float | None) -> bool:
        """Did this op land on the target, allowing for how the figure was
        WRITTEN? The display tolerance has to apply to derivations too, not just
        to raw cells — a derived headline is usually a percentage, which is
        precisely where a model rounds ("39%" for a true 39.45%). Checking
        derivations at full precision while forgiving cells would flag the
        rounding the prompt itself asks for."""
        return got is not None and (_close(target, got) or abs(target - got) <= tol)

    for op in ("sum", "pct_change", "diff", "mean", "max", "min"):
        if reproduces(compute(op, values)):
            return DERIVED, op
    # `share` is row-scoped: any row's share of the column total.
    for i in range(len(values)):
        if reproduces(compute("share", values, index=i)):
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
    match = _reconcile_value(target, figure.get("value"), results)
    if match is None:
        return GroundingCheck(UNGROUNDED, value=target)
    status, derivation = match
    return GroundingCheck(status, derivation, target)


def _reconcile_value(target: float, raw_value, results: list[QueryResult],
                     allow_dimension: bool = True) -> tuple[str, Derivation] | None:
    """Reproduce `target` from any column of any retained result, returning the
    STRONGEST route — EXACT short-circuits; otherwise the best of ROUNDED/DERIVED
    — or None when nothing reproduced it.

    The shared reconciliation kernel behind check_figure and check_table.
    `raw_value` is the number as WRITTEN (its precision drives the display-rounding
    tolerance).

    `allow_dimension` (default True) lets a value match a DIMENSION/code column
    exactly — right for the hero FIGURE (a headline can legitimately BE a year or
    a code). check_table passes False: a table MEASURE cell must be verified by a
    MEASURE column, never by a code column it merely collides with (a small count
    "3" matching an `awlevel` 3 is a spurious match, not grounding)."""
    best: tuple[str, Derivation] | None = None
    for r_idx, result in enumerate(results):
        for column, values in numeric_columns(result).items():
            if not allow_dimension and is_dimension(column):
                continue  # a code/dimension column can't stand in for a data cell
            match = _match_in_column(target, raw_value, column, values)
            if match is None:
                continue
            status, op = match
            derivation = Derivation(op=op, result_index=r_idx, column=column)
            if status == EXACT:
                return EXACT, derivation
            # Keep looking for an exact match, but remember the weaker one.
            if best is None or (best[0] == DERIVED and status == ROUNDED):
                best = (status, derivation)
    return best


# --- Table grounding -----------------------------------------------------------
# The results TABLE is the model re-typing the query rows one-for-one — the
# densest concentration of numbers on screen, and (until this) as unverified as
# the figure once was. check_table grades the MEASURE columns only (rank ordinals
# and dimension columns are excluded — see _is_measure_column — so the rate is a
# clean transcription-accuracy signal for the DATA, not dragged down by a
# model-added Rank column that was never in the DB). Each graded cell is
# reconciled with the same kernel as the figure (_reconcile_value: full
# reproduction — verbatim / display-rounded / derivable) but with
# `allow_dimension=False` — a measure cell is verified only by a MEASURE
# result-column, never by a code/dimension column it merely collides with (a
# small count "3" is not grounded by an `awlevel` 3). A legitimately computed
# measure (a share/%-change column) still grounds instead of false-alarming, at
# the cost of the same coincidental-match bias noted in this
# module's KNOWN LIMITATION. Observe-only: statuses land on
# usage_log.table_grounding (migration 25) and drive Admin -> Usage; nothing is
# altered or blocked. The raw rows stay in messages.results, so an all-columns
# variant is recomputable offline.

# Statuses recorded on usage_log.table_grounding (migration 25). NO_TABLE and
# UNCHECKED carry zero cell counts, so they self-exclude from the SUM-based rate.
TABLE_MATCHED = "matched"      # every checked numeric cell reproduced
TABLE_PARTIAL = "partial"      # some reproduced, some didn't
TABLE_UNMATCHED = "unmatched"  # no numeric cell reproduced
NO_TABLE = "no_table"          # no gradable numeric table cell in the answer
# UNCHECKED (above) is reused: a table was present but no results to check it against.

# A GFM delimiter row: only dashes/colons/pipes/spaces, e.g. `| --- | :--: |`.
# It must carry a pipe to be a table separator (a bare `---` is a horizontal rule).
_TABLE_SEP_RE = re.compile(r"^\s*\|?(\s*:?-{1,}:?\s*\|)+\s*:?-{0,}:?\s*$")


@dataclass(frozen=True)
class TableGroundingCheck:
    status: str
    cells_checked: int = 0
    cells_matched: int = 0


def _split_row(line: str) -> list[str]:
    """A GFM table row → trimmed cell strings, dropping the empty leading/trailing
    cells the surrounding pipes create."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_markdown_tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """Extract GFM pipe tables from `text` as `(header_cells, body_rows)` tuples.
    The `---` separator row is dropped; the header is kept so a column can be
    classified measure-vs-dimension. Fenced code regions (```...```) are skipped
    so a ```chart JSON block, still present in the shipped answer, is never read
    as a table."""
    tables: list[tuple[list[str], list[list[str]]]] = []
    lines = (text or "").splitlines()
    in_fence = False
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        # A header is a pipe row immediately followed by a delimiter row.
        if (not in_fence and "|" in line and i + 1 < n
                and "|" in lines[i + 1] and _TABLE_SEP_RE.match(lines[i + 1])):
            header = _split_row(line)
            i += 2  # consume header + separator
            body: list[list[str]] = []
            while (i < n and "|" in lines[i]
                   and not lines[i].lstrip().startswith("```")):
                body.append(_split_row(lines[i]))
                i += 1
            if body:
                tables.append((header, body))
            continue
        i += 1
    return tables


def _is_measure_column(header: str, values: list[float]) -> bool:
    """True when a numeric table column carries DATA (a measure), not row identity.

    A rank/ordinal or dimension column is not a measure: its cells aren't data
    transcribed from the query, so grading them muddies the transcription-accuracy
    signal — worst of all a model-added Rank column (1,2,3…) is never in the
    result and reads as a false `unmatched`. Excluded when the header names a
    dimension (rank/year/unitid/cipcode/id/… — the same `is_dimension` used to bar
    aggregation) OR the values are a pure 1..N sequence (a rank ordinal whatever
    the header says — "#", "No.")."""
    if is_dimension(header):
        return False
    if len(values) >= 2 and values == [float(k) for k in range(1, len(values) + 1)]:
        return False
    return True


def check_table(answer_markdown: str,
                results: list[QueryResult] | None) -> TableGroundingCheck:
    """Can the MEASURE cells of the answer's Markdown table(s) be reproduced from
    the retained query results? Observe-only, like check_figure.

    Grades numeric cells in MEASURE columns only (see _is_measure_column) — rank
    ordinals and dimension columns are excluded so the rate is a clean
    transcription-accuracy signal for the data, not dragged down by a model-added
    Rank column that was never in the DB. Each graded cell is reconciled via
    _reconcile_value with `allow_dimension=False`: a measure cell must be verified
    by a MEASURE result-column, never by a code/dimension column it merely
    collides with (a small count "3" is not grounded by an `awlevel` 3).
    NO_TABLE/UNCHECKED carry no counts so they don't move the rate."""
    cells: list[tuple[float, str]] = []
    for header, body in parse_markdown_tables(answer_markdown or ""):
        width = max((len(r) for r in body), default=0)
        for ci in range(width):
            col_head = header[ci] if ci < len(header) else ""
            col = [(raw, parse_number(raw))
                   for r in body if ci < len(r) for raw in (r[ci],)]
            nums = [(raw, v) for raw, v in col if v is not None]
            if not nums:
                continue  # a label column (states, institutions) — nothing to grade
            if not _is_measure_column(col_head, [v for _, v in nums]):
                continue  # a rank ordinal or dimension — not a transcribed measure
            cells.extend((v, raw) for raw, v in nums)
    if not cells:
        return TableGroundingCheck(NO_TABLE)
    if not results:
        return TableGroundingCheck(UNCHECKED)
    matched = sum(1 for v, raw in cells
                  if _reconcile_value(v, raw, results, allow_dimension=False) is not None)
    checked = len(cells)
    if matched == checked:
        status = TABLE_MATCHED
    elif matched == 0:
        status = TABLE_UNMATCHED
    else:
        status = TABLE_PARTIAL
    return TableGroundingCheck(status, cells_checked=checked, cells_matched=matched)
