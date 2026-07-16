"""System-prompt construction (app/prompt.py) — the per-deployment "which
collection years are actually loaded" fact.

Background / regression this guards against: which years are installed is a
per-deployment fact (every institution picks its own years via Admin ->
Imports), but it used to be hardcoded in THREE places (INSTRUCTIONS,
SCHEMA.md x2) all claiming "2020-21 ... 2024-25" while a real deployment could
hold a different set. `_years_fact()` replaces the hardcoded claim with a
live read of `ipeds_years()` (app/tools/sql.py's non-raising probe), inserted
into `build_system_prompt()` as a `DATASET (this deployment)` section ahead of
the (still-generic) SCHEMA GUIDE. Crucially `_years_fact()` is NOT cached: an
admin integrating/removing a year changes the installed set under a running
process, and a confidently stale range is exactly the bug being fixed.

Runs with no ipeds.db / app.db needed -- `ipeds_years` is monkeypatched on
`app.prompt` (a direct `from ... import ipeds_years`, so the patch target is
the name inside app.prompt, not app.tools.sql) for every case that cares about
its return value, so nothing here depends on a real or fixture database.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["IPEDS_DB_PATH"] = str(Path(tmp) / "ipeds.db")
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["COOKIE_SECURE"] = "false"

from app import prompt  # noqa: E402
from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _with_years(years, fn):
    """Run fn() with app.prompt.ipeds_years patched to return `years`."""
    orig = prompt.ipeds_years
    prompt.ipeds_years = lambda: years
    try:
        return fn()
    finally:
        prompt.ipeds_years = orig


# --- 1. _years_fact() names the actual installed years -----------------------

def test_years_fact_names_actual_years():
    def run():
        fact = prompt._years_fact()
        assert "3" in fact, f"expected the count (3) to appear, got: {fact!r}"
        for y in (2020, 2021, 2022):
            assert str(y) in fact, f"expected year {y} to be named, got: {fact!r}"
        assert "2022" in fact, "expected the most-recent year (2022) to be named"
        return fact
    _with_years([2020, 2021, 2022], run)


# --- 2. No-data path: build_system_prompt() must still succeed ---------------

def test_years_fact_no_data_wording():
    def run():
        fact = prompt._years_fact()
        assert "no dataset" in fact.lower() or "no collection years" in fact.lower(), (
            f"expected a 'no dataset loaded' style message for an empty probe, got: {fact!r}"
        )
    _with_years([], run)


def test_build_system_prompt_succeeds_with_no_data():
    def run():
        # Must not raise even though ipeds_years() returned [] -- this composes
        # with the fresh-deploy / no-data-yet onboarding path.
        out = prompt.build_system_prompt()
        assert isinstance(out, str) and out, "build_system_prompt() returned empty/non-string"
    _with_years([], run)


# --- 3. THE REGRESSION THIS EXISTS TO PREVENT: no hardcoded year range -------

def test_no_hardcoded_year_range_in_assembled_prompt():
    def run():
        out = prompt.build_system_prompt()
        assert "2020-21 … 2024-25" not in out, (
            "found the old hardcoded year-range claim '2020-21 … 2024-25' in "
            "build_system_prompt() output -- this is the exact regression the "
            "dynamic-year-facts change fixes; it must not creep back into "
            "INSTRUCTIONS or SCHEMA.md"
        )
        assert "2020-21→2021" not in out, (
            "found the old hardcoded '2020-21→2021' convention example with a "
            "baked-in year in build_system_prompt() output -- SCHEMA.md's "
            "ending-year convention must be illustrated generically, not tied to "
            "a specific installed year"
        )
    # Try it under a couple of different installed-year scenarios, since the
    # regression is about a STATIC claim in the prompt text, not about what
    # ipeds_years() itself returns.
    _with_years([2020, 2021, 2022, 2023, 2024, 2025], run)
    _with_years([2022], run)
    _with_years([], run)


# --- 4. _years_fact() must NOT be cached --------------------------------------

def test_years_fact_is_not_cached():
    first = _with_years([2020, 2021, 2022], prompt._years_fact)
    assert "2022" in first and "2026" not in first, (
        f"sanity check on the first call failed, got: {first!r}"
    )
    second = _with_years([2024, 2025, 2026], prompt._years_fact)
    assert "2026" in second, (
        "_years_fact() did not reflect a changed installed-years set on a second "
        "call -- if this fails, someone added caching (e.g. @lru_cache) to "
        "_years_fact(). It must stay uncached: an admin integrating/removing a "
        f"year via Admin -> Imports changes the installed set at runtime, and a "
        f"stale cached range is exactly the bug this function exists to fix. "
        f"first={first!r} second={second!r}"
    )
    assert "2020" not in second, (
        f"_years_fact() leaked stale years from the first call into the second: {second!r}"
    )


# --- 5. build_system_prompt() includes the years section ahead of the guide --

def test_build_system_prompt_includes_years_section_before_schema_guide():
    def run():
        out = prompt.build_system_prompt()
        dataset_idx = out.find("DATASET (this deployment)")
        schema_idx = out.find("SCHEMA GUIDE (authoritative)")
        assert dataset_idx != -1, "DATASET section header missing from build_system_prompt()"
        assert schema_idx != -1, "SCHEMA GUIDE section header missing from build_system_prompt()"
        assert dataset_idx < schema_idx, (
            "DATASET (this deployment) section must come before SCHEMA GUIDE"
        )
        years_fact = prompt._years_fact()
        assert years_fact in out, "the years-fact text itself is not present verbatim in the prompt"
        fact_idx = out.find(years_fact)
        assert dataset_idx < fact_idx < schema_idx, (
            "years-fact text is not positioned between the DATASET header "
            "and the SCHEMA GUIDE header"
        )
    _with_years([2021, 2022, 2023], run)


# --- 6. SCHEMA.md still carries real IPEDS-history facts ---------------------

def test_schema_md_still_has_ipeds_history_facts():
    text = prompt._schema_md()
    assert "(SCHEMA.md not found)" != text, "SCHEMA.md failed to load"
    low = text.lower()
    # These are facts about IPEDS ITSELF (when a survey started/changed), not
    # about which years a deployment happens to have loaded -- they must
    # survive any future generic-ising of the deployment-coverage claims.
    assert "sfa" in low and "2024-25" in low, (
        "expected the sfa (student financial aid) 2024-25-merge note to still be "
        "in SCHEMA.md -- this is a real IPEDS-history fact, not a "
        "deployment-coverage claim, and must not be stripped"
    )
    assert "2021-2024" in text or "2021" in low, (
        "expected the sfa_p1/sfa_p2 pre-merge year note to still be present"
    )


def run():
    check("_years_fact() names the actual installed years/count/most-recent",
          test_years_fact_names_actual_years)
    check("_years_fact() gives a 'no dataset loaded' message on an empty probe",
          test_years_fact_no_data_wording)
    check("build_system_prompt() succeeds (does not raise) with no data installed",
          test_build_system_prompt_succeeds_with_no_data)
    check("build_system_prompt() contains no hardcoded year range (regression guard)",
          test_no_hardcoded_year_range_in_assembled_prompt)
    check("_years_fact() is NOT cached -- reflects a changed installed-years set",
          test_years_fact_is_not_cached)
    check("build_system_prompt() places the DATASET section ahead of the SCHEMA GUIDE",
          test_build_system_prompt_includes_years_section_before_schema_guide)
    check("SCHEMA.md still documents real IPEDS-history facts (e.g. sfa 2024-25 merge)",
          test_schema_md_still_has_ipeds_history_facts)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} prompt test(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL PROMPT TESTS PASSED")


if __name__ == "__main__":
    run()
