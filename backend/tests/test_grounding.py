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


def test_a_label_only_table_is_no_table():
    # No numeric cells to grade (an address/accreditor lookup rendered as a table).
    md = "| Field | Value |\n| --- | --- |\n| City | Columbus |\n| State | Ohio |\n"
    got = grounding.check_table(md, [YEARS])
    assert got.status == grounding.NO_TABLE, got


def test_check_table_never_raises_on_junk():
    for text in [None, "", "| broken", "|||", "```\nunclosed fence\n"]:
        grounding.check_table(text, [YEARS])
    grounding.check_table(_TABLE_MD, None)


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
    check("prose with no table is no_table", test_prose_with_no_table_is_no_table)
    check("a table with no results is unchecked with zero counts",
          test_a_table_with_no_results_is_unchecked_with_zero_counts)
    check("a label-only table is no_table", test_a_label_only_table_is_no_table)
    check("check_table never raises on junk", test_check_table_never_raises_on_junk)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} grounding test(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL GROUNDING TESTS PASSED")


if __name__ == "__main__":
    run()
