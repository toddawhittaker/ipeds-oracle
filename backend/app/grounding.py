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

TRUNCATION. app/tools/sql.py cuts a result at sql_row_cap_model (200) and
prompt.py tells the model to fix a cut ranking with a separate
`SELECT SUM(...)` — but a model that instead sums the visible page gets a
wrong total that, before this, reconciled against those SAME partial rows and
came back "verified" (sql.py's own `⚠ AGGREGATION CHECK (truncated)` note is
the upstream half of this same problem: it warns the MODEL not to aggregate a
cut page; this is the check that must not corroborate it if the model does
anyway). The rule: a route may run over a truncated result IFF its value is
invariant to appending the rows that were cut. Truncation drops a SUFFIX of
rows, so a value at a KNOWN ROW INDEX is invariant (a verbatim cell, a
row-wise op, a row-anchored prev-row op) and stays allowed; anything that
reads the column's EXTENT — sum, mean, share, pct_change, diff, and a
cross-result total/complement sourced FROM a truncated result — is not, and
must refuse.

`max`/`min` are a DOCUMENTED EXCEPTION, not a gap in the gate: `compute("max"
/"min", …)` always returns an actual cell — the column's largest/smallest
value IS one of its cells — so a claimed max/min is indistinguishable from a
verbatim-cell claim and is caught by the always-allowed EXACT "value" route
before the gate is ever reached. Refusing every cell that happens to sit at a
page extremum would reject honest transcriptions, and a false NEGATIVE (a
correct answer flagged wrong) is the direction this module treats as more
damaging than a false positive — see the KNOWN LIMITATION above. What the gate
DOES change for them: "max"/"min" itself never fires as the REPORTED
derivation over a truncated result; the number still grounds, just via
"value" rather than "max"/"min". Grounding attests reproduction of the
number, not that the model's "this is the maximum" reading of a cut page is
correct — that reading isn't and can't be checked here.

The gate is therefore keyed on the (result, axis) pair at each call site, not
inside `compute()` itself, since `compute()` has no way to know which axis its
caller is using a column on.

No new STATUS is introduced by any of this — a refused figure still records
the existing UNGROUNDED, and a refused table cell simply isn't counted toward
cells_matched — but that does NOT make the gate inert. UNGROUNDED already
drives real behaviour downstream: llm._maybe_retry_figure SUPPRESSES a
retry-recovered figure that grades ungrounded, and llm._s5_fabricated can
degrade a tool-budget-exhausted answer when an ungrounded figure pairs with no
grounded table. So a truncated turn that used to verify (wrongly) now trips
both of those, and Admin -> Usage's grounding rates move down on truncated
turns BY DESIGN — a real, not merely observational, effect. Both downstream
behaviours are correct to keep (a partial-page total really is wrong); this
paragraph exists so "no new status" is never misread as "nothing changes".
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace

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
# A figure the RETRY forced, found ungrounded, and therefore withheld — the turn
# shipped no figure at all. Its own status because `ungrounded` put it in the
# Grounded-figures denominator as a miss, against that stat's definition ("turns
# that led with a hero figure"): 10 of the 25 ungrounded turns in the real
# usage_log were suppressions, reading 88.2% where the truth was 92.5%. Set only
# by llm._maybe_retry_figure; check_figure never returns it.
SUPPRESSED = "retry_suppressed"

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
# `row_total` is the SECOND instance of the same class as `diff` above, found
# the same way — a correct figure reported `ungrounded`. Every other op
# aggregates DOWN a column; a figure that totals ACROSS one row of a pivoted
# table had no route at all. That is the canonical shape of a by-award-level or
# by-category breakdown, and exactly what step 6(ii) invites for a peak-year hero
# stat: "324,575 — peak national nursing degrees in 2022" is the row-wise sum of
# associate+bachelor+master+doctorate+certificate for 2022, exactly reproducible
# and previously unreproducible by this kernel.
OPS = ("value", "sum", "mean", "pct_change", "diff", "share", "max", "min", "row_total")

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
# Markdown emphasis wrapping a cell: "**30,568**", "`225`", "*4.3%*".
#
# Without this the cell does not parse and is DROPPED from grading entirely —
# not counted, not checked, silently invisible. Measured on a live answer: 7 of
# 14 numeric cells escaped because the model bolded them, which is its own
# convention for the numbers that matter most. That undercounts the ✓ mark's
# coverage while it sounds authoritative, and leaves the emphasized figures the
# least verified thing in the table.
_EMPHASIS_RE = re.compile(r"^[*_`~]+|[*_`~]+$")
# A bound rather than a value: "<0.1%", ">1,000", "≤5". The model writes these
# when a share rounds below its display precision, and reading the digits as an
# exact quantity turns a CORRECT hedge into an unreproducible number — observed
# live, where "<0.1%" stood for a true 0.0179% and graded as a miss.
_HEDGE_RE = re.compile(r"^\s*([<≤>≥])")
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

# The same dimensions as written by a HUMAN, for Markdown table headers:
# `cipcode` arrives as "CIP" or "CIP Code", `awlevel` as "Award Level".
# Observed live — a "CIP" column of codes (52, 51, 13…) was graded as a measure,
# could never ground (its only match is a dimension column, which check_table
# bars), and produced five false misses on a correct answer plus one false GROUND
# where the code 11 collided with a share.
#
# EXACT membership, deliberately, with no suffix wildcards. Reusing the regex
# above against a space-normalized header instead looked tidier and was wrong:
# "Change from Prior Year" became "Change_from_Prior_Year", hit `.*_year`, and
# five legitimate measure cells went SILENTLY UNGRADED — the same invisible
# non-coverage the emphasis fix had just removed. The two naming domains are
# different and the wildcards only make sense in the snake_case one.
_DIMENSION_HEADERS = frozenset({
    "cip", "cipcode", "awlevel", "awardlevel", "unitid", "opeid", "id", "year",
    "rank", "majornum", "control", "sector", "fips", "zip", "rownum",
})


def is_dimension(column: str) -> bool:
    """True when a numeric column is an identifier/dimension rather than a
    measure, so aggregating it would produce a meaningless number."""
    name = (column or "").strip()
    if _DIMENSION_COL_RE.match(name):
        return True
    return re.sub(r"[\s_]+", "", name).lower() in _DIMENSION_HEADERS


@dataclass(frozen=True)
class Derivation:
    """How a figure's number was reproduced from the data."""
    op: str
    result_index: int      # which retained QueryResult (0-based); -1 spans several
    column: str

    def describe(self) -> str:
        # A cross-result derivation has no single owning result, and its column
        # already names both operands ("q1.bachelors/q2.state_total"), so the
        # usual q-prefix would double up into cross_share(q1.q1.x/q2.y).
        if self.result_index < 0:
            return f"{self.op}({self.column})"
        return f"{self.op}(q{self.result_index + 1}.{self.column})"


@dataclass(frozen=True)
class GroundingCheck:
    status: str
    derivation: Derivation | None = None
    value: float | None = None   # the parsed figure value, when parseable
    # True only when the truncation gate is SPECIFICALLY what refused this
    # figure: nothing reproduced it with the gate applied, but a second
    # reconciliation pass with every truncated result's gate forced OPEN would
    # have matched it. Telemetry only (see llm._derivation_label), so the
    # backend-only figure_derivation string can record WHY an UNGROUNDED figure
    # was refused instead of merely reproduced-or-not. Deliberately NOT
    # `any(r.truncated for r in results)` — that reads True whenever ANY
    # retained result was cut, whether or not it had anything to do with THIS
    # figure, and over-reports on exactly the ranking turns where a wholly
    # invented number is also common (see check_figure). Does not change
    # `status` or `grounded` — no new status value is introduced (see the
    # module docstring's TRUNCATION paragraph).
    blocked_by_truncation: bool = False

    @property
    def grounded(self) -> bool:
        """True when the number was reproduced from the data by some route."""
        return self.status in (EXACT, ROUNDED, DERIVED)


def _strip_emphasis(raw) -> str:
    """A cell with its Markdown emphasis removed, both ends. See _EMPHASIS_RE."""
    return _EMPHASIS_RE.sub("", str(raw if raw is not None else "").strip())


def parse_hedge(raw) -> str | None:
    """'<' or '>' when the cell states a BOUND, else None.

    `≤`/`≥` normalize to `<`/`>`: the inclusive/exclusive distinction is
    meaningless at display precision, and treating them alike keeps the
    comparison one branch instead of four.
    """
    m = _HEDGE_RE.match(_strip_emphasis(raw))
    if not m:
        return None
    return "<" if m.group(1) in "<≤" else ">"


def satisfies_hedge(op: str, bound: float, candidate: float | None) -> bool:
    """Does `candidate` satisfy a cell written as "<bound" / ">bound"?

    The model claimed only an inequality, so verification can only check the
    inequality — this is deliberately weaker evidence than an equality match, and
    a hedged cell is correspondingly easier to satisfy. That is the honest
    reading of the claim rather than a loosened tolerance: the alternative,
    comparing the bound's digits as if they were the quantity, marks correct
    answers wrong (see _HEDGE_RE).
    """
    if candidate is None:
        return False
    return candidate < bound if op == "<" else candidate > bound


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
    s = _LEADING_JUNK_RE.sub("", _strip_emphasis(raw))
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


# A display rounding read from TRAILING ZEROS may never move the number by more
# than this share of it. Trailing zeros alone are an unreliable signal of
# INTENDED precision: "1,000" has three of them, which would otherwise license a
# +/-500 window and let the figure "1,000" verify against a true value of 1,400.
# Honest headline rounding is small in relative terms (42,300 for 42,318 is
# 0.04%), so capping at 5% keeps every legitimate case while refusing to call a
# 40% miss a rounding.
#
# It governs the INTEGER branch only — see _displayed_precision_tol. An explicit
# decimal is not ambiguous the way trailing zeros are, and applying the cap there
# too was a defect: it bound on every value below one unit of its own leading
# place and refused correct numbers.
_MAX_ROUNDING_SHARE = 0.05


def _displayed_precision_tol(raw, target: float) -> float:
    """An absolute tolerance derived from how precisely the figure was WRITTEN.

    Display rounding is legitimate: a model told to write a readable headline
    will round 42,318 to "42,300". The digits it chose tell us how much rounding
    it intended, so a value written to the hundreds place tolerates +/-50.
    Without this, honest rounding would read as ungrounded and swamp the signal.

    Two branches, and only one of them is ambiguous:

      * an EXPLICIT DECIMAL states its precision outright — "0.4" means one
        decimal place, +/-0.05, and there is nothing to second-guess. The window
        it yields also shrinks with every decimal written, so it is
        self-limiting (never wider than +/-0.5);
      * an INTEGER has to be read from trailing zeros, which is a guess, so that
        branch is additionally capped by _MAX_ROUNDING_SHARE.

    Capping the decimal branch too was a live defect. `abs(target) * 0.05` binds
    whenever |target| < 10^-k for a value written to k decimals — i.e. EVERY
    sub-1% figure at one decimal place, which is exactly the shape a flat trend
    takes. A correct "+0.4%" for a true 0.3535% needs the +/-0.05 its own
    notation declares and was given +/-0.02, so it graded `ungrounded` (no ✓ for
    the reader) and the same arithmetic in a table cell raised the ⚠ "Check N of
    M values" caution on a correct table. Measured over 64 retained messages,
    removing the cap here recovered one figure and one cell and changed the
    fabricated-match rate on neither probe.
    """
    s = str(raw or "")
    frac = re.search(r"\.(\d+)", s)
    if frac:
        # Written precision, uncapped: unambiguous, and self-limiting.
        return 0.5 * (10 ** -len(frac.group(1)))
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


def aligned_numeric_columns(result: QueryResult) -> dict[str, list[float | None]]:
    """Per-column numeric cells kept ALIGNED TO ROW INDEX — None where the cell
    is null or the row is short.

    Row alignment is what lets a value be looked up by the row it belongs to
    rather than anywhere in the column (see _anchor_rows). numeric_columns() is
    this with the Nones dropped; keeping one definition means the two can't drift
    on which columns count as numeric.
    """
    if not result or not result.columns:
        return {}
    out: dict[str, list[float | None]] = {}
    for idx, name in enumerate(result.columns):
        values: list[float | None] = []
        usable = True
        for row in result.rows:
            cell = row[idx] if idx < len(row) else None
            if cell is None:
                values.append(None)
                continue
            v = _as_number(cell)
            if v is None:
                usable = False
                break
            values.append(v)
        if usable and any(v is not None for v in values):
            out[name] = values
    return out


def numeric_columns(result: QueryResult) -> dict[str, list[float]]:
    """Per-column numeric cells, in ROW ORDER (pct_change depends on it).

    A column is included only if EVERY non-null cell parses as a number, so a
    mixed label column ("2024", "provisional") never masquerades as a series.
    Nulls are skipped rather than disqualifying the column.
    """
    return {name: [v for v in values if v is not None]
            for name, values in aligned_numeric_columns(result).items()}


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
                     values: list[float], *, whole_column: bool = True
                     ) -> tuple[str, str] | None:
    """Try to reproduce `target` from one column. Returns (status, op) — the op
    is the point, since it is what makes a coincidental match recognizable when
    reviewing recorded statuses — or None when nothing reproduced it.

    Ordered cheapest-and-most-certain first, so a verbatim cell is never
    reported as a coincidental derivation.

    `whole_column` (default True) gates the aggregation routes below the
    verbatim/hedge checks: False means the caller's result was truncated, so
    every route that reads this column's EXTENT (sum/mean/max/min/pct_change/
    diff/share) would be reading a cut suffix as if it were the whole column —
    see the module docstring's TRUNCATION paragraph. The verbatim and hedge
    routes above run either way; a value at a known row is unaffected by which
    rows are missing.
    """
    # A hedged cell ("<0.1%") states a BOUND, so every route below tests the
    # inequality instead of equality — see satisfies_hedge.
    hedge = parse_hedge(raw_value)
    tol = 0.0 if hedge else _displayed_precision_tol(raw_value, target)
    if hedge:
        for v in values:
            if satisfies_hedge(hedge, target, v):
                return ROUNDED, "bound"
    else:
        for v in values:
            if _close(target, v):
                return EXACT, "value"
        if tol:
            for v in values:
                if abs(target - v) <= tol:
                    return ROUNDED, "value"
    # Aggregations only make sense over a MEASURE (see _DIMENSION_COL_RE), and
    # only over a column whose EXTENT is actually known — a truncated result's
    # column is missing however many rows were cut, so its sum/mean/etc. would
    # be the same wrong partial total sql.py's note warns the model against.
    if not whole_column or is_dimension(column):
        return None

    def reproduces(got: float | None) -> bool:
        """Did this op land on the target, allowing for how the figure was
        WRITTEN? The display tolerance has to apply to derivations too, not just
        to raw cells — a derived headline is usually a percentage, which is
        precisely where a model rounds ("39%" for a true 39.45%). Checking
        derivations at full precision while forgiving cells would flag the
        rounding the prompt itself asks for."""
        if hedge:
            return satisfies_hedge(hedge, target, got)
        return got is not None and (_close(target, got) or abs(target - got) <= tol)

    for op in ("sum", "pct_change", "diff", "mean", "max", "min"):
        if reproduces(compute(op, values)):
            return DERIVED, f"bound_{op}" if hedge else op
    # `share` is row-scoped: any row's share of the column total.
    for i in range(len(values)):
        if reproduces(compute("share", values, index=i)):
            return DERIVED, "bound_share" if hedge else "share"
    return None


def _ungate(results: list[QueryResult]) -> list[QueryResult]:
    """The same results with every truncation flag forced OFF.

    Used ONLY to answer "would this have matched if the truncation gate were
    open?" — for `blocked_by_truncation` and `TableGroundingCheck.cells_blocked`,
    never to ground anything for real. A shallow `dataclasses.replace`, so the
    row data itself is shared, not copied.
    """
    return [replace(r, truncated=False) if r.truncated else r for r in results]


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
    match = _reconcile_value(target, raw_value, results)
    if match is None:
        # Telemetry only (see GroundingCheck.blocked_by_truncation): re-run the
        # SAME reconciliation with every truncated result's gate forced open,
        # and only report "blocked" when that second pass is what would have
        # matched. `any(r.truncated for r in results)` alone over-reports — a
        # wholly invented number still refuses with the gate open, and would
        # be wrongly marked "truncated" just because some retained result in
        # the mix happened to be cut.
        blocked = (any(r.truncated for r in results)
                   and _reconcile_value(target, raw_value, _ungate(results)) is not None)
        return GroundingCheck(UNGROUNDED, value=target, blocked_by_truncation=blocked)
    status, derivation = match
    return GroundingCheck(status, derivation, target)


def measure_columns(result: QueryResult) -> dict[str, list[float | None]]:
    """The row-aligned MEASURE columns of a result — numeric columns minus the
    dimension/rank ones. The unit both the row-wise routes work in."""
    aligned = aligned_numeric_columns(result)
    return {name: values for name, values in aligned.items()
            if _is_measure_column(name, [v for v in values if v is not None])}


def row_series(result: QueryResult) -> list[list[float]]:
    """Every result ROW's measure values, in COLUMN order — one list per row.

    The row-wise counterpart to a column's value list, and the series every
    row-wise op runs over. Built from row-ALIGNED columns because a per-column
    null-skip would misalign the columns against each other — pairing row 3 of
    one column with row 4 of the next.

    A row's series is empty unless at least two measure columns supply a value:
    with one, the "series" is a single cell, which the `value` route already
    covers. Computed for the whole result at once because the callers grade many
    cells against it and rebuilding the column map per cell is quadratic.
    """
    if not result or not result.columns:
        return []
    cols = list(measure_columns(result).values())
    if len(cols) < 2:
        return []
    out: list[list[float]] = []
    for i in range(len(result.rows)):
        vals = [c[i] for c in cols if i < len(c)]
        out.append([] if any(v is None for v in vals) or len(vals) < 2
                   else [v for v in vals if v is not None])
    return out


def _row_totals(result: QueryResult) -> list[tuple[int, float]]:
    """(row index, row total) for every row whose measure cells are all present.

    Now just `sum` over each row's series — row totals were the first row-wise
    route and had their own bespoke implementation; folding them into row_series
    keeps ONE definition of "a row's measure values" for every row-wise op. The
    row INDEX rides along so the derivation can name the actual result row rather
    than a position in this filtered list.
    """
    return [(i, math.fsum(s)) for i, s in enumerate(row_series(result)) if s]


# --- Cross-result derivations --------------------------------------------------
# Every op so far works INSIDE one result. But the model routinely splits a
# question across queries: get the top N rows from one, then the denominator from
# a second `SELECT SUM(...)`. Each share is then one result's row over another
# result's scalar, and nothing could reproduce it.
#
# Observed live on an ordinary question ("what share of Ohio's public bachelor's
# degrees does each of the top 5 account for?"): all eight unreproduced cells AND
# the hero figure were exact arithmetic across two results —
# 11,620/45,883 = 25.3%, 30,568/45,883 = 66.6%, 45,883-30,568 = 15,315.
#
# The ingredient is a TOTAL: a measure column's sum, from any result. A one-row
# `SELECT SUM(x)` result is just that with one value, so both come from the same
# rule. Complements (T - S) are included because "all others" is the other half
# of every share breakdown — and it is the numerator of the next share, which is
# why a plain difference route is not enough.

# Bounds on the candidate set. The routes below are quadratic in the number of
# totals, and this runs on every answer, so a wide many-column result must not
# turn into a large search — which would cost both time and precision.
# How many result rows one table row may anchor to. A PIVOT is the reason a
# group exists at all: one table row per year, one column per category, means
# that row's numbers live in as many result rows as there are categories. Real
# pivots are narrow — a handful of categories — so a group larger than this is
# not "this row's rows", it is most of the result, i.e. the unrestricted column
# search under another name. Refusing it keeps the precision row anchoring was
# introduced to buy back.
_MAX_ANCHOR_GROUP = 12

_MAX_CROSS_TOTALS = 12
_MAX_FOR_COMPLEMENTS = 8


def _cross_scalars(results: list[QueryResult]) -> list[tuple[str, float]]:
    """(label, value) for every measure-column TOTAL across all results, plus
    pairwise complements. The label names the derivation for telemetry."""
    totals: list[tuple[str, float]] = []
    for r_idx, result in enumerate(results):
        # A truncated result's column sum is the same wrong partial total
        # sql.py's note warns against — skip it as a SOURCE of totals/
        # complements entirely, rather than filtering its cells out below,
        # so it never enters `out` and can't seed a complement either.
        # `enumerate` stays over the FULL list (not just the survivors) so
        # the `q{r_idx+1}` labels keep naming their real result — renumbering
        # would make a recorded derivation point at the wrong result.
        if result.truncated:
            continue
        for name, values in measure_columns(result).items():
            dense = [v for v in values if v is not None]
            if dense:
                totals.append((f"q{r_idx + 1}.{name}", math.fsum(dense)))
            if len(totals) >= _MAX_CROSS_TOTALS:
                break
        if len(totals) >= _MAX_CROSS_TOTALS:
            break
    out = list(totals)
    if len(totals) <= _MAX_FOR_COMPLEMENTS:
        for i, (na, a) in enumerate(totals):
            for nb, b in totals[i + 1:]:
                if a != b:
                    out.append((f"{na}-{nb}", abs(a - b)))
    return out


def _match_cross_result(scalars, numerators, reproduces,
                        is_pct: bool) -> tuple[str, str] | None:
    """Reproduce a value from totals spanning several results.

    Tried LAST everywhere it appears: it is the widest search in the module, so
    it must never displace a verbatim cell or an in-result derivation.

    `is_pct` — whether the value was WRITTEN as a percentage — splits the two
    routes, and it is what makes this affordable. Unsplit, an answer with seven
    results offered 11 totals plus 55 pairwise complements, and every cell was
    tried against all 66 plus a share of each: fabricated cells went from 0.9% to
    10.4% grounded, exactly the coincidental-match blowup this module's KNOWN
    LIMITATION warns about. A share is written "25.3%" and a count is not, so the
    marker the model already writes cuts the search roughly in half and refuses
    the mismatched half outright.
    """
    if not is_pct:
        for name, v in scalars:                   # an absolute: a total/complement
            if reproduces(v):
                return "cross", name
        return None
    for n_name, n_v in numerators:                # a percentage: a share of a total
        for d_name, d_v in scalars:
            if not d_v:
                continue
            got = n_v / d_v * 100.0
            # A share of a total cannot exceed the total. Cheap, and it removes
            # the ratios that only ever match by accident.
            if 0.0 < got <= 100.0 and reproduces(got):
                return "cross_share", f"{n_name}/{d_name}"
    return None


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
            # Keyed per-RESULT, not per-turn (see the module docstring's
            # TRUNCATION paragraph) — sql.py/prompt.py both tell the model to
            # fix a cut ranking with a separate untruncated SELECT SUM(...), so
            # an untruncated result in the same turn must stay fully checkable.
            match = _match_in_column(target, raw_value, column, values,
                                     whole_column=not result.truncated)
            if match is None:
                continue
            status, op = match
            derivation = Derivation(op=op, result_index=r_idx, column=column)
            if status == EXACT:
                return EXACT, derivation
            # Keep looking for an exact match, but remember the weaker one.
            if best is None or (best[0] == DERIVED and status == ROUNDED):
                best = (status, derivation)
    if best is not None and best[0] != DERIVED:
        return best          # a verbatim/rounded cell already beats a derivation
    # Row-wise totals, last: strictly weaker evidence than a column route, and
    # only for the FIGURE. check_table passes allow_dimension=False and is
    # deliberately excluded — a table grades hundreds of cells, so widening its
    # match surface would inflate the Grounded-cells rate with coincidental hits,
    # while the figure is one value per turn where the false `ungrounded` was
    # actually observed.
    if allow_dimension:
        tol = _displayed_precision_tol(raw_value, target)
        for r_idx, result in enumerate(results):
            for row_i, total in _row_totals(result):
                if _close(target, total) or (tol and abs(target - total) <= tol):
                    return DERIVED, Derivation(op="row_total", result_index=r_idx,
                                               column=f"row{row_i + 1}")
    # A per-row RATIO of two measure columns is deliberately NOT offered here,
    # only in _match_at_row (route 2b). A table cell has an anchor row, so that
    # route tries k(k-1) pairs from ONE row; the figure has no anchor, so the
    # same idea has to try every row's pairs — rows x k(k-1), all landing in
    # (0,100], each against a display-rounding window. That is not a search, it
    # is a sieve. Measured against 400 FABRICATED hero percentages on one
    # synthetic result: 22.5% of them "verified" on a 200-row/2-measure result,
    # 89.2% when written without a decimal, and 97.0% at 200 rows x 6 measures
    # (the honest routes score 0.8-16.5% on the same inputs). It shipped in #318
    # and was caught in review before any release: `derived` is in figure.js's
    # VERIFIED_STATUSES, so it renders the reader-facing "✓ verified", and both
    # _maybe_retry_figure and _s5_fabricated act on the verdict.
    # A last-row-only bound measures clean (0-2.2%) but does not recover the
    # real cases — a headline share legitimately cites any row (observed: row 5
    # of 6). So the figure keeps NO route for this shape, and a correct per-row
    # share reads `ungrounded`, i.e. no mark at all. That is the documented
    # asymmetry: the mark is positive-only because a missing mark costs a little
    # trust while a false one destroys it. Re-opening this needs a bound that is
    # measured on FABRICATED figures, not only on correct ones.
    # Cross-result totals, absolutely last — the widest search here, so it may
    # only run once every in-result route has failed. Numerators are the totals
    # themselves: this reaches a summary line ("top 5 combined", "all others")
    # and a headline share, both of which are one result's total over another's.
    # A cell belonging to a specific ROW gets the richer numerator set in
    # _match_at_row, which knows which row it is.
    if best is None:
        hedge = parse_hedge(raw_value)
        tol = 0.0 if hedge else _displayed_precision_tol(raw_value, target)

        def reproduces(got: float | None) -> bool:
            if hedge:
                return satisfies_hedge(hedge, target, got)
            return got is not None and (_close(target, got) or abs(target - got) <= tol)

        scalars = _cross_scalars(results)
        hit = _match_cross_result(scalars, scalars, reproduces,
                                  "%" in _strip_emphasis(raw_value))
        if hit:
            return DERIVED, Derivation(op=hit[0], result_index=-1, column=hit[1])
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
    # How many FAILED cells were refused specifically BY the truncation gate —
    # i.e. a second reconciliation pass with the gate forced open would have
    # matched them. Deliberately NOT `cells_checked - cells_matched`, which
    # would just restate the existing counts: it is computed by actually
    # re-running the failed cells with the gate open (see check_table), so it
    # counts only a gate refusal, never an ordinary transcription miss or a
    # wholly fabricated number that fails either way. In-memory telemetry only
    # — no usage_log column, no migration; see llm._stamp_table_grounding for
    # where it surfaces (an INFO log line, not a persisted field).
    cells_blocked: int = 0


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


# --- Row anchoring -------------------------------------------------------------
# A table row DESCRIBES a result row. Matching it to that row is what turns
# grounding from "does this number appear anywhere in the column?" into "is this
# the number the query returned FOR THIS ENTITY?" — and the difference is not
# academic. Measured on the retained corpus before this change: fabricating every
# number in a real answer (scaling each by 1.2-1.9x) still left 41% of the cells
# "grounded", and on the widest turn 60% — 878 of those false grounds were plain
# EXACT hits on `total_degrees`, a column carrying 506 values across three
# results. At that density, "this value is somewhere in the column" is nearly
# free, and the ✓ mark it feeds was correspondingly weak.
#
# Anchoring also happens to be what makes a row-wise derived column reachable:
# a "% change" cell is (last - first) / first FOR ITS OWN ROW, so it can only be
# reproduced once you know which row that is. One mechanism, both problems.
#
# When a row CANNOT be anchored the old unrestricted search still runs, so a
# reshaped/pivoted/summary table (the conversation-scoped borrow case, a "Total"
# row) keeps grounding exactly as before.

# Markdown emphasis around a label ("**Ohio State**") is decoration, not identity.
_LABEL_DECOR_RE = re.compile(r"[*_`~]+")

# The column-scoped ops an ANCHORED row may still use: only the ones that can
# legitimately repeat on every entity row (a national total or average carried
# alongside). `max`/`min` and `pct_change`/`diff` are excluded — see _match_at_row.
_ANCHORED_COLUMN_OPS = ("sum", "mean")


def _norm_label(value) -> str:
    """A cell → a comparable label. Case/whitespace/emphasis-insensitive."""
    s = _LABEL_DECOR_RE.sub("", str(value if value is not None else ""))
    return re.sub(r"\s+", " ", s).strip().lower()


@dataclass(frozen=True)
class _Prepared:
    """Per-result lookups built ONCE, because check_table grades every cell of
    every row against every result and rebuilding these per cell is quadratic."""
    measures: dict[str, list[float | None]]    # row-aligned measure columns
    dense: dict[str, list[float]]              # the same, nulls dropped (for aggregates)
    dense_pos: dict[str, dict[int, int]]       # row index -> position in `dense`
    aggs: dict[str, tuple[float | None, ...]]  # per column: the row-INDEPENDENT aggregates
    series: list[list[float]]                  # per row: measure values, column order
    row_labels: list[set[str]]                 # per row: normalized text cells
    row_numbers: list[set[float]]              # per row: numeric cells, for O(1) lookup
    truncated: bool                            # was this result CUT — gates column-extent ops


def _prepare(result: QueryResult) -> _Prepared:
    measures = measure_columns(result)
    dense: dict[str, list[float]] = {}
    dense_pos: dict[str, dict[int, int]] = {}
    for name, values in measures.items():
        d, pos = [], {}
        for i, v in enumerate(values):
            if v is not None:
                pos[i] = len(d)
                d.append(v)
        dense[name] = d
        dense_pos[name] = pos
    # sum/mean don't depend on the row, so computing them per graded cell was
    # re-running fsum over a 200-value column hundreds of times.
    aggs = {name: tuple(compute(op, d) for op in _ANCHORED_COLUMN_OPS)
            for name, d in dense.items()}
    labels, numbers = [], []
    for row in (result.rows if result else []):
        labels.append({lbl for cell in row
                       if _as_number(cell) is None and (lbl := _norm_label(cell))})
        numbers.append({v for cell in row if (v := _as_number(cell)) is not None})
    return _Prepared(measures, dense, dense_pos, aggs, row_series(result),
                     labels, numbers, bool(result and result.truncated))


def _anchor_rows(table_row: list[str], prep: _Prepared) -> list[int]:
    """Which result rows does this table row describe? [] when nothing matches.

    Returns a GROUP — every row tied at the best admissible score — not a single
    row, because a PIVOTED table row legitimately describes several result rows
    at once. A long result of (year, modality, bachelors) rendered as one row per
    year with a column per modality means that row's three numbers live in three
    DIFFERENT result rows.

    Requiring a unique winner (the previous behaviour) got this exactly
    backwards, and the two halves compounded. Observed live on conversation 23:

      * result 5 — (year, modality, bachelors), the result that actually holds
        all 15 numbers — scored a three-way tie for "2021" and was REFUSED as
        ambiguous;
      * result 3 — a superseded two-way split the model had already replaced —
        matched exactly one row, so it anchored UNIQUELY and won;
      * and because SOMETHING anchored, check_table took the anchored path and
        never consulted result 5 at all.

    Each of the 5 year rows was then graded against a single result row holding
    one of its three numbers: 5 of 15 cells, a `partial` caution on a table whose
    every number was correct and present in the turn's own results.

    So ambiguity in the right result IS the pivot group. Grading against the
    group keeps the anti-false-positive property that matters — cells are still
    confined to result rows describing THIS entity, never "anywhere in the
    column" — while letting a pivot reproduce.

    Scored on (label matches, numeric matches). A label match outranks a numeric
    one because an entity name is identity while a number can collide, and a lone
    numeric match (0,1) is still refused: anchoring on one coincidental number
    would reject correct cells, turning a false positive into the far more
    damaging false negative.
    """
    labels = {lbl for cell in table_row
              if parse_number(cell) is None and (lbl := _norm_label(cell))}
    numbers = [v for cell in table_row if (v := parse_number(cell)) is not None]
    best_score, best_rows = (0, 0), []
    for i, row_labels in enumerate(prep.row_labels):
        t = sum(1 for lbl in labels if lbl in row_labels)
        # IDENTITY, not the display tolerance _close() applies. Anchoring on a
        # relative tolerance made adjacent years indistinguishable — 2023 is
        # within 0.1% of 2021/2022/2024/2025 — so every row of a by-year result
        # tied and the anchor was refused, dropping cells that had grounded
        # before. A transcribed value either IS the returned one or the row
        # falls back to the unrestricted search.
        # Set membership, which is that identity test in O(1): both sides reach
        # float() through the same decimal text, so a value that came from this
        # row compares equal. A hypothetical representation drift costs only the
        # anchor (the row falls back), never a wrong answer.
        n = sum(1 for x in numbers if x in prep.row_numbers[i])
        score = (t, n)
        if score > best_score:
            best_score, best_rows = score, [i]
        elif score == best_score and score > (0, 0):
            best_rows.append(i)
    t, n = best_score
    if not (t >= 1 or n >= 2):
        return []
    # A group that spans (nearly) the whole result is not an anchor — it is the
    # unrestricted column search wearing an anchor's clothes, which is exactly
    # the precision the row anchoring was introduced to buy back. Refuse it and
    # let the explicit fallback handle the row, so the behaviour stays honest
    # about which search actually ran.
    if len(best_rows) > _MAX_ANCHOR_GROUP:
        return []
    return best_rows


def _match_at_row(target: float, raw_value, prep: _Prepared, index: int,
                  scalars: list[tuple[str, float]] = ()) -> str | None:
    """Reproduce `target` from ONE anchored result row. Returns the op that did
    it (telemetry/debugging only — check_table needs just the boolean) or None.

    What this deliberately does NOT offer, versus the unrestricted search: a
    `value` or `share` at any OTHER row index. Those two are the entire
    false-ground mechanism measured above. Whole-column aggregates stay — there
    are only ~6 per column, and dropping them would cost recall on a row that
    legitimately carries one.
    """
    # A hedged cell ("<0.1%") states a BOUND; every route tests the inequality.
    hedge = parse_hedge(raw_value)
    tol = 0.0 if hedge else _displayed_precision_tol(raw_value, target)

    def reproduces(got: float | None) -> bool:
        if hedge:
            return satisfies_hedge(hedge, target, got)
        return got is not None and (_close(target, got) or abs(target - got) <= tol)

    # 1. This row's own cells — the overwhelmingly common case: the model
    #    transcribed the number the query returned for this entity.
    for values in prep.measures.values():
        v = values[index] if index < len(values) else None
        if v is None:
            continue
        if reproduces(v) if hedge else (_close(target, v) or (tol and abs(target - v) <= tol)):
            return "bound" if hedge else "value"
    # 2. Derived ACROSS this row — the blind spot. A "% change"/"Total"/"Change"
    #    column is computed from the row's own measures in column order.
    series = prep.series[index] if index < len(prep.series) else []
    if series:
        for op in ("sum", "pct_change", "diff", "mean"):
            if reproduces(compute(op, series)):
                return f"row_{op}"
        for i in range(len(series)):
            if reproduces(compute("share", series, index=i)):
                return "row_share"
    # 2b. This row's own cells, one OVER another. Neither route above expresses
    #     it: compute("share") is a cell's share of its own COLUMN's total, and
    #     row_share (just above) is its share of the ROW's sum — so a "Share"
    #     column that is, per row, `priv_np / total` had no route at all. Found
    #     live on a table whose every number was correct and whose share column
    #     graded 5-of-6 UNREPRODUCED, raising the reader-facing caution on
    #     faultless work.
    #
    #     Cheapest search in this function — k measure columns give k(k-1)
    #     ordered pairs from ONE row (2 here), against the cross-result route's
    #     totals-times-complements — and it runs BEFORE that route on purpose:
    #     the six-year aggregate share of that same table (25.98%) rounds to the
    #     same 26.0% as 2023's own row, so the wide route was "verifying" one
    #     cell through a ratio that has nothing to do with it. A cell whose own
    #     row explains it must ground on its own row.
    #
    #     Both guards are the ones _match_cross_result already proved necessary:
    #     the value must be WRITTEN as a percentage (unsplit, that marker was
    #     worth 0.9% -> 10.4% fabricated grounds there), and a share cannot
    #     exceed its whole, which is also what refuses the INVERTED pair.
    if "%" in _strip_emphasis(raw_value):
        pairs = [(name, values[index]) for name, values in prep.measures.items()
                 if index < len(values) and values[index] is not None]
        for n_name, n_v in pairs:
            for d_name, d_v in pairs:
                if d_name == n_name or not d_v:
                    continue
                got = n_v / d_v * 100.0
                if 0.0 < got <= 100.0 and reproduces(got):
                    return f"row_ratio({n_name}/{d_name})"
    # 3. Derived DOWN a column: only the ops that can legitimately REPEAT on an
    #    entity row — a national total or average carried beside each row — plus
    #    `share` pinned to THIS row (a share-of-column-total column is
    #    row-indexed, and pinning it is what keeps it from matching any row's
    #    share).
    #
    #    `max`/`min` are deliberately absent. The row that legitimately holds the
    #    column max grounds through route 1 (its own cell), so they add no recall
    #    — while for every OTHER row, matching the column max is precisely the
    #    mistranscription this check exists to catch (copying the top row's
    #    number down a column). Same for `pct_change`/`diff`, which describe a
    #    column's trend and are meaningless on one entity's row.
    #
    #    Column-extent (sum/mean/share-of-total) is gated on `prep.truncated` —
    #    those three read the column's TOTAL, which a cut result never truly
    #    has. `dense_pos` is hoisted OUTSIDE the guard because route 4 below
    #    (prev_diff/prev_pct_change) needs it too and is allowed either way —
    #    both its endpoints are held rows, unaffected by which rows were cut.
    for name, values in prep.dense.items():
        pos = prep.dense_pos[name].get(index)
        if not prep.truncated:
            aggs = prep.aggs[name]
            for op, got in zip(_ANCHORED_COLUMN_OPS, aggs, strict=True):
                if reproduces(got):
                    return op
            # share, from the already-summed total rather than compute(), which
            # would re-fsum the whole column once per graded cell.
            total = aggs[0]
            if pos is not None and total and reproduces(values[pos] / total * 100.0):
                return "share"
        # 4. Change against the PREVIOUS ROW — a "% vs prior year" / "Change"
        #    column, which is neither across the row (route 2) nor a whole-column
        #    aggregate. Found by probing the anchored kernel for what it still
        #    could not reproduce: a correct year-over-year column graded 3/6.
        #    Both ends are pinned to the anchored row, so it costs two candidates.
        if pos is not None and pos > 0:
            prev = values[pos - 1]
            if reproduces(values[pos] - prev):
                return "prev_diff"
            if prev and reproduces((values[pos] - prev) / abs(prev) * 100.0):
                return "prev_pct_change"
    # 5. LAST: this row's own value over a total from ANOTHER result — the
    #    canonical "share of the state/national total", where the denominator
    #    came from a separate SELECT SUM(...). The row's cells are the numerators
    #    that _reconcile_value's fallback cannot know about.
    if scalars:
        numerators = [(name, values[index]) for name, values in prep.measures.items()
                      if index < len(values) and values[index] is not None]
        hit = _match_cross_result(scalars, numerators + list(scalars), reproduces,
                                  "%" in _strip_emphasis(raw_value))
        if hit:
            return f"{hit[0]}({hit[1]})"
    return None


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
    Each row is first ANCHORED to the result row it describes (see _anchor_rows);
    an anchored cell is graded against that row alone, which is what makes a
    row-wise derived column reachable and what stops a value from grounding
    against an unrelated row of a long column. A row that can't be anchored falls
    back to the unrestricted search, so reshaped and summary tables are unaffected.

    NO_TABLE/UNCHECKED carry no counts so they don't move the rate.

    A truncated result refuses its column-extent routes (see the module
    docstring's TRUNCATION paragraph) but the cell is still COUNTED — a middle
    path of excluding blocked cells from `cells_checked`, so an all-blocked
    table went silent instead of raising the ⚠, was considered and rejected: a
    cell that matches ONLY the partial column sum is, with high probability,
    precisely the wrong total this fix exists to catch, so silencing it there
    would invert the feature. If the ⚠ turns out to fire often on genuinely
    correct truncated-turn answers in production, `cells_blocked` on the
    returned TableGroundingCheck is what makes that measurable: a failed cell
    counts there only when a second reconciliation pass with the truncation
    gate forced OPEN would have matched it — never merely
    `cells_checked - cells_matched`, which can't distinguish a gate refusal
    from an ordinary transcription miss or a wholly fabricated number.
    llm._stamp_table_grounding logs it at INFO when nonzero; that log line is
    the production visibility, not a new persisted field (see
    TableGroundingCheck.cells_blocked)."""
    # (value, raw, table_row) — the row is kept so the cell can be anchored.
    cells: list[tuple[float, str, list[str]]] = []
    for header, body in parse_markdown_tables(answer_markdown or ""):
        width = max((len(r) for r in body), default=0)
        gradable: list[int] = []
        for ci in range(width):
            col_head = header[ci] if ci < len(header) else ""
            nums = [v for r in body if ci < len(r)
                    for v in (parse_number(r[ci]),) if v is not None]
            if not nums:
                continue  # a label column (states, institutions) — nothing to grade
            if not _is_measure_column(col_head, nums):
                continue  # a rank ordinal or dimension — not a transcribed measure
            gradable.append(ci)
        for r in body:
            for ci in gradable:
                if ci < len(r) and (v := parse_number(r[ci])) is not None:
                    cells.append((v, r[ci], r))
    if not cells:
        return TableGroundingCheck(NO_TABLE)
    if not results:
        return TableGroundingCheck(UNCHECKED)

    preps = [_prepare(result) for result in results]
    scalars = _cross_scalars(results)
    # The gate-open second pass is built ONCE per call, and only when it can
    # possibly matter — guarded on whether ANY result was truncated at all, so
    # the overwhelming majority of turns (nothing truncated) pay nothing extra.
    # It re-derives everything the first pass derived (preps, cross scalars)
    # from the same results with every truncation flag forced off, so a failed
    # cell can be re-tried through the identical anchored/unrestricted routes.
    any_truncated = any(r.truncated for r in results)
    results_open = _ungate(results) if any_truncated else None
    preps_open = ([_prepare(result) for result in results_open]
                  if results_open is not None else None)
    scalars_open = _cross_scalars(results_open) if results_open is not None else None

    anchors: dict[int, list[list[int]]] = {}   # table row identity -> per-result groups
    matched = 0
    blocked = 0
    for v, raw, row in cells:
        key = id(row)
        if key not in anchors:
            anchors[key] = [_anchor_rows(row, prep) for prep in preps]
        row_anchors = anchors[key]
        # Anchored rows are searched ACROSS every result, so the result holding
        # this row's numbers is consulted even when another result also anchored.
        # That is the second half of the live failure: a superseded result
        # anchored uniquely and wrongly, and because SOMETHING anchored, the
        # result with the real numbers never got a look in. Grouping fixes it at
        # the source — that result is no longer refused as "ambiguous" — so this
        # stays a strict either/or and the unrestricted search is still reserved
        # for rows that anchor nowhere.
        anchored = any(row_anchors)
        if anchored:
            ok = any(_match_at_row(v, raw, prep, i, scalars) is not None
                     for prep, group in zip(preps, row_anchors, strict=True)
                     for i in group)
        else:
            ok = _reconcile_value(v, raw, results, allow_dimension=False) is not None
        if ok:
            matched += 1
            continue
        # Failed — was the truncation gate specifically what refused it? Reuse
        # the SAME anchor groups (anchoring itself doesn't depend on
        # `truncated`, only on labels/numbers already present in the rows) but
        # evaluate them against the gate-open preps/results/scalars.
        if preps_open is None:
            continue
        if anchored:
            would_match = any(
                _match_at_row(v, raw, prep, i, scalars_open) is not None
                for prep, group in zip(preps_open, row_anchors, strict=True)
                for i in group)
        else:
            would_match = _reconcile_value(
                v, raw, results_open, allow_dimension=False) is not None
        if would_match:
            blocked += 1
    checked = len(cells)
    if matched == checked:
        status = TABLE_MATCHED
    elif matched == 0:
        status = TABLE_UNMATCHED
    else:
        status = TABLE_PARTIAL
    return TableGroundingCheck(status, cells_checked=checked, cells_matched=matched,
                               cells_blocked=blocked)
