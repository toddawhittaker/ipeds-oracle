"""Figure grounding (backend/app/grounding.py).

The regression this guards: an answer's hero figure is the most prominent number
on the screen and, before this module, the least verified — app/llm.py's
_extract_figure checked only that the JSON had a value and a label, so a number
the model invented while re-typing a Markdown table reached the user with
nothing comparing it back to the rows SQLite returned.

Two failure directions matter, and they pull against each other:

  * a FALSE NEGATIVE (an invented number reported as fine) defeats the point;
  * a FALSE POSITIVE (a legitimately derived headline flagged as ungrounded) is
    what would make the measurement useless — prompt step 6(ii) explicitly tells
    the model to derive a % change / share / average / max, so if those read as
    ungrounded the recorded rate is noise and no policy can ever hang off it.

Both directions are asserted below. parse_number is pinned against the formats
the prompt actually asks for ("42,318", "+12.4%"), since a parse failure would
silently turn every figure into no_figure and quietly zero out the metric.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import grounding  # noqa: E402
from app.tools.sql import QueryResult  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def result(columns, rows, truncated=False):
    return QueryResult(columns=list(columns), rows=[tuple(r) for r in rows],
                       truncated=truncated, row_count=len(rows))


# --- Row-wise ratio of two measure columns (the chat-51 blind spot) -----------
# Found live. An answer tabulated "Total RN awards", "Private non-profit awards"
# and a "Share" column that is, per row, priv / total * 100. Every number was
# correct and every Share cell graded UNREPRODUCED -- 5 of 6 -- because no route
# expressed "this row's column B over this row's column A": compute("share") is
# a cell's share of its own COLUMN's total, and row_share is its share of the
# ROW's sum. The one cell that did "match" matched by coincidence, through the
# whole-column cross_share, which is the more alarming half.

_RATIO = result(["year", "total_rn", "priv_np_rn"],
                [(2020, 251160, 67536), (2021, 260564, 71169),
                 (2022, 268486, 71373), (2023, 264511, 68790),
                 (2024, 254773, 65027), (2025, 258184, 60836)])

_RATIO_MD = """| Year | Total RN awards | Private non-profit awards | Share |
| --- | ---: | ---: | ---: |
| 2020 | 251,160 | 67,536 | 26.9% |
| 2021 | 260,564 | 71,169 | 27.3% |
| 2022 | 268,486 | 71,373 | 26.6% |
| 2023 | 264,511 | 68,790 | 26.0% |
| 2024 | 254,773 | 65,027 | 25.5% |
| 2025 | 258,184 | 60,836 | 23.6% |
"""


def test_a_per_row_ratio_column_grounds_every_cell():
    """THE REGRESSION. All 18 cells are correct and reproducible; before the
    row-ratio route only 13 did, so a correct answer raised the reader-facing
    caution."""
    got = grounding.check_table(_RATIO_MD, [_RATIO])
    assert got.cells_checked == 18, got
    assert got.cells_matched == 18, f"expected every cell to ground, got {got}"
    assert got.status == grounding.TABLE_MATCHED, got


def test_a_per_row_ratio_figure_does_NOT_ground():
    """The documented limitation, and the reason it is one.

    A table cell has an anchor row, so _match_at_row tries k(k-1) pairs from
    that ONE row. The figure has no anchor, so the same idea must try every
    row's pairs -- rows x k(k-1) ratios, all in (0,100], each against a
    display-rounding window. #318 shipped exactly that and it verified 22-97%
    of FABRICATED hero percentages (see the next test). The figure therefore
    keeps no route for this shape: a correct per-row share reads `ungrounded`
    and renders NO mark, which is the safe direction for a positive-only mark.

    Delete this test the day a MEASURED bound exists -- not before."""
    check = grounding.check_figure(
        {"value": "23.6%", "unit": "%", "label": "Share of RN degrees"}, [_RATIO])
    assert check.status == grounding.UNGROUNDED, check


def test_a_fabricated_figure_percentage_does_not_ground_against_a_wide_result():
    """THE TEST THAT WAS MISSING, and the reason #318's regression shipped.

    All three of that PR's counterweights ran through check_table
    (allow_dimension=False) or _match_at_row -- neither of which reaches the
    figure path -- so deleting BOTH guards from the figure block left every
    test green. This one drives check_figure against the shape the sieve fed
    on: many rows, several measure columns, a percentage that is in no way
    derivable from the data.

    A handful of coincidental hits is expected and fine; a majority is the
    failure. The bound is deliberately loose so it pins the CLASS rather than
    a tolerance."""
    import random
    rows = 200
    rnd = random.Random(4)
    wide = result(["year", "a", "b", "c", "d", "e", "f"],
                  [tuple([2000 + i] + [rnd.randint(50, 90000) for _ in range(6)])
                   for i in range(rows)])
    rnd = random.Random(99)
    n = 200
    grounded = 0
    for _ in range(n):
        v = rnd.uniform(0.5, 99.5)
        fig = {"value": f"{v:.1f}%", "unit": "%", "label": "fabricated hero stat"}
        if grounding.check_figure(fig, [wide]).status != grounding.UNGROUNDED:
            grounded += 1
    rate = grounded / n
    # The bound is 0.50, not something tight, and the looseness is MEASURED
    # rather than cautious. On this exact shape the surviving honest routes --
    # mostly _match_cross_result's totals-and-complements, which is documented
    # as the widest search in the module -- already ground 13-20% of fabricated
    # percentages depending only on the data seed (0..5 measured:
    # 18.0 / 15.0 / 13.5 / 20.0 / 16.5 / 19.5). A `< 0.20` bound therefore sat
    # INSIDE the existing noise and went red on seed 3 for reasons unrelated to
    # any code change.
    #
    # So this pins the CLASS and says so: an unbounded per-row ratio sieve
    # scored 95.5% here, and nothing incremental can hide under 50% without
    # first tripping the corpus recall/precision measurement in
    # test_fabricated_numbers_are_rejected_at_scale. Tightening this number is
    # only meaningful after the cross-result baseline itself is bounded.
    assert rate < 0.50, (
        f"{rate:.1%} of fabricated hero percentages verified against a "
        f"{rows}-row x 6-measure result -- the figure path has an unbounded "
        f"ratio sieve again (#318 measured 95.5% here; the honest-route "
        f"baseline on this shape is 13-20%)")


def test_the_row_ratio_beats_the_coincidental_column_share():
    """PRECISION, and the reason the row-local route must be tried BEFORE the
    cross-result one. The six-year aggregate share is 404,731/1,557,678 =
    25.98%, which rounds to the same 26.0% as 2023's own share -- so 2023 used
    to "verify" against a whole-column ratio that has nothing to do with its
    row. A cell must ground through its OWN row when its own row explains it."""
    prep = grounding._prepare(_RATIO)
    scalars = grounding._cross_scalars([_RATIO])
    op = grounding._match_at_row(26.0, "26.0%", prep, 3, scalars)
    assert op is not None
    assert "cross" not in op, f"2023 grounded coincidentally as {op}"


def test_a_wrong_share_still_fails():
    """The anti-vacuity half: the new route must not ground an arbitrary
    percentage. 30.0% is not any pair ratio in any row of this result."""
    md = _RATIO_MD.replace("| 23.6% |", "| 30.0% |")
    got = grounding.check_table(md, [_RATIO])
    assert got.cells_matched == got.cells_checked - 1, got
    assert got.status == grounding.TABLE_PARTIAL, got


def test_a_ratio_not_written_as_a_percent_does_not_ground():
    """The `%` marker splits the two routes here exactly as it does for
    cross-result shares, where dropping it took fabricated grounds from 0.9% to
    10.4%. A bare count that happens to equal a ratio is not a share."""
    md = ("| Year | Total RN awards | Private non-profit awards | Count |\n"
          "| --- | ---: | ---: | ---: |\n"
          "| 2020 | 251,160 | 67,536 | 26.9 |\n")
    got = grounding.check_table(md, [_RATIO])
    assert got.cells_matched == 2, f"the bare 26.9 must not ground: {got}"


def test_an_out_of_range_ratio_does_not_ground():
    """A share cannot exceed its whole. total/priv is 371.9% for 2020; the
    (0,100] guard is what stops the INVERTED pair matching."""
    prep = grounding._prepare(_RATIO)
    assert grounding._match_at_row(371.9, "371.9%", prep, 0, ()) is None



# --- QueryResult.to_storage / from_storage (cross-turn grounding persistence) --

def test_storage_round_trip_preserves_columns_and_cells():
    """A turn's result is persisted (messages.results) so a LATER turn can ground
    against it. The round-trip must preserve exactly what grounding reads —
    columns + cell values, including NULLs — or a cross-turn figure would ground
    against corrupted numbers."""
    r = result(["year", "awards"], [(2021, 550), (2022, None), (2023, 729)])
    back = QueryResult.from_storage(r.to_storage())
    assert back.columns == ["year", "awards"], back.columns
    assert back.rows == [(2021, 550), (2022, None), (2023, 729)], back.rows
    # ...and grounding sees the same numeric column it would have live.
    assert grounding.numeric_columns(back)["awards"] == [550.0, 729.0]


def test_to_storage_caps_rows():
    r = result(["n"], [(i,) for i in range(500)])
    assert len(r.to_storage(max_rows=200)["rows"]) == 200


def test_from_storage_tolerates_a_malformed_blob():
    # Reads persisted data — a missing/partial blob must degrade to empty, never
    # raise, or one bad row would break a live follow-up's grounding.
    assert QueryResult.from_storage({}).columns == []
    assert QueryResult.from_storage({"columns": ["a"]}).rows == []
    assert QueryResult.from_storage(None).rows == []


# A recent-years completions strip — the exact shape prompt step 6(i)(b) asks for.
YEARS = result(["year", "awards"],
               [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)])
# A ranking table — the shape step 6(ii) derives a leader/share headline from.
RANKING = result(["institution", "awards"],
                 [("Ohio State", 400), ("Texas A&M", 300), ("Arizona State", 300)])


# --- parse_number --------------------------------------------------------------

def test_parse_number_handles_the_formats_the_prompt_asks_for():
    cases = [
        ("42,318", 42318.0),      # thousands separators (step 6 asks for these)
        ("+12.4%", 12.4),         # a derived percentage change
        ("-3.5%", -3.5),
        ("$1,234.50", 1234.5),
        ("1250", 1250.0),
        ("~42,000", 42000.0),     # the hedge that sometimes rides along
        (1250, 1250.0),           # already numeric
        (12.5, 12.5),
        # Magnitude suffixes: not the format the prompt asks for, but models
        # write them anyway. Unparsed, they'd be filed as `no_figure` and
        # silently DROPPED from the measurement instead of checked.
        ("1.2M", 1_200_000.0),
        ("2.5 million", 2_500_000.0),
        ("850K", 850_000.0),
        ("1.1B", 1_100_000_000.0),
    ]
    for raw, want in cases:
        got = grounding.parse_number(raw)
        assert got == want, f"parse_number({raw!r}) -> {got!r}, want {want!r}"


def test_parse_number_rejects_non_numbers():
    for raw in [None, "", "   ", "Ohio State University", "n/a", "--", True, False]:
        assert grounding.parse_number(raw) is None, f"{raw!r} should not parse"


# --- numeric_columns -----------------------------------------------------------

def test_numeric_columns_keeps_row_order_and_skips_label_columns():
    cols = grounding.numeric_columns(RANKING)
    assert "institution" not in cols, "a text label column is not a numeric series"
    assert cols["awards"] == [400.0, 300.0, 300.0], cols


def test_numeric_columns_rejects_a_mixed_column():
    # A column that is numeric for some rows and text for others is NOT a series
    # -- treating it as one would invent derivations from a footnote row.
    mixed = result(["year", "n"], [(2023, 5), ("provisional", 6)])
    assert "year" not in grounding.numeric_columns(mixed)


def test_numeric_columns_skips_nulls_without_dropping_the_column():
    r = result(["n"], [(5,), (None,), (7,)])
    assert grounding.numeric_columns(r)["n"] == [5.0, 7.0]


# --- compute -------------------------------------------------------------------

def test_compute_matches_the_prompts_derivation_menu():
    v = [1000.0, 1100.0, 1200.0, 1250.0]
    assert grounding.compute("sum", v) == 4550.0
    assert grounding.compute("mean", v) == 1137.5
    assert grounding.compute("max", v) == 1250.0
    assert grounding.compute("min", v) == 1000.0
    assert abs(grounding.compute("pct_change", v) - 25.0) < 1e-9
    assert grounding.compute("value", v, index=2) == 1200.0
    assert abs(grounding.compute("share", [400.0, 300.0, 300.0], index=0) - 40.0) < 1e-9


def test_a_derived_absolute_change_is_not_flagged():
    """THE false-positive regression, taken verbatim from production.

    The model led a trend with "217 — Net increase since 2021" off its own
    550→767 table. 767-550=217 is exactly right, but step 6(ii) asks for the net
    "% CHANGE" and the vocabulary had only `pct_change` — so the kernel could
    not reproduce the ABSOLUTE form the model actually chose and recorded
    `ungrounded`. A kernel that cannot reproduce a correct number manufactures
    evidence of model error, which is the worst way for this metric to be
    wrong."""
    ohio_cs = result(["year", "cs_bachelors"],
                     [(2021, 550), (2022, 580), (2023, 729), (2024, 841), (2025, 767)])
    got = grounding.check_figure(
        {"value": "217", "label": "Net increase since 2021"}, [ohio_cs])
    assert got.status == grounding.DERIVED, got
    assert got.derivation.op == "diff", got.derivation
    assert got.derivation.column == "cs_bachelors", got.derivation
    # The percentage form of the SAME change must still ground (217/550 ≈ 39%).
    pct = grounding.check_figure({"value": "+39%", "label": "Growth since 2021"},
                                 [ohio_cs])
    assert pct.status == grounding.DERIVED, pct
    assert pct.derivation.op == "pct_change", pct.derivation


def test_diff_needs_two_points_but_tolerates_a_zero_baseline():
    # No non-zero guard, unlike pct_change: from 0 a ratio is undefined but an
    # absolute change is not.
    assert grounding.compute("diff", [550.0, 767.0]) == 217.0
    assert grounding.compute("diff", [5.0]) is None, "needs >=2 points"
    assert grounding.compute("diff", [0.0, 42.0]) == 42.0
    assert grounding.compute("pct_change", [0.0, 42.0]) is None, "ratio undefined"


def test_diff_stays_barred_on_a_dimension_column():
    # Widening the op vocabulary must not widen the collision surface: diff over
    # a year column (2025-2021=4) is meaningless and must not match a real 4.
    years_only = result(["year"], [(2021,), (2022,), (2023,), (2024,), (2025,)])
    got = grounding.check_figure({"value": "4", "label": "Years covered"}, [years_only])
    assert got.status == grounding.UNGROUNDED, got


def test_compute_degrades_instead_of_raising():
    # A bad op or unsupportable data must return None, never raise -- a wrong
    # provenance from the model would otherwise break the whole turn.
    assert grounding.compute("nonsense", [1.0, 2.0]) is None
    assert grounding.compute("sum", []) is None
    assert grounding.compute("pct_change", [5.0]) is None, "needs >=2 points"
    assert grounding.compute("pct_change", [0.0, 5.0]) is None, "zero baseline"
    assert grounding.compute("share", [0.0, 0.0]) is None, "zero total"
    assert grounding.compute("value", [1.0], index=9) is None, "index out of range"


# --- check_figure: the false-NEGATIVE direction --------------------------------

def test_an_invented_number_is_ungrounded():
    """THE regression: a headline that appears nowhere in the data and follows
    from no derivation of it must not pass as grounded."""
    fig = {"value": "87,654", "label": "Awards in 2024"}
    got = grounding.check_figure(fig, [YEARS])
    assert got.status == grounding.UNGROUNDED, got
    assert got.grounded is False


def test_a_plausible_but_wrong_total_is_ungrounded():
    # 5,000 is close to the real 5,550 total and would read as plausible to a
    # magnitude-based reviewer -- but it is not the sum, and nothing derives it.
    got = grounding.check_figure({"value": "5,000", "label": "Total"}, [YEARS])
    assert got.status == grounding.UNGROUNDED, got


# --- check_figure: the false-POSITIVE direction --------------------------------

def test_a_verbatim_cell_is_exact():
    got = grounding.check_figure({"value": "1,250", "label": "Awards"}, [YEARS])
    assert got.status == grounding.EXACT, got
    assert got.derivation.column == "awards", got.derivation


def test_a_derived_pct_change_is_not_flagged():
    """Step 6(ii) tells the model to lead a trend with a net % change. If that
    read as ungrounded the metric would be pure noise."""
    got = grounding.check_figure({"value": "+25.0%", "label": "Change since 2021"},
                                 [YEARS])
    assert got.status == grounding.DERIVED, got
    assert got.derivation.op == "pct_change", got.derivation


def test_a_derived_share_is_not_flagged():
    got = grounding.check_figure({"value": "40%", "label": "Ohio State's share"},
                                 [RANKING])
    assert got.status == grounding.DERIVED, got
    assert got.derivation.op == "share", got.derivation


def test_a_column_sum_is_not_flagged():
    got = grounding.check_figure({"value": "4,550", "label": "Total awards"}, [YEARS])
    assert got.status == grounding.DERIVED, got
    assert got.derivation.op == "sum", got.derivation


def test_a_dimension_column_is_never_aggregated():
    """A REAL collision this caught: the +25.0% awards trend above verified as
    share(year) — 2021/(2021+2022+2023+2024) = 24.98%, inside tolerance. Summing
    or sharing a `year` column is meaningless, and `year` is in nearly every
    IPEDS result, so leaving it aggregatable made a coincidence likelier than
    the truth. Here the only way to reach 24.98% is via that bogus share."""
    years_only = result(["year"], [(2021,), (2022,), (2023,), (2024,)])
    got = grounding.check_figure({"value": "24.98%", "label": "Share"}, [years_only])
    assert got.status == grounding.UNGROUNDED, got
    # ...and the same guard must not block a legitimate measure column.
    assert grounding.is_dimension("year") is True
    assert grounding.is_dimension("unitid") is True
    assert grounding.is_dimension("awards") is False
    assert grounding.is_dimension("ctotalt") is False


def test_display_rounding_is_not_flagged():
    """A model told to write a readable headline rounds 1,250 to "1,300"; honest
    display rounding must not read as an invented number."""
    exact = result(["n"], [(1247,)])
    got = grounding.check_figure({"value": "1,200", "label": "Awards"}, [exact])
    assert got.status == grounding.ROUNDED, got


def test_rounding_tolerance_does_not_swallow_a_real_mismatch():
    # The precision-derived tolerance must stay tied to the digits WRITTEN: a
    # value written to the hundreds tolerates +/-50, not +/-500.
    exact = result(["n"], [(1900,)])
    got = grounding.check_figure({"value": "1,200", "label": "Awards"}, [exact])
    assert got.status == grounding.UNGROUNDED, got


def test_trailing_zeros_alone_cannot_license_a_huge_rounding_window():
    """Trailing zeros are an unreliable precision signal: "1,000" has three,
    which on digit-count alone would license a +/-500 window and let the figure
    verify against a true 1,400. Rounding is capped in RELATIVE terms."""
    got = grounding.check_figure({"value": "1,000", "label": "Awards"},
                                 [result(["n"], [(1400,)])])
    assert got.status == grounding.UNGROUNDED, got
    # ...while an honest headline rounding of the same shape still passes.
    got = grounding.check_figure({"value": "1,000", "label": "Awards"},
                                 [result(["n"], [(1012,)])])
    assert got.status == grounding.ROUNDED, got


def test_a_small_percentage_keeps_the_precision_it_was_WRITTEN_to():
    """A figure under 1.0 must still get the rounding window its own digits
    declare. Found live: a correct "+0.4%" net change read `ungrounded`.

    `_MAX_ROUNDING_SHARE` caps the window at 5% of the value. For a number
    written to k decimals that cap binds whenever |target| < 10^-k — so every
    sub-1% figure at one decimal place was given +/-0.02 when its own notation
    declares +/-0.05. The true net change here is 0.3535%, which rounds to 0.4
    at one decimal place; the cap refused it and the reader lost the ✓ on a
    correct number.
    """
    years = result(["year", "awards"],
                   [(2020, 146826), (2021, 152183), (2022, 157536),
                    (2023, 155020), (2024, 146679), (2025, 147345)])
    got = grounding.check_figure(
        {"value": "+0.4%", "label": "RN bachelor's, 2020-2025"}, [years])
    assert got.status == grounding.DERIVED, got
    assert got.derivation.op == "pct_change", got


def test_a_small_percentage_outside_its_own_rounding_window_still_fails():
    """The paired negative — the window is the WRITTEN precision, not a licence.
    A true 0.9% cannot be reported as "+0.4%": 0.9 is outside 0.4 +/- 0.05.
    Without this, the fix above would be indistinguishable from removing the
    tolerance check for small numbers altogether.
    """
    years = result(["year", "awards"], [(2020, 100000), (2025, 100900)])
    got = grounding.check_figure(
        {"value": "+0.4%", "label": "net change"}, [years])
    assert got.status == grounding.UNGROUNDED, got


def test_a_table_cell_under_one_percent_also_keeps_its_written_precision():
    """The same defect on the table path, where it raises the reader-facing ⚠.
    Live case: a year-over-year "+0.5%" column cell against a true 0.4541%
    graded the table `partial`, i.e. "Check 1 of 12 values" on a correct table.
    """
    base = result(["year", "awards"],
                  [(2024, 146679), (2025, 147345)])
    answer = ("| Year | Degrees | YoY % change |\n"
              "| --- | ---: | ---: |\n"
              "| 2024 | 146,679 | — |\n"
              "| 2025 | 147,345 | +0.5% |\n")
    got = grounding.check_table(answer, [base])
    assert got.status == grounding.TABLE_MATCHED, got
    assert got.cells_matched == got.cells_checked, got


def test_a_magnitude_suffix_figure_is_measured_not_dropped():
    got = grounding.check_figure({"value": "1.2M", "label": "Bachelor's degrees"},
                                 [result(["awards"], [(1_200_000,)])])
    assert got.status == grounding.EXACT, got
    # ...and an invented one in the same notation is still caught.
    got = grounding.check_figure({"value": "9.9M", "label": "Bachelor's degrees"},
                                 [result(["awards"], [(1_200_000,)])])
    assert got.status == grounding.UNGROUNDED, got


# --- check_figure: the non-events ----------------------------------------------

def test_no_figure_and_non_numeric_headline_are_not_measured():
    # A figure whose headline is a NAME ("Ohio State") is legitimate -- there is
    # simply no arithmetic to check, and counting it would bias the rate.
    assert grounding.check_figure(None, [YEARS]).status == grounding.NO_FIGURE
    assert grounding.check_figure({}, [YEARS]).status == grounding.NO_FIGURE
    got = grounding.check_figure({"value": "Ohio State", "label": "Leader"}, [YEARS])
    assert got.status == grounding.NO_FIGURE, got


def test_a_figure_with_no_results_is_unchecked_not_ungrounded():
    """No retained results is an absence of evidence, not evidence of a bad
    number -- calling it ungrounded would poison the measured rate."""
    got = grounding.check_figure({"value": "1,250", "label": "Awards"}, [])
    assert got.status == grounding.UNCHECKED, got
    assert got.grounded is False


def test_it_searches_every_retained_result_not_just_the_last():
    """The reason results are retained at all: a brief runs several queries and
    the headline commonly comes from an EARLIER one."""
    got = grounding.check_figure({"value": "400", "label": "Leader"},
                                 [RANKING, YEARS])
    assert got.status == grounding.EXACT, got
    assert got.derivation.result_index == 0, got.derivation
    assert got.derivation.describe() == "value(q1.awards)", got.derivation.describe()


def test_check_figure_never_raises_on_junk():
    for fig in [{"value": []}, {"value": {}}, "not a dict", 42]:
        grounding.check_figure(fig, [YEARS])
    grounding.check_figure({"value": "1"}, [result([], [])])


# --- table grounding: the GFM parser ------------------------------------------
# Regression: the parser must find real result tables while never mistaking a
# ```chart JSON block (still present in the shipped answer) for one, or a bare
# `---` horizontal rule for a table separator.

_TABLE_MD = (
    "Here are the recent years.\n\n"
    "| Year | Awards |\n"
    "| --- | --- |\n"
    "| 2021 | 1,000 |\n"
    "| 2024 | 1,250 |\n\n"
    "Trend below.\n"
)


def test_parse_markdown_tables_extracts_header_and_body_rows():
    tables = grounding.parse_markdown_tables(_TABLE_MD)
    assert len(tables) == 1, tables
    # Separator dropped; header kept (for measure/dimension classification), two
    # body rows of two cells each.
    header, body = tables[0]
    assert header == ["Year", "Awards"], header
    assert body == [["2021", "1,000"], ["2024", "1,250"]], body


def test_parse_markdown_tables_skips_a_chart_fence():
    md = ("| Year | Awards |\n| --- | --- |\n| 2024 | 1,250 |\n\n"
          "```chart\n"
          '{"type":"line","data":[{"x":2024,"y":1250}]}\n'
          "```\n")
    tables = grounding.parse_markdown_tables(md)
    # Exactly the one real table — the chart JSON (no pipes, and fenced) is not it.
    assert tables == [(["Year", "Awards"], [["2024", "1,250"]])], tables


def test_parse_markdown_tables_ignores_a_bare_horizontal_rule():
    # A `---` under a pipe line is an HR, not a separator (no pipe in the rule).
    md = "Some prose with a | pipe in it\n\n---\n\nMore prose.\n"
    assert grounding.parse_markdown_tables(md) == []


# --- table grounding: check_table ---------------------------------------------

def test_a_verbatim_table_is_matched():
    # Only the MEASURE column (Awards: 1000, 1250) is graded — Year is a dimension
    # and is excluded, so 2 cells checked, both verbatim in YEARS.
    got = grounding.check_table(_TABLE_MD, [YEARS])
    assert got.status == grounding.TABLE_MATCHED, got
    assert got.cells_checked == 2 and got.cells_matched == 2, got


def test_a_dropped_digit_cell_is_partial():
    # "1,250" mistyped as "1,240" reproduces from nothing → one Awards cell
    # unmatched (Year excluded from grading).
    bad = _TABLE_MD.replace("1,250", "1,240")
    got = grounding.check_table(bad, [YEARS])
    assert got.status == grounding.TABLE_PARTIAL, got
    assert got.cells_checked == 2 and got.cells_matched == 1, got


def test_a_rank_ordinal_column_is_excluded_from_grading():
    # The live-test regression: a model-added Rank column (1,2,3) is never in the
    # DB result, so grading it would drag a perfectly-transcribed ranking table to
    # ~partial. Measure-only grading counts the award column ONLY.
    md = ("| Rank | State | Awards |\n"
          "| --- | --- | --- |\n"
          "| 1 | A | 1,250 |\n"
          "| 2 | B | 1,200 |\n"
          "| 3 | C | 1,100 |\n")
    got = grounding.check_table(md, [YEARS])
    # 3 Awards cells graded (all in YEARS), the 3 rank cells + 3 labels excluded.
    assert got.status == grounding.TABLE_MATCHED, got
    assert got.cells_checked == 3 and got.cells_matched == 3, got


def test_an_unlabeled_rank_ordinal_is_excluded_by_its_1_to_n_values():
    # A rank column headed "#" (which is_dimension doesn't name) is still caught
    # because its values are a pure 1..N sequence.
    md = ("| # | Awards |\n"
          "| --- | --- |\n"
          "| 1 | 1,250 |\n"
          "| 2 | 1,200 |\n"
          "| 3 | 1,100 |\n")
    got = grounding.check_table(md, [YEARS])
    assert got.cells_checked == 3 and got.cells_matched == 3, got


def test_a_wholly_invented_table_is_unmatched():
    md = "| A | B |\n| --- | --- |\n| 88888 | 77777 |\n"
    got = grounding.check_table(md, [YEARS])
    assert got.status == grounding.TABLE_UNMATCHED, got
    assert got.cells_checked == 2 and got.cells_matched == 0, got


def test_a_display_rounded_cell_still_matches():
    # 1,250 written as "1,300" is honest hundreds-place rounding (0.04 share) —
    # the same tolerance the figure grants (see _displayed_precision_tol).
    md = "| Year | Awards |\n| --- | --- |\n| 2024 | 1,300 |\n"
    got = grounding.check_table(md, [YEARS])
    assert got.status == grounding.TABLE_MATCHED, got


def test_a_legitimately_computed_column_grounds_not_false_alarms():
    # DECIDED (full reproduction): a share column reproduces via the `share` op, so
    # a computed column grounds instead of reading as a transcription error.
    # Ohio State 400 of 1000 total = 40.0%.
    md = ("| Institution | Awards | Share |\n"
          "| --- | --- | --- |\n"
          "| Ohio State | 400 | 40.0% |\n"
          "| Texas A&M | 300 | 30.0% |\n"
          "| Arizona State | 300 | 30.0% |\n")
    got = grounding.check_table(md, [RANKING])
    assert got.status == grounding.TABLE_MATCHED, got


def test_a_measure_cell_matching_only_a_dimension_column_is_unmatched():
    # ANOMALY 1 (observed live: counts "3"/"8" grounded against `awlevel`): a table
    # MEASURE cell must be verified by a MEASURE column, never by a code/dimension
    # column it merely collides with. Here 7 appears only in the awlevel DIMENSION
    # column (not in the `awards` measure), so the cell must read unmatched — NOT
    # grounded against the code. Without the fix _reconcile_value matched awlevel.
    awlevel_only = result(["awlevel", "awards"], [(3, 1250), (7, 1100)])
    md = "| Level | Count |\n| --- | --- |\n| Doctoral | 7 |\n"
    got = grounding.check_table(md, [awlevel_only])
    assert got.status == grounding.TABLE_UNMATCHED, got
    assert got.cells_checked == 1 and got.cells_matched == 0, got


def test_a_real_small_count_still_grounds_via_the_measure_column():
    # The fix must lose no legitimate match: a small count that IS a real measure
    # still grounds — via the MEASURE column, not the dimension it also equals.
    # 3 appears in BOTH awlevel (dimension) and grads (measure); measure-only
    # reconciliation must pick grads, so the cell grounds for the right reason.
    both = result(["awlevel", "grads"], [(3, 3), (5, 12)])
    md = "| Level | Grads |\n| --- | --- |\n| Doctoral | 3 |\n"
    got = grounding.check_table(md, [both])
    assert got.status == grounding.TABLE_MATCHED, got
    # Direct kernel assertion: measure-only reconciliation lands on `grads`.
    match = grounding._reconcile_value(3.0, "3", [both], allow_dimension=False)
    assert match is not None and match[1].column == "grads", match


def test_a_figure_that_is_a_dimension_value_still_grounds_exact():
    # REGRESSION GUARD for the figure path: `allow_dimension` defaults True, so a
    # hero figure that legitimately IS a year/code still grounds exact against the
    # dimension column (unchanged by the table-path fix). Pairs with
    # test_diff_stays_barred_on_a_dimension_column (which guards the aggregation
    # bar); this guards the intended EXACT-on-dimension for figures.
    years_only = result(["year"], [(2021,), (2022,), (2023,), (2024,), (2025,)])
    got = grounding.check_figure({"value": "2024", "label": "Latest year covered"},
                                 [years_only])
    assert got.status == grounding.EXACT, got


def test_prose_with_no_table_is_no_table():
    got = grounding.check_table("Ohio State University is in Columbus, OH.", [YEARS])
    assert got.status == grounding.NO_TABLE, got
    assert got.cells_checked == 0 and got.cells_matched == 0, got


def test_a_table_with_no_results_is_unchecked_with_zero_counts():
    # A recited table with no query this turn: UNCHECKED, and NO counts — so it
    # self-excludes from the SUM-based rate rather than reading as 0/N failures.
    got = grounding.check_table(_TABLE_MD, [])
    assert got.status == grounding.UNCHECKED, got
    assert got.cells_checked == 0 and got.cells_matched == 0, got


# --- Row-anchored table grounding ---------------------------------------------
# A table row DESCRIBES a result row, and grading it against THAT row rather than
# against every value in the column is what fixes two opposite defects at once.
#
# 1. FALSE NEGATIVES (the old KNOWN BLIND SPOT). Every op used to run DOWN a
#    column, while a "% change" column is computed ACROSS a row —
#    (2024 - 2021) / 2021 for THAT row. A table whose every number was correct
#    graded `partial`, or `unmatched` when the derived column was the only
#    measure. That measurement is why the shipped table mark is positive-only.
# 2. FALSE POSITIVES. Measured on the retained corpus: scaling every number in
#    eight real answers by 1.2-1.9x still left 24.0% of the cells "grounded"
#    (2142/8920), and 34% on the widest turn — 878 of those were plain EXACT hits
#    on a `total_degrees` column holding 506 values across three results. At that
#    density "somewhere in the column" is nearly free. After anchoring: 0.63%
#    (56/8920), with real cells unchanged at 446/446.
#
# The tests below hold BOTH directions, because a fix that only chased one would
# have been easy and wrong.
_ROWWISE = QueryResult(
    columns=["stabbr", "enroll_2021", "enroll_2024"],
    rows=[("CA", 100000, 110000), ("TX", 200000, 190000),
          ("NY", 50000, 52500), ("FL", 80000, 88000)],
    row_count=4)


def test_a_correct_row_wise_pct_change_column_grounds():
    """WAS the blind spot: 8/12, because nothing could compute across a row.
    Each % is (2024 - 2021)/2021 for its OWN row, reachable only once the row is
    anchored."""
    md = ("| State | 2021 | 2024 | % change |\n| --- | --- | --- | --- |\n"
          "| CA | 100,000 | 110,000 | +10.0% |\n"
          "| TX | 200,000 | 190,000 | -5.0% |\n"
          "| NY | 50,000 | 52,500 | +5.0% |\n"
          "| FL | 80,000 | 88,000 | +10.0% |\n")
    got = grounding.check_table(md, [_ROWWISE])
    assert got.status == grounding.TABLE_MATCHED, got
    assert (got.cells_matched, got.cells_checked) == (12, 12), got


def test_a_correct_pct_change_column_ALONE_grounds():
    """The sharper edge: with no raw measure column beside it the row anchors on
    its LABEL alone. This case graded `unmatched` — a wholly correct table
    indistinguishable from a fabricated one — which is why the caution could not
    even be narrowed to `unmatched`."""
    md = ("| State | % change |\n| --- | --- |\n"
          "| CA | +10.0% |\n| TX | -5.0% |\n| NY | +5.0% |\n| FL | +10.0% |\n")
    got = grounding.check_table(md, [_ROWWISE])
    assert got.status == grounding.TABLE_MATCHED, got
    assert (got.cells_matched, got.cells_checked) == (4, 4), got


def test_a_correct_share_of_total_column_still_reproduces():
    """`share` is column-scoped but ROW-INDEXED, so the anchoring rewrite had to
    keep it and pin it to the anchored row. If it were dropped (or left free to
    match any row's share) this correct table would break."""
    md = ("| State | Enrollment | Share |\n| --- | --- | --- |\n"
          "| CA | 110,000 | 25.0% |\n| TX | 190,000 | 43.1% |\n"
          "| NY | 52,500 | 11.9% |\n| FL | 88,000 | 20.0% |\n")
    got = grounding.check_table(md, [_ROWWISE])
    assert got.status == grounding.TABLE_MATCHED, got
    assert (got.cells_matched, got.cells_checked) == (8, 8), got


def test_a_fabricated_table_is_still_caught():
    """Numbers absent from the result stay unmatched — the floor the widened
    match surface must not erode."""
    md = ("| State | 2021 | 2024 |\n| --- | --- | --- |\n"
          "| CA | 123,456 | 234,567 |\n| TX | 345,678 | 456,789 |\n")
    got = grounding.check_table(md, [_ROWWISE])
    assert got.status == grounding.TABLE_UNMATCHED, got
    assert (got.cells_matched, got.cells_checked) == (0, 4), got


def test_a_value_from_ANOTHER_row_does_not_ground():
    """THE precision regression, and the one worth the whole rewrite: CA's row
    carries TX's 190,000. Every digit of it is present in the result, so the old
    column-wide search called it `exact` and the ✓ mark shipped — the single
    likeliest real transcription error (copying a number off the wrong row) was
    the one the check could never see.

    190,000 is also the column MAX, which is why `max`/`min` had to leave the
    anchored op set: they would have re-admitted exactly this cell."""
    md = ("| State | 2021 | 2024 |\n| --- | --- | --- |\n"
          "| CA | 100,000 | 190,000 |\n")
    got = grounding.check_table(md, [_ROWWISE])
    assert (got.cells_matched, got.cells_checked) == (1, 2), \
        f"another row's value must not ground, got {got}"


def test_adjacent_years_do_not_tie_the_anchor():
    """A LIVE false negative found while measuring, and the reason the anchor
    compares by identity instead of _close(): with a relative tolerance, 2023 is
    within 0.1% of 2021/2022/2024/2025, so every row of a by-year result tied,
    the anchor was refused as ambiguous, and correct cells that used to ground
    stopped grounding. False negatives are the direction that would make a
    caution cry wolf, so this one mattered more than the false positives.

    Every cell here needs the anchor to be worth anything: the Total is a
    row-wise sum, which the unrestricted fallback deliberately refuses. Without
    that the fallback would rescue the row and this test would pass with the bug
    still in place — which is exactly what its first draft did.
    """
    by_year = QueryResult(columns=["year", "bachelor", "master"],
                          rows=[(2023, 1290, 1295), (2024, 1289, 1296)], row_count=2)
    md = ("| Year | Bachelor | Master | Total |\n| --- | --- | --- | --- |\n"
          "| 2023 | 1,290 | 1,295 | 2,585 |\n")
    got = grounding.check_table(md, [by_year])
    assert (got.cells_matched, got.cells_checked) == (3, 3), \
        f"adjacent years must not make the anchor ambiguous, got {got}"


def test_a_repeated_value_does_not_evict_an_entity_from_the_anchor_group():
    """THE REGRESSION (live, conversation 17): `_anchor_rows` counted numeric
    matches from a LIST, so a value appearing twice in one table row scored
    twice for whichever result rows hold it — and the tie-only grouping then
    dropped every lower-scoring entity.

    On `| IN | 2,475 | MT | 67 | AK | 67 |` the two 67s give Montana and Alaska
    `(1 label, 2 numbers)` while Indiana scores `(1, 1)` and is EVICTED, so a
    correct 2,475 is graded against Montana's and Alaska's rows and cannot
    ground: `partial 2/3`, a ⚠ on a table whose every number was right.

    Counting the same value twice is double-counting EVIDENCE, not stronger
    evidence — one table row saying "67" twice tells you nothing more about
    Montana than saying it once. Scoring distinct values makes all three tie at
    `(1, 1)` and the group holds all three.

    #331 stopped the model emitting multi-entity rows, so this is defence in
    depth rather than a live fault; the kernel weakness is real either way."""
    states = QueryResult(columns=["state", "masters"],
                         rows=[("IN", 2475), ("MT", 67), ("AK", 67)], row_count=3)
    md = ("| State | Master's | State | Master's | State | Master's |\n"
          "| --- | --- | --- | --- | --- | --- |\n"
          "| IN | 2,475 | MT | 67 | AK | 67 |\n")
    got = grounding.check_table(md, [states])
    assert (got.cells_matched, got.cells_checked) == (3, 3), got


def test_a_merged_table_anchors_in_every_result_it_draws_from():
    """The msg82 shape, which is what surfaced the tie bug: the model builds ONE
    table from several queries. The row must anchor independently per result, or
    the column that came from the second query can never ground."""
    awards = QueryResult(columns=["year", "bachelor", "master"],
                         rows=[(2023, 72757, 16813), (2024, 68382, 16269)], row_count=2)
    insts = QueryResult(columns=["year", "institutions"],
                        rows=[(2023, 1290), (2024, 1289)], row_count=2)
    md = ("| Year | Bachelor | Master | Institutions |\n| --- | --- | --- | --- |\n"
          "| 2023 | 72,757 | 16,813 | 1,290 |\n"
          "| 2024 | 68,382 | 16,269 | 1,289 |\n")
    got = grounding.check_table(md, [awards, insts])
    assert got.status == grounding.TABLE_MATCHED, got
    assert (got.cells_matched, got.cells_checked) == (6, 6), got


def test_an_unanchorable_summary_row_falls_back_to_the_column_search():
    """A row that describes no result row — a Total line — has no anchor, so it
    must fall through to the unrestricted search rather than being marked wrong.
    This fallback is what keeps reshaped and summary tables grounding as before."""
    md = ("| State | Enrollment |\n| --- | --- |\n"
          "| CA | 110,000 |\n| TX | 190,000 |\n| **Total** | 440,500 |\n")
    got = grounding.check_table(md, [_ROWWISE])
    assert got.status == grounding.TABLE_MATCHED, \
        f"a column-total row must still ground via the fallback, got {got}"


def test_a_row_total_column_grounds_for_tables_now():
    """Row totals were figure-only because an unanchored row-wise search across
    hundreds of cells invited coincidences. Anchored, the row is known, so a
    Total column costs one candidate value — and this is the canonical
    by-award-level table shape."""
    md = ("| Year | Bachelor | Master | Total |\n| --- | --- | --- | --- |\n"
          "| 2023 | 72,757 | 16,813 | 89,570 |\n")
    awards = QueryResult(columns=["year", "bachelor", "master"],
                         rows=[(2023, 72757, 16813)], row_count=1)
    got = grounding.check_table(md, [awards])
    assert (got.cells_matched, got.cells_checked) == (3, 3), \
        f"a correct row total must ground in an anchored row, got {got}"


# The live shape from conversation 23: a LONG result the model rendered as a
# PIVOT — one table row per year, one column per modality.
_PIVOT_LONG = QueryResult(
    columns=["year", "modality", "bachelors"],
    rows=[(2021, "fully-online", 6181), (2021, "not-online", 24538),
          (2021, "some-online", 3094), (2022, "fully-online", 6002),
          (2022, "not-online", 24771), (2022, "some-online", 4410)],
    row_count=6)
# The DECOY, and the test is worthless without it: a superseded two-way split
# the model had already replaced. It holds ONE of each year row's numbers, so it
# anchors uniquely — while the result above ties three ways on the same year and
# used to be refused as "ambiguous".
_PIVOT_SUPERSEDED = QueryResult(
    columns=["year", "modality", "bachelors"],
    rows=[(2021, "fully-online", 6181), (2021, "not-fully-online", 27632),
          (2022, "fully-online", 6002), (2022, "not-fully-online", 29181)],
    row_count=4)


def test_a_pivoted_table_row_grounds_against_the_group_it_describes():
    """FOUND LIVE (conversation 23): a ⚠ caution on a table whose every number
    was correct and present in the turn's own results.

    A pivoted table row describes SEVERAL result rows at once — its three
    numbers live in three different rows of the long result. Anchoring to a
    single row then graded all three against one of them, so a correct row
    scored 1 of 3: five year rows, 5 of 15 cells, `partial`.

    Two halves compounded, and the decoy result is what reproduces the second:

      * the result that actually holds all the numbers ties three ways per year
        and was REFUSED as ambiguous;
      * the superseded result matches exactly one row per year, so it anchored
        UNIQUELY and won;
      * and because SOMETHING anchored, check_table took the anchored path and
        never consulted the right result at all.

    Without _PIVOT_SUPERSEDED in the list this passes with the bug still present
    — the ambiguous-refusal alone would fall through to the unrestricted search
    and rescue the row. That is exactly how the first draft of this test fooled
    me.
    """
    md = ("| Year | Fully-Online | Some Online | Not Online |\n"
          "| --- | --- | --- | --- |\n"
          "| 2021 | 6,181 | 3,094 | 24,538 |\n"
          "| 2022 | 6,002 | 4,410 | 24,771 |\n")
    got = grounding.check_table(md, [_PIVOT_SUPERSEDED, _PIVOT_LONG])
    assert (got.cells_matched, got.cells_checked) == (6, 6), \
        f"every pivoted cell is in the results and must ground, got {got}"
    assert got.status == grounding.TABLE_MATCHED, got.status


def test_grouping_still_refuses_another_rows_value():
    """The precision half of the group change, and the bound that makes the
    recall half safe to want.

    A group is every result row sharing the table row's identity — NOT the whole
    column. So a cell copied off a DIFFERENT entity's row must still fail, or
    grouping would have quietly re-opened the column-wide search that row
    anchoring was introduced to close.
    """
    md = ("| Year | Fully-Online | Some Online | Not Online |\n"
          "| --- | --- | --- | --- |\n"
          # 24,771 belongs to 2022, not 2021.
          "| 2021 | 6,181 | 3,094 | 24,771 |\n")
    got = grounding.check_table(md, [_PIVOT_LONG])
    assert (got.cells_matched, got.cells_checked) == (2, 3), \
        f"a value from another year's row must not ground, got {got}"


_YOY = QueryResult(columns=["year", "awards"],
                   rows=[(2021, 500), (2022, 550), (2023, 610), (2024, 700)],
                   row_count=4)


def test_a_year_over_year_change_column_grounds():
    """A SECOND blind spot of the same class, found by probing the anchored
    kernel for what it still couldn't reproduce. A "% vs prior year" column is
    computed against the PREVIOUS ROW — not across the row (the cross-column
    route) and not a whole-column aggregate — so a correct one graded 3/6.

    Both forms appear in real answers, so both are pinned: the percentage and
    the absolute change."""
    pct = ("| Year | Awards | % vs prior |\n| --- | --- | --- |\n"
           "| 2022 | 550 | +10.0% |\n| 2023 | 610 | +10.9% |\n| 2024 | 700 | +14.8% |\n")
    got = grounding.check_table(pct, [_YOY])
    assert (got.cells_matched, got.cells_checked) == (6, 6), got
    absolute = ("| Year | Awards | Change |\n| --- | --- | --- |\n"
                "| 2022 | 550 | +50 |\n| 2023 | 610 | +60 |\n| 2024 | 700 | +90 |\n")
    got = grounding.check_table(absolute, [_YOY])
    assert (got.cells_matched, got.cells_checked) == (6, 6), got


def test_a_WRONG_year_over_year_change_is_still_caught():
    """The route reproduces the change; it does not accept any plausible number
    in its place. 610 from 550 is +10.9%, not +25.0%."""
    md = "| Year | Awards | % vs prior |\n| --- | --- | --- |\n| 2023 | 610 | +25.0% |\n"
    got = grounding.check_table(md, [_YOY])
    assert (got.cells_matched, got.cells_checked) == (1, 2), \
        f"a wrong year-over-year change must not ground, got {got}"


# --- Cell formats found in LIVE answers ---------------------------------------
# Both of these were found by driving real questions through the app and reading
# the cautions, not by review. Each turned a CORRECT answer into a warning.

def test_an_emphasized_cell_is_graded_not_skipped():
    """A bolded cell used to fail parse_number entirely, so it was DROPPED —
    never counted, never checked, invisible. Measured on a live answer: 7 of 14
    numeric cells escaped because the model bolded them, which is its own
    convention for the numbers that matter most. Silent non-coverage is the worst
    shape for a trust mark: it undercounts while sounding authoritative."""
    r = QueryResult(columns=["stabbr", "awards"],
                    rows=[("CA", 11620), ("TX", 6287)], row_count=2)
    md = ("| State | Awards |\n| --- | --- |\n"
          "| CA | **11,620** |\n| TX | `6,287` |\n")
    got = grounding.check_table(md, [r])
    assert (got.cells_matched, got.cells_checked) == (2, 2), \
        f"emphasized cells must be graded, not skipped: {got}"


def test_an_emphasized_WRONG_cell_is_still_caught():
    """The other half — grading them must not mean waving them through."""
    r = QueryResult(columns=["stabbr", "awards"],
                    rows=[("CA", 11620), ("TX", 6287)], row_count=2)
    md = "| State | Awards |\n| --- | --- |\n| CA | **99,999** |\n"
    got = grounding.check_table(md, [r])
    assert (got.cells_matched, got.cells_checked) == (0, 1), got


def test_a_hedged_cell_is_checked_as_a_BOUND():
    """"<0.1%" is a correct hedge for a share that rounds below the displayed
    precision. Reading its digits as an exact quantity compared 0.1 against a
    true 0.0179 and called a correct answer a miss — observed live.

    Verification can only check what was claimed, and an inequality is a weaker
    claim than an equality; that asymmetry is the honest reading, not a loosened
    tolerance."""
    r = QueryResult(columns=["awlevel", "awards"],
                    rows=[(3, 3), (5, 11620), (7, 2697)], row_count=3)
    md = ("| Award level | Awards | Share |\n| --- | --- | --- |\n"
          "| Associate's | 3 | <0.1% |\n")
    got = grounding.check_table(md, [r])
    assert (got.cells_matched, got.cells_checked) == (2, 2), \
        f"a correct hedge must not read as a miss: {got}"


def test_a_hedge_no_value_satisfies_is_still_caught():
    """The guard that keeps the hedge from being a free pass: an inequality is
    only satisfied if something in the data actually satisfies it. Nothing here
    is below 0.1, so the claim fails."""
    r = QueryResult(columns=["stabbr", "share_pct"],
                    rows=[("CA", 40.0), ("TX", 35.0), ("NY", 25.0)], row_count=3)
    md = "| State | share_pct |\n| --- | --- |\n| CA | <0.1 |\n"
    got = grounding.check_table(md, [r])
    assert (got.cells_matched, got.cells_checked) == (0, 1), \
        f"an unsatisfiable bound must not ground: {got}"


def test_a_greater_than_hedge_reads_the_other_way():
    r = QueryResult(columns=["stabbr", "awards"],
                    rows=[("CA", 11620), ("TX", 6287)], row_count=2)
    md = "| State | Awards |\n| --- | --- |\n| CA | >10,000 |\n| TX | >99,000 |\n"
    got = grounding.check_table(md, [r])
    assert (got.cells_matched, got.cells_checked) == (1, 2), \
        f"'>' must hold only where a value exceeds the bound: {got}"


def test_a_human_written_dimension_header_is_not_graded():
    """Observed live: a "CIP" column of codes (52, 51, 13…) was graded as a
    measure. It can never ground — its only match is a dimension column, which
    check_table bars — so it produced five false misses on a correct answer, and
    one false GROUND where the code 11 collided with a share.

    The dimension list was written for snake_case result columns (`cipcode`);
    a table header is written for a human."""
    r = QueryResult(columns=["cipcode", "masters"],
                    rows=[("52", 201792), ("51", 150393)], row_count=2)
    md = ("| CIP | Program | Master's Degrees |\n| --- | --- | --- |\n"
          "| 52 | Business | 201,792 |\n| 51 | Health | 150,393 |\n")
    got = grounding.check_table(md, [r])
    assert (got.cells_matched, got.cells_checked) == (2, 2), \
        f"a human-written code column must not be graded as a measure: {got}"


def test_a_measure_header_that_ENDS_in_year_is_still_graded():
    """The other direction, and a defect I introduced fixing the one above:
    matching the snake_case dimension regex against a space-normalized header
    turned "Change from Prior Year" into "Change_from_Prior_Year", which hits
    `.*_year`. Five legitimate measure cells went SILENTLY ungraded — the same
    invisible non-coverage the emphasis fix had just removed."""
    r = QueryResult(columns=["year", "degrees"],
                    rows=[(2020, 30422), (2021, 33500)], row_count=2)
    md = ("| Year | Degrees | Change from Prior Year |\n| --- | --- | --- |\n"
          "| 2021 | 33,500 | +3,078 |\n")
    got = grounding.check_table(md, [r])
    assert got.cells_checked == 2, \
        f"a measure column whose name ends in 'Year' must still be graded: {got}"
    assert got.cells_matched == 2, got


def test_a_cross_result_share_reproduces():
    """The live failure: rows from one query, the denominator from another. All
    eight unreproduced cells of an ordinary "what share does each of the top 5
    account for?" answer were exact arithmetic across two results, as was its
    hero figure — 11,620/45,883 = 25.3%, 45,883-30,568 = 15,315."""
    top = QueryResult(columns=["instnm", "bachelors"],
                      rows=[("Alpha", 11620), ("Beta", 6287)], row_count=2)
    total = QueryResult(columns=["state_total"], rows=[(45883,)], row_count=1)
    md = ("| Institution | Bachelor's | Share |\n| --- | --- | --- |\n"
          "| Alpha | 11,620 | 25.3% |\n| Beta | 6,287 | 13.7% |\n")
    got = grounding.check_table(md, [top, total])
    assert (got.cells_matched, got.cells_checked) == (4, 4), got
    # The complement — "all others" — and its share of the same total.
    md2 = ("| Group | Degrees | Share |\n| --- | --- | --- |\n"
           "| All others | 27,976 | 61.0% |\n")
    got2 = grounding.check_table(md2, [top, total])
    assert (got2.cells_matched, got2.cells_checked) == (2, 2), got2


def _padding_results(n):
    """`n` single-column results whose totals are all distinct and unrelated —
    the noise that pushes a turn over the old cliff. Deliberately far from the
    values used by the caller's real result so nothing collides by luck."""
    return [QueryResult(columns=[f"pad{i}"], rows=[(31 * (i + 1),)], row_count=1)
            for i in range(n)]


def test_a_complement_still_grounds_on_a_result_rich_turn():
    """THE REGRESSION: `_cross_scalars` appended pairwise complements only when
    it held <= 8 totals, and past that appended NONE — so the widest route in
    the module did not degrade on a result-rich turn, it switched OFF. Measured
    on the retained corpus: 20% of turns with results (17 of 84) were over that
    line, and it is a cliff, not a slope — 8 totals yielded 28 complements, 9
    yielded zero. Conversation-scoped grounding makes it common rather than
    exotic: the prior-results window alone can carry six results before this
    turn's own, and CLAUDE.md's worked example (45,883-30,568 = 15,315) is
    exactly the shape that stopped grounding.

    The real complement here lives in ONE result (state total vs its top
    institution), which is the case the same-result-first ordering exists to
    protect: it must survive even when ten unrelated totals are queued ahead of
    it. A value reachable only as a difference — never as a total or a cell —
    is what makes this test about the complement route and not about `value`."""
    base = QueryResult(columns=["state_total", "top_inst"],
                       rows=[(45883, 30568)], row_count=1)
    # The real result goes LAST, behind ten unrelated totals. In source order its
    # pair sits around index 65 of 66, far outside the ceiling — so this only
    # grounds if same-result pairs are promoted ahead of cross-result ones. With
    # `base` first the test passes either way, which is how the ordering was
    # briefly unpinned (caught by mutating the sort out).
    results = _padding_results(10) + [base]          # 12 totals: over the old cliff
    assert len([n for n, _ in grounding._cross_scalars(results) if "-" in n]), (
        "no complement offered on a 12-total turn — the route switched off")
    md = "| Group | Degrees |\n| --- | --- |\n| All others | 15,315 |\n"
    got = grounding.check_table(md, results)
    assert (got.cells_matched, got.cells_checked) == (1, 1), got


def test_the_complement_set_stays_bounded_on_a_rich_turn():
    """The cap must bound the OUTPUT, not the eligibility to produce any. Twelve
    totals is 66 unordered pairs, and this list is searched per graded cell, so
    an unbounded expansion costs both runtime and precision — measured, the
    fabricated-cell count rises monotonically with the ceiling (28 pairs → 38
    fabricated cells against 30 at six)."""
    complements = [n for n, _v in grounding._cross_scalars(_padding_results(12))
                   if "-" in n]
    assert complements, "still needs SOME complements at the ceiling"
    assert len(complements) <= grounding._MAX_COMPLEMENTS, len(complements)


def test_a_cross_result_SHARE_needs_the_percent_marker():
    """THE precision guard on the widest route in the module, pinned directly
    because the aggregate probe cannot see it: the corpus is uneven enough that
    removing this moved the total by 0.09pp while taking the REAL corpus from
    0.9% to 10.4%.

    A share is written "25.3%" and a count is not, so the marker the model
    already writes decides which of the two cross routes may run. Without it,
    every count in the answer is also tried as a ratio of every pair of totals."""
    top = QueryResult(columns=["instnm", "bachelors"],
                      rows=[("Alpha", 11620), ("Beta", 6287)], row_count=2)
    total = QueryResult(columns=["state_total"], rows=[(45883,)], row_count=1)
    md = ("| Institution | Bachelor's | Pct |\n| --- | --- | --- |\n"
          "| Alpha | 11,620 | 25.3 |\n")
    got = grounding.check_table(md, [top, total])
    assert (got.cells_matched, got.cells_checked) == (1, 2), \
        f"an unmarked ratio must not reach the cross-share route: {got}"


def test_a_cross_result_share_over_100_percent_is_refused():
    """The other guard: a share of a total cannot exceed the total, so a ratio
    that lands above 100% is arithmetic that happened to work out, not a share."""
    small = QueryResult(columns=["label", "n"], rows=[("x", 900)], row_count=1)
    tiny = QueryResult(columns=["denominator"], rows=[(3,)], row_count=1)
    # 900/3*100 = 30,000% — reachable only if the range guard is missing.
    md = "| Label | Pct |\n| --- | --- |\n| x | 30,000% |\n"
    got = grounding.check_table(md, [small, tiny])
    assert got.cells_matched == 0, f"an impossible share must not ground: {got}"


# --- The fabrication probe: an AGGREGATE bound on both directions -------------
# The tests above pin named shapes. This one pins the property those shapes are
# evidence for, because a change can widen the match surface without breaking any
# individual case — which is exactly how the column-wide search stayed wrong for
# two releases while every test passed.
#
# It renders each synthetic result as the table a faithful model would write,
# then re-renders it with every MEASURE cell scaled by 1.2-1.9x. The label and
# dimension cells are left alone on purpose: the realistic failure is a model
# mistyping a number for the RIGHT entity, and keeping the row identifiable is
# what forces the anchored path to be the thing under test.
#
# BOTH bounds are asserted in one test, and that pairing is the point: a checker
# that grounds nothing scores a perfect 0% on fabricated data, so a precision
# bound alone could be satisfied by breaking the feature entirely.

def _cross_pair() -> list[QueryResult]:
    """The top-N-plus-a-separate-total shape: rows from one query, the
    denominator from another `SELECT SUM(...)`. Its share column is only
    reachable through the cross-result route, so it is the ONLY case here that
    exercises it — and without it this probe is blind to that whole class.
    Proven: the route's first draft took fabricated cells from 0.9% to 10.4% on
    the real corpus while this probe moved 0.20% -> 0.23%."""
    top = QueryResult(columns=["instnm", "bachelors"],
                      rows=[("Alpha University", 11620), ("Beta College", 6287),
                            ("Gamma State", 4515), ("Delta Institute", 4313),
                            ("Epsilon University", 3833)], row_count=5)
    total = QueryResult(columns=["state_total"], rows=[(45883,)], row_count=1)
    return [top, total]


def _many_scalars() -> list[QueryResult]:
    """A ranking plus the SIX one-row probe queries a multi-step answer
    accumulates (a max year, two counts, a national total, a min, a max).

    This is the case that makes the cross-result route expensive: six totals
    become six values plus fifteen pairwise complements, and every graded cell is
    tried against all of them. Without a corpus entry this shape, the probe
    passes the route's over-permissive first draft — measured: that draft took
    the REAL corpus from 0.9% to 10.4% while the probe moved 0.20% -> 0.23%,
    because a two-result entry offers only three scalars to collide with.
    """
    ranking = QueryResult(
        columns=["instnm", "degrees"],
        rows=[(f"College {i:02d}", 900 + i * 311) for i in range(40)], row_count=40)
    scalars = [("max_year", 2025), ("institutions", 273), ("awarding", 157),
               ("national_total", 128063), ("max_degrees", 16043), ("median", 4207)]
    return [ranking] + [QueryResult(columns=[n], rows=[(v,)], row_count=1)
                        for n, v in scalars]


def _render_cross(results, rng=None) -> tuple[str, int]:
    """The cross-result table: each row's own value plus its share of the OTHER
    result's total. Only the two measure cells are perturbed — the label stays
    correct, so the row still anchors and the cross route is what is under test."""
    top, total = results[0], results[1].rows[0][0]
    changed = 0
    lines = ["| Institution | Bachelor's | Share |", "| --- | --- | --- |"]
    for name, value in top.rows:
        v, share = value, value / total * 100.0
        if rng is not None:
            v = round(value * rng.uniform(1.2, 1.9)) or value + 1
            share = round(share * rng.uniform(1.2, 1.9), 1)
            changed += 2
        lines.append(f"| {name} | {v:,} | {share:.1f}% |")
    return "\n".join(lines), changed


def _synthetic_corpus() -> list[tuple[str, QueryResult]]:
    """Result shapes mirroring the ones measured on the real corpus."""
    # The dense ranking column: 120 spread values in one column is where a
    # fabricated number found a coincidental EXACT hit 878 times.
    ranking = QueryResult(
        columns=["instnm", "total_degrees"],
        rows=[(f"University {i:03d}", 60 + i * 53) for i in range(120)],
        row_count=120)
    # The pivoted by-award-level breakdown (several measure columns, few rows).
    pivoted = QueryResult(
        columns=["year", "certificate", "associate", "bachelor", "master"],
        rows=[(2021, 9426, 84794, 159890, 50340), (2022, 11091, 85506, 165493, 51554),
              (2023, 11467, 83192, 162729, 51691), (2024, 10005, 82966, 153519, 49832)],
        row_count=4)
    # A two-measure entity table (the "% change" shape's raw form).
    states = QueryResult(
        columns=["stabbr", "enroll_2021", "enroll_2024"],
        rows=[(s, 40000 + i * 7300, 41000 + i * 7900)
              for i, s in enumerate(["CA", "TX", "NY", "FL", "OH", "PA", "IL", "MI"])],
        row_count=8)
    # A TRUNCATED listing: verbatim cells only, no total column. The truncation
    # gate refuses column-EXTENT aggregates, not per-cell lookups, so a listing
    # made entirely of verbatim cells must keep grounding/refusing exactly like
    # any other result -- appended (not inserted) so the shared rng draws below
    # don't reshuffle the other cases' numbers.
    truncated_listing = QueryResult(
        columns=["instnm", "total_degrees"],
        rows=[(f"College {i:03d}", 200 + i * 47) for i in range(60)],
        row_count=60, truncated=True)
    # (name, results, renderer) — an entry can carry SEVERAL results, because a
    # single-result corpus cannot exercise anything cross-result.
    return [("ranking", [ranking], _render), ("pivoted", [pivoted], _render),
            ("states", [states], _render), ("cross-result", _cross_pair(), _render_cross),
            ("many-scalars", _many_scalars(), _render),
            ("truncated-listing", [truncated_listing], _render)]


def _render(results, rng: random.Random | None = None) -> tuple[str, int]:
    """The first result as a Markdown table. With `rng`, every MEASURE cell is
    scaled into a different but plausible number; returns how many it changed so a
    probe can never silently grade still-correct cells."""
    result = results[0]
    measures = set(grounding.measure_columns(result))
    changed = 0
    lines = ["| " + " | ".join(result.columns) + " |",
             "| " + " | ".join("---" for _ in result.columns) + " |"]
    for row in result.rows:
        cells = []
        for name, cell in zip(result.columns, row, strict=True):
            if rng is not None and name in measures and isinstance(cell, (int, float)):
                scaled = round(cell * rng.uniform(1.2, 1.9))
                cell = scaled if scaled != cell else cell + 1
                changed += 1
            cells.append(str(cell) if grounding.is_dimension(name) or
                         not isinstance(cell, (int, float)) else f"{cell:,}")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines), changed


# Measured 0.20% on this corpus with the anchored kernel; 8.1% with anchoring
# disabled (and 24% on the real corpus before this change). The bound sits an
# order of magnitude above what passes and well below what regresses: loose
# enough that adding a legitimate derivation op won't trip it, tight enough to
# still be a gate rather than a rubber stamp.
_MAX_FALSE_GROUND_PCT = 2.0


def test_fabricated_numbers_are_rejected_at_scale():
    corpus = _synthetic_corpus()
    checked = matched = 0
    for name, results, render in corpus:
        md, _ = render(results)
        got = grounding.check_table(md, results)
        assert got.status == grounding.TABLE_MATCHED, f"{name}: faithful table -> {got}"
        checked, matched = checked + got.cells_checked, matched + got.cells_matched
    assert matched == checked, \
        f"a faithfully rendered table must ground fully ({matched}/{checked})"

    rng = random.Random(20260725)
    f_checked = f_matched = 0
    for name, results, render in corpus:
        c = m = 0
        for _ in range(20):
            md, n = render(results, rng)
            assert n, f"{name}: the probe changed nothing — it would prove nothing"
            got = grounding.check_table(md, results)
            c, m = c + got.cells_checked, m + got.cells_matched
        # PER CASE, not just in aggregate. The corpus is deliberately uneven —
        # the dense ranking contributes 2,400 of ~4,000 cells at 0%, so a case
        # that doubled its false grounds moved the total by 0.09pp and sailed
        # under the bound. A regression confined to one shape is the likely
        # kind, so the shape it hits has to be what fails.
        rate = 100.0 * m / c
        assert rate <= _MAX_FALSE_GROUND_PCT, (
            f"{name}: {rate:.1f}% of fabricated cells grounded ({m}/{c}) — over the "
            f"{_MAX_FALSE_GROUND_PCT}% bound; the match surface has widened")
        f_checked, f_matched = f_checked + c, f_matched + m
    rate = 100.0 * f_matched / f_checked
    assert rate <= _MAX_FALSE_GROUND_PCT, (
        f"{rate:.1f}% of fabricated cells grounded ({f_matched}/{f_checked}) — over the "
        f"{_MAX_FALSE_GROUND_PCT}% bound; the match surface has widened")


def test_a_wrong_row_total_is_still_caught():
    """The other half: the row-wise route must reproduce the total, not accept
    any plausible number in its place."""
    md = ("| Year | Bachelor | Master | Total |\n| --- | --- | --- | --- |\n"
          "| 2023 | 72,757 | 16,813 | 91,000 |\n")
    awards = QueryResult(columns=["year", "bachelor", "master"],
                         rows=[(2023, 72757, 16813)], row_count=1)
    got = grounding.check_table(md, [awards])
    assert (got.cells_matched, got.cells_checked) == (2, 3), \
        f"a wrong row total must not ground, got {got}"


def test_a_label_only_table_is_no_table():
    # No numeric cells to grade (an address/accreditor lookup rendered as a table).
    md = "| Field | Value |\n| --- | --- |\n| City | Columbus |\n| State | Ohio |\n"
    got = grounding.check_table(md, [YEARS])
    assert got.status == grounding.NO_TABLE, got


def test_check_table_never_raises_on_junk():
    for text in [None, "", "| broken", "|||", "```\nunclosed fence\n"]:
        grounding.check_table(text, [YEARS])
    grounding.check_table(_TABLE_MD, None)


# --- Row-wise totals (the SECOND false `ungrounded` found in production) --------

def _pivoted() -> QueryResult:
    """A by-award-level breakdown — the canonical shape behind a peak-year hero
    stat, and the exact result that produced the observed false negative."""
    return QueryResult(
        columns=["year", "associate", "bachelor", "master", "doctorate", "certificate"],
        rows=[(2021, 84794, 159890, 50340, 10019, 9426),
              (2022, 85506, 165493, 51554, 10931, 11091),
              (2023, 83192, 162729, 51691, 12103, 11467),
              (2024, 82966, 153519, 49832, 13051, 10005),
              (2025, 85227, 153285, 48868, 13759, 11627)])


def test_a_row_total_grounds():
    """THE REGRESSION: every other op aggregates DOWN a column, so a figure that
    totals ACROSS one row had no route and reported `ungrounded` despite being
    exactly reproducible. Observed live: "324,575 — peak national nursing degrees
    in 2022" is the row-wise sum of that year's five award-level columns. A kernel
    that cannot reproduce a CORRECT number manufactures evidence of model error."""
    r = grounding.check_figure(
        {"value": "324,575", "label": "Peak national nursing degrees in 2022"}, [_pivoted()])
    assert r.status == grounding.DERIVED, f"a reproducible row total must ground, got {r.status}"
    assert r.derivation and r.derivation.op == "row_total", r.derivation
    # It names WHICH row, so a reviewer can check the claim.
    assert r.derivation.describe() == "row_total(q1.row2)", r.derivation.describe()


def test_the_year_column_is_not_added_into_the_total():
    """2022 + the five measures would be 326,597 — a different number. If the
    dimension column leaked into the sum, THAT is what would ground."""
    r = grounding.check_figure({"value": "326,597", "label": "bogus"}, [_pivoted()])
    assert r.status == grounding.UNGROUNDED, \
        f"a total that includes the year column must NOT ground, got {r.status}"


def test_a_single_measure_column_has_no_row_total():
    """With one measure column the 'row total' is just the cell, which `value`
    already covers — adding a route here would only invent coincidental matches."""
    one = QueryResult(columns=["year", "awards"], rows=[(2021, 500), (2022, 767)])
    r = grounding.check_figure({"value": "767", "label": "x"}, [one])
    assert r.status == grounding.EXACT and r.derivation.op == "value", \
        f"expected the verbatim cell, got {r.status}/{r.derivation}"


def test_a_verbatim_cell_still_beats_a_row_total():
    """Ordering matters: row totals are the weakest route and must never displace
    a cell that is present verbatim."""
    r = grounding.check_figure({"value": "165,493", "label": "bachelor's in 2022"}, [_pivoted()])
    assert r.status == grounding.EXACT and r.derivation.op == "value", \
        f"expected the verbatim cell to win, got {r.status}/{r.derivation}"


def test_an_UNANCHORED_table_cell_still_does_not_use_row_totals():
    """Row totals reach table cells ONLY through an anchored row (see
    test_a_row_total_column_grounds_for_tables_now). The unrestricted fallback
    still refuses them, which is what keeps the original reasoning intact: an
    unanchored search across hundreds of cells is where a free-floating row-wise
    route would manufacture coincidental hits.

    This row cannot anchor — its only identity is the year 2022, one numeric
    match, and a lone numeric match is deliberately not enough."""
    md = "| Year | Total |\n| --- | --- |\n| 2022 | 324,575 |"
    r = grounding.check_table(md, [_pivoted()])
    assert r.cells_matched == 0, \
        f"an unanchored row total must not ground a table cell (matched {r.cells_matched})"


# --- Truncation and grounding ---------------------------------------------------
# app/tools/sql.py's own comment on the ⚠ AGGREGATION CHECK note names this
# exactly: summing/counting/averaging a CUT page as a TOTAL "looks perfect (and
# which grounding would "validate" — the same partial rows recompute the same
# wrong total)". Before this section QueryResult.truncated was never read by
# this module at all (`grep -c truncat app/grounding.py` -> 0): a figure/table
# cell reconciled against a truncated result exactly as if nothing had been
# cut, so the model's wrong total came back "verified".
#
# The rule: a route may run over a truncated result IFF its value is invariant
# to appending the rows that were cut. Truncation removes a SUFFIX of rows, so
# a value at a KNOWN ROW is invariant (a verbatim cell, a row-wise op, a
# row-anchored prev-row op); anything that reads the column's EXTENT (sum,
# mean, max, min, share, pct_change, diff, and a cross-result total/complement
# sourced FROM a truncated result) is not, and must refuse. There is no new
# status: a refused figure lands as the existing UNGROUNDED, a refused table
# cell simply doesn't count toward cells_matched.

def test_a_column_sum_over_a_truncated_result_does_not_ground():
    """THE HEADLINE regression. app/tools/sql.py's own comment on the
    truncation note: 'grounding would "validate" — the same partial rows
    recompute the same wrong total'. A SUM over a result CUT by
    sql_row_cap_model must not ground, even though the arithmetic over the
    VISIBLE page is exact — the model may have reported the same wrong total
    sql.py warns about, and check_figure must not corroborate it."""
    rows = [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)]
    r = result(["year", "awards"], rows, truncated=True)
    total = sum(v for _, v in rows)
    assert total == 4550
    got = grounding.check_figure({"value": "4,550", "label": "Total awards"}, [r])
    assert got.status == grounding.UNGROUNDED, got


def test_a_truncated_mean_max_min_and_share_all_refuse():
    """Catches a fix that gates `sum` alone and leaves its column-extent
    siblings reachable. mean and share are genuinely refused: neither's
    computed value ever coincides with a raw cell, so there is no other route
    that could ground them by accident.

    max/min are a documented EXCEPTION, not a gap: compute("max"/"min", ...)
    always returns an actual cell (the largest/smallest cell IS one of the
    cells), so a claimed max/min is indistinguishable from a verbatim-cell
    claim and grounds via the always-allowed EXACT "value" route regardless of
    truncation — what the gate changes for them is that "max"/"min" itself
    never fires as the REPORTED derivation over a truncated result."""
    rows = [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)]
    values = [v for _, v in rows]
    r = result(["year", "awards"], rows, truncated=True)
    mean = sum(values) / len(values)
    assert mean == 1137.5
    got = grounding.check_figure({"value": "1,137.5", "label": "Average awards"}, [r])
    assert got.status == grounding.UNGROUNDED, got
    share = values[-1] / sum(values) * 100.0
    got = grounding.check_figure({"value": f"{share:.1f}%", "label": "2024 share"}, [r])
    assert got.status == grounding.UNGROUNDED, got
    got = grounding.check_figure({"value": "1,250", "label": "Max"}, [r])
    assert got.status == grounding.EXACT and got.derivation.op == "value", got
    got = grounding.check_figure({"value": "1,000", "label": "Min"}, [r])
    assert got.status == grounding.EXACT and got.derivation.op == "value", got


def test_a_column_pct_change_over_a_truncated_result_does_not_ground():
    """Catches "it isn't an aggregate so it's safe" reasoning: pct_change reads
    like a two-point calculation, but `values[-1]` is the CUT BOUNDARY once a
    result is truncated, not the series' true endpoint — both its start and end
    are only "the last row I happen to have", which shifts with the cap."""
    rows = [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)]
    r = result(["year", "awards"], rows, truncated=True)
    pct = (1250 - 1000) / 1000 * 100.0
    assert pct == 25.0
    got = grounding.check_figure({"value": "+25.0%", "label": "Change since 2021"}, [r])
    assert got.status == grounding.UNGROUNDED, got


def test_a_truncated_result_does_not_supply_a_cross_result_total():
    """`_cross_scalars` feeds the widest search in the module: a measure
    column's SUM from ANY retained result, plus pairwise complements. If that
    result was CUT, its sum is the same wrong partial total sql.py's note
    warns about — so a truncated result must be SKIPPED as a source of
    cross-result totals/complements entirely, not merely have its own cells
    excluded from the in-result sum route (see the previous test).

    NOTE ON WHAT THIS DOES AND DOESN'T COVER: this exercises `_cross_scalars`'
    OWN `if result.truncated: continue` guard, a mechanism entirely separate
    from `_reconcile_value`'s per-result `whole_column=not result.truncated`
    gate on the plain column sum/mean/share routes. Mutating the LATTER to a
    per-turn form (`not any(r.truncated for r in results)`) does not touch
    `_cross_scalars` at all, so this test still passes under that mutation —
    it is not coverage of the `whole_column` gate. See
    test_an_untruncated_result_still_grounds_its_own_column_sum_when_a_SIBLING_is_truncated
    for that."""
    top, total = _cross_pair()
    top = QueryResult(columns=top.columns, rows=top.rows, truncated=True,
                      row_count=top.row_count)
    page_total = sum(v for _, v in top.rows)
    assert page_total == 30568
    grand_total = total.rows[0][0]
    assert grand_total == 45883
    share = page_total / grand_total * 100.0
    got = grounding.check_figure(
        {"value": f"{share:.1f}%", "label": "Top 5 share of the state total"},
        [top, total])
    assert got.status == grounding.UNGROUNDED, got
    complement = grand_total - page_total
    assert complement == 15315
    got2 = grounding.check_figure(
        {"value": f"{complement:,}", "label": "All others"}, [top, total])
    assert got2.status == grounding.UNGROUNDED, got2


def test_a_table_column_total_over_a_truncated_result_is_not_matched():
    """A per-row "Grand Total" cell repeating the WHOLE column's sum is
    verified through the anchored row's column-scoped route
    (_ANCHORED_COLUMN_OPS) — a column-extent aggregate exactly as vulnerable to
    a truncated cut as the figure path. The row's own verbatim cell still
    grounds; only the aggregate refuses, so the table reads PARTIAL rather than
    either extreme."""
    rows = [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)]
    r = result(["year", "awards"], rows, truncated=True)
    total = sum(v for _, v in rows)
    assert total == 4550
    md = ("| Year | Awards | Grand Total |\n| --- | --- | --- |\n"
          f"| 2021 | 1,000 | {total:,} |\n")
    got = grounding.check_table(md, [r])
    assert got.cells_checked == 2, got
    assert got.cells_matched < got.cells_checked, \
        f"a column-extent total over a truncated result must not fully match: {got}"
    assert got.status == grounding.TABLE_PARTIAL, got


def test_a_share_of_a_truncated_column_total_is_not_matched():
    """The `share` sub-branch of the anchored route is pinned to THIS row but
    still divides by the whole column's SUM — the same truncated denominator as
    the Grand Total case above, only expressed as a percentage."""
    rows = [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)]
    r = result(["year", "awards"], rows, truncated=True)
    total = sum(v for _, v in rows)
    share = 1000 / total * 100.0
    md = (f"| Year | Awards | Share |\n| --- | --- | --- |\n"
          f"| 2021 | 1,000 | {share:.1f}% |\n")
    got = grounding.check_table(md, [r])
    assert got.cells_checked == 2, got
    assert got.cells_matched < got.cells_checked, \
        f"a share of a truncated column total must not match: {got}"
    assert got.status == grounding.TABLE_PARTIAL, got


def test_a_verbatim_cell_in_a_truncated_result_still_grounds():
    """A present row is not made wrong by the cut: the recall counterpart to
    the sum test above — same result and truncation, a cell instead of a
    total."""
    rows = [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)]
    r = result(["year", "awards"], rows, truncated=True)
    got = grounding.check_figure({"value": "1,250", "label": "Awards"}, [r])
    assert got.status == grounding.EXACT, got
    assert got.derivation.column == "awards", got.derivation


def test_an_untruncated_result_still_grounds_every_aggregate_op():
    """The anti-vacuity pair for the three refusal tests above: the IDENTICAL
    rows, only untruncated, must still ground every aggregate op. If this test
    failed alongside the refusal tests it would prove the new gate keys on
    something other than `.truncated` — e.g. always refusing aggregates, or a
    coincidence in the column name."""
    rows = [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)]
    r = result(["year", "awards"], rows, truncated=False)
    got = grounding.check_figure({"value": "4,550", "label": "Total"}, [r])
    assert got.status == grounding.DERIVED and got.derivation.op == "sum", got
    got = grounding.check_figure({"value": "1,137.5", "label": "Average"}, [r])
    assert got.status == grounding.DERIVED and got.derivation.op == "mean", got
    got = grounding.check_figure({"value": "+25.0%", "label": "Change"}, [r])
    assert got.status == grounding.DERIVED and got.derivation.op == "pct_change", got
    got = grounding.check_figure({"value": "250", "label": "Absolute change"}, [r])
    assert got.status == grounding.DERIVED and got.derivation.op == "diff", got
    got = grounding.check_figure({"value": "27.5%", "label": "2024 share"}, [r])
    assert got.status == grounding.DERIVED and got.derivation.op == "share", got
    # max/min always ground via the always-allowed verbatim "value" route (see
    # the previous refusal test's docstring) — unaffected by truncation either
    # way, so the recall bound here is only that they still ground SOMEHOW.
    got = grounding.check_figure({"value": "1,250", "label": "Max"}, [r])
    assert got.grounded, got
    got = grounding.check_figure({"value": "1,000", "label": "Min"}, [r])
    assert got.grounded, got


def test_a_row_total_still_grounds_in_a_truncated_result():
    """row_total is built from ONE held row's own cells (_row_totals) and is
    unaffected by rows that were cut — allowed even when truncated, the
    truncated counterpart of test_a_row_total_grounds."""
    p = _pivoted()
    truncated = result(p.columns, p.rows, truncated=True)
    got = grounding.check_figure(
        {"value": "324,575", "label": "Peak national nursing degrees in 2022"},
        [truncated])
    assert got.status == grounding.DERIVED and got.derivation.op == "row_total", got
    assert got.derivation.describe() == "row_total(q1.row2)", got.derivation.describe()


def test_a_row_wise_pct_change_column_still_grounds_in_a_truncated_result():
    """The anchored row-wise routes work from ONE anchored row's own cells and
    must stay reachable under truncation — the truncated counterpart of
    test_a_correct_row_wise_pct_change_column_grounds."""
    truncated = QueryResult(columns=_ROWWISE.columns, rows=_ROWWISE.rows,
                            truncated=True, row_count=_ROWWISE.row_count)
    md = ("| State | 2021 | 2024 | % change |\n| --- | --- | --- | --- |\n"
          "| CA | 100,000 | 110,000 | +10.0% |\n"
          "| TX | 200,000 | 190,000 | -5.0% |\n"
          "| NY | 50,000 | 52,500 | +5.0% |\n"
          "| FL | 80,000 | 88,000 | +10.0% |\n")
    got = grounding.check_table(md, [truncated])
    assert got.status == grounding.TABLE_MATCHED, got
    assert (got.cells_matched, got.cells_checked) == (12, 12), got


def test_a_prior_row_change_column_still_grounds_in_a_truncated_result():
    """prev_diff/prev_pct_change compare an anchored row against the PREVIOUS
    anchored row — both endpoints are held rows, so this is allowed under
    truncation. Catches a fix that wraps the WHOLE anchored column route in the
    truncation guard instead of scoping it to the column-extent ops."""
    truncated = QueryResult(columns=_YOY.columns, rows=_YOY.rows, truncated=True,
                            row_count=_YOY.row_count)
    pct = ("| Year | Awards | % vs prior |\n| --- | --- | --- |\n"
           "| 2022 | 550 | +10.0% |\n| 2023 | 610 | +10.9% |\n| 2024 | 700 | +14.8% |\n")
    got = grounding.check_table(pct, [truncated])
    assert (got.cells_matched, got.cells_checked) == (6, 6), got


def test_a_cross_result_share_still_grounds_when_only_the_numerator_result_is_truncated():
    """app/tools/sql.py and app/prompt.py both tell the model to fix a
    truncated ranking by running a SEPARATE `SELECT SUM(...)` for the true
    total — so a cut top-N page plus a one-row scalar from an UNTRUNCATED
    query must stay fully verifiable for the TABLE path (check_table's row
    anchoring + `_match_at_row`'s cross-result route).

    CORRECTED CLAIM (was headlined "THE MOST IMPORTANT recall test" —
    mutation-tested and found not to earn that): this exercises
    `_cross_scalars`' own per-result `if result.truncated: continue` guard,
    the SAME mechanism as test_a_truncated_result_does_not_supply_a_
    cross_result_total above, not `_reconcile_value`'s separate `whole_column`
    gate on the plain column sum/mean/share routes. A per-turn version of
    THAT gate (`whole_column=not any(r.truncated for r in results)`) leaves
    `_cross_scalars` untouched and this test keeps passing — it does not
    discriminate per-result from per-turn for `whole_column`. That
    discrimination is
    test_an_untruncated_result_still_grounds_its_own_column_sum_when_a_SIBLING_is_truncated,
    which mutation-testing confirmed DOES fail under the per-turn form. This
    test is kept for its own real property (the table cross-result route
    tolerates a truncated numerator result), just not the one its old name
    claimed."""
    top, total = _cross_pair()
    top = QueryResult(columns=top.columns, rows=top.rows, truncated=True,
                      row_count=top.row_count)
    md = ("| Institution | Bachelor's | Share |\n| --- | --- | --- |\n"
          "| Alpha University | 11,620 | 25.3% |\n"
          "| Beta College | 6,287 | 13.7% |\n")
    got = grounding.check_table(md, [top, total])
    assert (got.cells_matched, got.cells_checked) == (4, 4), got
    assert got.status == grounding.TABLE_MATCHED, got


def test_an_untruncated_result_still_grounds_its_own_column_sum_when_a_SIBLING_is_truncated():
    """THE MOST IMPORTANT recall test for `_reconcile_value`'s `whole_column`
    gate specifically (the property the two tests above turned out NOT to
    cover — mutation-tested and found to pass under a per-turn version of
    THIS gate).

    `_reconcile_value` passes `whole_column=not result.truncated`, computed
    FRESH for the result the candidate column actually belongs to. A plausible
    but wrong rewrite — `whole_column=not any(r.truncated for r in results)`
    — checks the whole TURN instead: every aggregate refuses the instant any
    result in the turn was cut, even one this candidate column has nothing to
    do with. That is a real cost, not a theoretical one: it is exactly the
    remediation sql.py/prompt.py instruct — a truncated ranking page plus a
    SEPARATE untruncated query — so the per-turn bug would refuse the
    honest, fully-reproducible total from the second query merely because the
    first was cut.

    The figure is the untruncated sibling's own column SUM, chosen (and
    verified below) to coincide with no raw cell in EITHER result at the
    rounding tolerance "4,550" implies — otherwise the always-allowed
    verbatim/rounded-cell route would match it regardless of `whole_column`,
    and the test would pass for the wrong reason exactly like the two above.

    Verified both ways per the coordinator's request, by temporarily applying
    the exact mutation to app/grounding.py and restoring the file afterward:
    passes green against the code as shipped (DERIVED, op "sum",
    result_index 1); under the mutation the assertion still fails, though NOT
    via UNGROUNDED as first assumed -- with the in-result sum route gated
    off, the value still falls through to the module's cross-result fallback
    (`_cross_scalars` is untouched by this particular mutation, and the
    sibling's own scalar is still in its candidate list), landing on
    `derivation.op == "cross"` / `result_index == -1` instead of the correct
    in-result `"sum"` / `1`. Asserting the SPECIFIC op and result_index, not
    just DERIVED-ness, is what catches that -- a looser
    `got.status == grounding.DERIVED` alone would NOT discriminate here."""
    top, _unused_total = _cross_pair()
    truncated_ranking = QueryResult(columns=top.columns, rows=top.rows,
                                    truncated=True, row_count=top.row_count)
    sibling_rows = [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)]
    sibling = result(["year", "awards"], sibling_rows, truncated=False)
    sibling_sum = sum(v for _, v in sibling_rows)
    assert sibling_sum == 4550
    # Not reachable via the always-allowed verbatim/rounded routes in EITHER
    # result at the "4,550" rounding tolerance (+/-5) -- if it were, this test
    # would pass whether the gate is per-result or per-turn, and would not
    # discriminate.
    all_cells = [v for _, v in top.rows] + [v for _, v in sibling_rows]
    assert all(abs(sibling_sum - c) > 5 for c in all_cells), all_cells

    got = grounding.check_figure(
        {"value": f"{sibling_sum:,}", "label": "Total awards (untruncated query)"},
        [truncated_ranking, sibling])
    assert got.status == grounding.DERIVED, got
    assert got.derivation.op == "sum", got.derivation
    assert got.derivation.result_index == 1, \
        f"expected the derivation to name the untruncated sibling (index 1): {got.derivation}"


# --- Truncation telemetry: blocked_by_truncation / cells_blocked --------------
# Both fields exist so a false ⚠/UNGROUNDED caused by the truncation gate is
# DISTINGUISHABLE from an ordinary transcription miss or an invented number —
# check_table's own docstring makes exactly this false-alarm-measurability
# promise (pointing at the figure's "truncated" figure_derivation label as the
# fallback measurement), but the label it points to is written only by
# check_figure, and only on an already-failed figure — a truncated-turn TABLE
# that raises the caution records nothing distinguishing a gate refusal from
# an ordinary miss. These tests pin both halves of the fix.

def test_blocked_by_truncation_is_false_for_a_figure_nothing_could_reproduce():
    """THE REGRESSION. check_figure currently sets `blocked = any(r.truncated
    for r in results)` — true whenever ANY retained result was cut, whether or
    not the gate is what refused THIS figure. An invented number (8,675,309 —
    the reviewer's own example) would not be reproduced even with the gate
    disabled, so `blocked_by_truncation` must read False here; the `any()`
    form reads True purely because a truncated result happens to be in the
    mix, which is exactly the over-reporting the reviewer measured (a wholly
    invented figure recording figure_derivation="truncated")."""
    rows = [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)]
    r = result(["year", "awards"], rows, truncated=True)
    got = grounding.check_figure({"value": "8,675,309", "label": "Awards"}, [r])
    assert got.status == grounding.UNGROUNDED, got
    assert got.blocked_by_truncation is False, \
        f"an unreproducible number must not read as gate-blocked: {got}"


def test_blocked_by_truncation_is_true_only_when_the_gate_is_what_refused():
    """The positive half, so the fix can't be 'always False': the truncated
    result's own column SUM — reproducible with the gate open, refused only
    because it is closed — must still read blocked_by_truncation=True. This
    already passes under the current `any()` form (it isn't the discriminator
    on its own), but a fix that always returns False would fail it."""
    rows = [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)]
    r = result(["year", "awards"], rows, truncated=True)
    total = sum(v for _, v in rows)
    assert total == 4550
    got = grounding.check_figure({"value": "4,550", "label": "Total awards"}, [r])
    assert got.status == grounding.UNGROUNDED, got
    assert got.blocked_by_truncation is True, \
        f"a value the gate alone refused must still read as gate-blocked: {got}"


def test_check_table_counts_cells_only_a_truncated_aggregate_would_have_matched():
    """THE REGRESSION for the table half: `TableGroundingCheck` has no
    `cells_blocked` field at all today, so a truncated-turn table that raises
    the ⚠ caution records nothing distinguishing a gate refusal from an
    ordinary transcription miss — exactly the gap check_table's own docstring
    claims is covered (it isn't; that label is figure-only).

    A `cells_blocked` that simply equalled `cells_checked - cells_matched`
    would be worthless — it would say nothing the existing two counts don't
    already say. So this fixture carries THREE kinds of cell: a verbatim cell
    (always matches, regardless of truncation), the truncated result's own
    column SUM (gate-blocked — would match if the gate were open, the "Total"
    cell), and a wholly fabricated cell (would never match regardless of the
    gate). Only the middle one may count as blocked."""
    rows = [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)]
    r = result(["year", "awards"], rows, truncated=True)
    total = sum(v for _, v in rows)
    assert total == 4550
    md = ("| Year | Awards | Total |\n| --- | --- | --- |\n"
          f"| 2021 | 1,000 | {total:,} |\n"
          "| 2022 | 88,888 | — |\n")
    got = grounding.check_table(md, [r])
    assert got.cells_checked == 3, got
    assert got.cells_matched == 1, got   # only the 2021 Awards cell (1,000)
    assert got.status == grounding.TABLE_PARTIAL, got
    # getattr, not direct attribute access: TableGroundingCheck has no
    # cells_blocked field yet, and a bare `got.cells_blocked` would raise
    # AttributeError, which the hand-rolled `check()` runner (catches
    # AssertionError only) can't report as a clean failure.
    assert getattr(got, "cells_blocked", None) == 1, \
        f"exactly the Total cell (gate-blocked) should count, not the " \
        f"fabricated 88,888 cell: {got}"


def test_cells_blocked_is_zero_when_no_result_was_truncated():
    """Anti-vacuity partner: an ordinary, wholly wrong table with NO truncated
    result anywhere must report cells_blocked=0 — catches a `cells_blocked`
    that defaults to something nonzero, or that counts every unmatched cell
    regardless of cause (which the previous test's fabricated-cell half
    already guards from the other side)."""
    md = "| A | B |\n| --- | --- |\n| 88888 | 77777 |\n"
    got = grounding.check_table(md, [YEARS])
    assert got.status == grounding.TABLE_UNMATCHED, got
    assert getattr(got, "cells_blocked", None) == 0, \
        f"nothing here was refused by the truncation gate: {got}"


# --- Truncated-result persistence (to_storage/from_storage) -------------------
# to_storage previously emitted only {"columns","rows"} and DROPPED `truncated`
# entirely, so from_storage always rebuilt truncated=False — a borrowed prior
# result (grounding is conversation-scoped) could never refuse an aggregate, no
# matter how the checks above behave live.

def test_to_storage_preserves_the_truncated_flag():
    """Without this, a borrowed prior result can never refuse an aggregate,
    regardless of what check_figure/check_table do with the flag. `.get()`,
    not `[...]`, so a not-yet-fixed to_storage fails this as a clean
    AssertionError rather than a KeyError the hand-rolled `check()` runner
    (catches AssertionError only) can't report."""
    r = result(["n"], [(1,), (2,)], truncated=True)
    assert r.to_storage().get("truncated") is True


def test_to_storage_flags_a_result_its_own_row_cap_cut():
    """to_storage's OWN max_rows cap can cut a result that run_sql itself
    never flagged as truncated (persistence's cap need not match the
    model-facing row cap). The stored blob must reflect ITS OWN cut, not just
    whatever the QueryResult carried in."""
    r = result(["n"], [(i,) for i in range(500)], truncated=False)
    blob = r.to_storage(max_rows=200)
    assert blob.get("truncated") is True, blob
    assert len(blob["rows"]) == 200, blob


def test_from_storage_defaults_truncated_false_for_a_legacy_blob():
    """A pre-change blob has no 'truncated' key at all — from_storage must
    default False so an existing persisted turn behaves exactly as before. And
    an UNTRUNCATED to_storage blob must still carry NO key (never a literal
    False), which is what keeps test_chat_router.py's exact blob literal
    `[{"columns": ["n"], "rows": [[1], [2]]}]` valid."""
    legacy = {"columns": ["n"], "rows": [[1], [2]]}
    back = QueryResult.from_storage(legacy)
    assert back.truncated is False
    r = result(["n"], [(1,), (2,)], truncated=False)
    blob = r.to_storage()
    assert "truncated" not in blob, blob


def test_a_borrowed_prior_result_refuses_an_aggregate_after_a_round_trip():
    """The end-to-end for the borrowed-results half: grounding is
    conversation-scoped, so a prior turn's results are re-hydrated via
    from_storage for a LATER turn to check against. A persisted truncation
    must survive that round trip, or a borrowed truncated result could
    validate the same wrong total the LIVE turn correctly refuses."""
    rows = [(2021, 1000), (2022, 1100), (2023, 1200), (2024, 1250)]
    r = result(["year", "awards"], rows, truncated=True)
    back = QueryResult.from_storage(r.to_storage())
    assert back.truncated is True
    got = grounding.check_figure({"value": "4,550", "label": "Total awards"}, [back])
    assert got.status == grounding.UNGROUNDED, got
    got = grounding.check_figure({"value": "1,250", "label": "Awards"}, [back])
    assert got.status == grounding.EXACT, got


def run():
    print("Testing figure grounding (app/grounding.py)...")
    check("QueryResult storage round-trip preserves columns/cells",
          test_storage_round_trip_preserves_columns_and_cells)
    check("to_storage caps rows", test_to_storage_caps_rows)
    check("from_storage tolerates a malformed blob",
          test_from_storage_tolerates_a_malformed_blob)
    check("parse_number handles the prompt's formats",
          test_parse_number_handles_the_formats_the_prompt_asks_for)
    check("parse_number rejects non-numbers", test_parse_number_rejects_non_numbers)
    check("numeric_columns keeps row order, skips label columns",
          test_numeric_columns_keeps_row_order_and_skips_label_columns)
    check("numeric_columns rejects a mixed column",
          test_numeric_columns_rejects_a_mixed_column)
    check("numeric_columns skips nulls",
          test_numeric_columns_skips_nulls_without_dropping_the_column)
    check("compute matches the prompt's derivation menu",
          test_compute_matches_the_prompts_derivation_menu)
    check("a derived ABSOLUTE change is not flagged (production false positive)",
          test_a_derived_absolute_change_is_not_flagged)
    check("diff needs 2 points but tolerates a zero baseline",
          test_diff_needs_two_points_but_tolerates_a_zero_baseline)
    check("diff stays barred on a dimension column",
          test_diff_stays_barred_on_a_dimension_column)
    check("compute degrades instead of raising",
          test_compute_degrades_instead_of_raising)
    check("an invented number is ungrounded", test_an_invented_number_is_ungrounded)
    check("a plausible but wrong total is ungrounded",
          test_a_plausible_but_wrong_total_is_ungrounded)
    check("a verbatim cell is exact", test_a_verbatim_cell_is_exact)
    check("a derived % change is not flagged", test_a_derived_pct_change_is_not_flagged)
    check("a derived share is not flagged", test_a_derived_share_is_not_flagged)
    check("a column sum is not flagged", test_a_column_sum_is_not_flagged)
    check("a dimension column is never aggregated (collision regression)",
          test_a_dimension_column_is_never_aggregated)
    check("display rounding is not flagged", test_display_rounding_is_not_flagged)
    check("rounding tolerance doesn't swallow a real mismatch",
          test_rounding_tolerance_does_not_swallow_a_real_mismatch)
    check("a small percentage keeps its WRITTEN precision",
          test_a_small_percentage_keeps_the_precision_it_was_WRITTEN_to)
    check("a small percentage outside its own window still fails",
          test_a_small_percentage_outside_its_own_rounding_window_still_fails)
    check("a sub-1% table cell keeps its written precision too",
          test_a_table_cell_under_one_percent_also_keeps_its_written_precision)
    check("trailing zeros can't license a huge rounding window",
          test_trailing_zeros_alone_cannot_license_a_huge_rounding_window)
    check("a magnitude-suffix figure is measured, not dropped",
          test_a_magnitude_suffix_figure_is_measured_not_dropped)
    check("no-figure / non-numeric headline are not measured",
          test_no_figure_and_non_numeric_headline_are_not_measured)
    check("no results is 'unchecked', not 'ungrounded'",
          test_a_figure_with_no_results_is_unchecked_not_ungrounded)
    check("it searches every retained result",
          test_it_searches_every_retained_result_not_just_the_last)
    check("check_figure never raises on junk", test_check_figure_never_raises_on_junk)
    # --- table grounding ---
    check("parse_markdown_tables extracts header and body rows",
          test_parse_markdown_tables_extracts_header_and_body_rows)
    check("parse_markdown_tables skips a chart fence",
          test_parse_markdown_tables_skips_a_chart_fence)
    check("parse_markdown_tables ignores a bare horizontal rule",
          test_parse_markdown_tables_ignores_a_bare_horizontal_rule)
    check("a verbatim table is matched (measure column only)",
          test_a_verbatim_table_is_matched)
    check("a dropped-digit cell is partial", test_a_dropped_digit_cell_is_partial)
    check("a rank ordinal column is excluded from grading",
          test_a_rank_ordinal_column_is_excluded_from_grading)
    check("an unlabeled rank ordinal is excluded by its 1..N values",
          test_an_unlabeled_rank_ordinal_is_excluded_by_its_1_to_n_values)
    check("a wholly invented table is unmatched",
          test_a_wholly_invented_table_is_unmatched)
    check("a display-rounded cell still matches",
          test_a_display_rounded_cell_still_matches)
    check("a legitimately computed column grounds (full-reproduction rule)",
          test_a_legitimately_computed_column_grounds_not_false_alarms)
    check("a measure cell matching only a dimension column is unmatched (anomaly 1)",
          test_a_measure_cell_matching_only_a_dimension_column_is_unmatched)
    check("a real small count still grounds via the measure column",
          test_a_real_small_count_still_grounds_via_the_measure_column)
    check("a figure that IS a dimension value still grounds exact (figure path)",
          test_a_figure_that_is_a_dimension_value_still_grounds_exact)
    check("prose with no table is no_table", test_prose_with_no_table_is_no_table)
    check("a table with no results is unchecked with zero counts",
          test_a_table_with_no_results_is_unchecked_with_zero_counts)
    check("a label-only table is no_table", test_a_label_only_table_is_no_table)
    check("check_table never raises on junk", test_check_table_never_raises_on_junk)
    check("a row-wise total grounds (the observed false ungrounded)", test_a_row_total_grounds)
    check("the year column is never added into a row total",
          test_the_year_column_is_not_added_into_the_total)
    check("a single measure column has no row total", test_a_single_measure_column_has_no_row_total)
    check("a verbatim cell still beats a row total", test_a_verbatim_cell_still_beats_a_row_total)
    check("an UNANCHORED table cell still does not use row totals",
          test_an_UNANCHORED_table_cell_still_does_not_use_row_totals)
    print("  -- row-anchored table grounding (was the KNOWN BLIND SPOT) --")
    check("a CORRECT row-wise %-change column grounds",
          test_a_correct_row_wise_pct_change_column_grounds)
    check("a CORRECT %-change column ALONE grounds (anchors on its label)",
          test_a_correct_pct_change_column_ALONE_grounds)
    check("a CORRECT share-of-total column still reproduces",
          test_a_correct_share_of_total_column_still_reproduces)
    check("a fabricated table is still caught",
          test_a_fabricated_table_is_still_caught)
    check("a value from ANOTHER row does not ground (precision regression)",
          test_a_value_from_ANOTHER_row_does_not_ground)
    check("a PIVOTED row grounds against the group it describes (live ⚠)",
          test_a_pivoted_table_row_grounds_against_the_group_it_describes)
    check("grouping still refuses another row's value (precision bound)",
          test_grouping_still_refuses_another_rows_value)
    check("adjacent years do not tie the anchor (live false negative)",
          test_adjacent_years_do_not_tie_the_anchor)
    check("a repeated value does not evict an entity from the anchor group",
          test_a_repeated_value_does_not_evict_an_entity_from_the_anchor_group)
    check("a merged table anchors in every result it draws from",
          test_a_merged_table_anchors_in_every_result_it_draws_from)
    check("an unanchorable summary row falls back to the column search",
          test_an_unanchorable_summary_row_falls_back_to_the_column_search)
    check("a row-total column grounds for tables now",
          test_a_row_total_column_grounds_for_tables_now)
    check("a wrong row total is still caught",
          test_a_wrong_row_total_is_still_caught)
    check("a year-over-year change column grounds (second blind spot)",
          test_a_year_over_year_change_column_grounds)
    check("a WRONG year-over-year change is still caught",
          test_a_WRONG_year_over_year_change_is_still_caught)
    check("fabricated numbers are rejected at scale (aggregate bound, both directions)",
          test_fabricated_numbers_are_rejected_at_scale)
    print("  -- cell formats found in LIVE answers (each was a false caution) --")
    check("an emphasized cell is graded, not silently skipped",
          test_an_emphasized_cell_is_graded_not_skipped)
    check("an emphasized WRONG cell is still caught",
          test_an_emphasized_WRONG_cell_is_still_caught)
    check("a hedged cell is checked as a BOUND",
          test_a_hedged_cell_is_checked_as_a_BOUND)
    check("a hedge nothing satisfies is still caught",
          test_a_hedge_no_value_satisfies_is_still_caught)
    check("a '>' hedge reads the other way",
          test_a_greater_than_hedge_reads_the_other_way)
    check("a human-written dimension header is not graded",
          test_a_human_written_dimension_header_is_not_graded)
    check("a measure header ENDING in 'Year' is still graded",
          test_a_measure_header_that_ENDS_in_year_is_still_graded)
    print("  -- cross-result derivations (rows from one query, total from another) --")
    check("a cross-result share reproduces",
          test_a_cross_result_share_reproduces)
    check("a complement still grounds on a result-rich turn",
          test_a_complement_still_grounds_on_a_result_rich_turn)
    check("the complement set stays bounded on a rich turn",
          test_the_complement_set_stays_bounded_on_a_rich_turn)
    check("a cross-result SHARE needs the percent marker (precision guard)",
          test_a_cross_result_SHARE_needs_the_percent_marker)
    check("a cross-result share over 100% is refused",
          test_a_cross_result_share_over_100_percent_is_refused)
    print("  -- truncated results: refuse column-extent aggregates, allow row-scoped ones --")
    check("a column SUM over a truncated result does not ground (headline regression)",
          test_a_column_sum_over_a_truncated_result_does_not_ground)
    check("a truncated mean/max/min/share all refuse (max/min ground only via verbatim)",
          test_a_truncated_mean_max_min_and_share_all_refuse)
    check("a column pct_change over a truncated result does not ground",
          test_a_column_pct_change_over_a_truncated_result_does_not_ground)
    check("a truncated result does not supply a cross-result total",
          test_a_truncated_result_does_not_supply_a_cross_result_total)
    check("a table column total over a truncated result is not matched",
          test_a_table_column_total_over_a_truncated_result_is_not_matched)
    check("a share of a truncated column total is not matched",
          test_a_share_of_a_truncated_column_total_is_not_matched)
    check("a verbatim cell in a truncated result still grounds",
          test_a_verbatim_cell_in_a_truncated_result_still_grounds)
    check("an untruncated result still grounds every aggregate op (anti-vacuity)",
          test_an_untruncated_result_still_grounds_every_aggregate_op)
    check("a row total still grounds in a truncated result",
          test_a_row_total_still_grounds_in_a_truncated_result)
    check("a row-wise pct_change column still grounds in a truncated result",
          test_a_row_wise_pct_change_column_still_grounds_in_a_truncated_result)
    check("a prior-row change column still grounds in a truncated result",
          test_a_prior_row_change_column_still_grounds_in_a_truncated_result)
    check("a cross-result share still grounds when only the numerator result "
          "is truncated (table cross-result route, NOT the whole_column gate "
          "-- see corrected docstring)",
          test_a_cross_result_share_still_grounds_when_only_the_numerator_result_is_truncated)
    check("an untruncated result still grounds its own column sum when a "
          "SIBLING is truncated (the actual most-important recall test for "
          "the whole_column gate)",
          test_an_untruncated_result_still_grounds_its_own_column_sum_when_a_SIBLING_is_truncated)
    print("  -- truncation telemetry: blocked_by_truncation / cells_blocked --")
    check("blocked_by_truncation is False for a figure nothing could reproduce",
          test_blocked_by_truncation_is_false_for_a_figure_nothing_could_reproduce)
    check("blocked_by_truncation is True only when the gate is what refused",
          test_blocked_by_truncation_is_true_only_when_the_gate_is_what_refused)
    check("check_table counts cells only a truncated aggregate would have matched",
          test_check_table_counts_cells_only_a_truncated_aggregate_would_have_matched)
    check("cells_blocked is zero when no result was truncated",
          test_cells_blocked_is_zero_when_no_result_was_truncated)
    print("  -- row-wise ratio of two measure columns --")
    check("a per-row ratio column grounds every cell",
          test_a_per_row_ratio_column_grounds_every_cell)
    check("a per-row ratio figure does NOT ground (documented limit)",
          test_a_per_row_ratio_figure_does_NOT_ground)
    check("a fabricated figure percentage does not ground against a wide result",
          test_a_fabricated_figure_percentage_does_not_ground_against_a_wide_result)
    check("the row ratio beats the coincidental column share",
          test_the_row_ratio_beats_the_coincidental_column_share)
    check("a wrong share still fails", test_a_wrong_share_still_fails)
    check("a ratio not written as a percent does not ground",
          test_a_ratio_not_written_as_a_percent_does_not_ground)
    check("an out-of-range ratio does not ground",
          test_an_out_of_range_ratio_does_not_ground)
    print("  -- truncated-result persistence (to_storage/from_storage) --")
    check("to_storage preserves the truncated flag",
          test_to_storage_preserves_the_truncated_flag)
    check("to_storage flags a result its own row cap cut",
          test_to_storage_flags_a_result_its_own_row_cap_cut)
    check("from_storage defaults truncated=False for a legacy blob",
          test_from_storage_defaults_truncated_false_for_a_legacy_blob)
    check("a borrowed prior result refuses an aggregate after a round trip",
          test_a_borrowed_prior_result_refuses_an_aggregate_after_a_round_trip)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} grounding test(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL GROUNDING TESTS PASSED")


if __name__ == "__main__":
    run()
