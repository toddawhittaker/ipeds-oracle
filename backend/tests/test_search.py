"""Search-query parsing (backend/app/search.py).

Pure input->output, so it is tested here rather than through the endpoint: the
SQL these terms end up in is exercised in test_chat_router.py, but the RULES
("hello world" is two terms, `"hello world"` is one) are what a user is actually
promised, and they should fail here — one small file — rather than three layers
up.

Each check names a regression with a plausible way of happening:

  * a quoted phrase splitting into words, which silently turns an exact-phrase
    search into an AND of its words and returns far too much;
  * the term cap disappearing, so one text field can generate unbounded SQL;
  * LIKE wildcards going unescaped, where searching `%` matches every row and
    reads to the user as a broken search box.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.search import MAX_TERM_LEN, MAX_TERMS, like_pattern, parse_terms  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def run():
    def words_are_separate_terms():
        assert parse_terms("hello world") == ["hello", "world"]
        # Runs of whitespace, and leading/trailing space, produce no empty terms
        # — an empty term becomes the pattern '%%', which matches everything.
        assert parse_terms("  hello   world  ") == ["hello", "world"]
        assert parse_terms("hello\tworld\n") == ["hello", "world"]
    check("space-separated words become separate terms",
          words_are_separate_terms)

    def a_quoted_run_is_one_term():
        assert parse_terms('"hello world"') == ["hello world"]
        # The distinction the whole quoting rule exists for: unquoted is two
        # terms that may match far apart, quoted is one string.
        assert parse_terms('"hello world"') != parse_terms("hello world")
    check("a quoted run stays one term", a_quoted_run_is_one_term)

    def quoted_and_bare_mix():
        assert parse_terms('nursing "award level" 2023') == \
            ["nursing", "award level", "2023"]
        # A quote butted against a word ends that word rather than swallowing it.
        assert parse_terms('foo"bar baz"') == ["foo", "bar baz"]
    check("quoted and bare terms mix in one query", quoted_and_bare_mix)

    def an_unclosed_quote_runs_to_the_end():
        # Someone mid-type. The useful answer is to search what they have typed,
        # not to drop it or refuse — and it means no caller handles a parse error.
        assert parse_terms('"nursing compl') == ["nursing compl"]
        assert parse_terms('one "two three') == ["one", "two three"]
    check("an unclosed quote searches to the end of the string",
          an_unclosed_quote_runs_to_the_end)

    def empty_queries_have_no_terms():
        # No terms means the endpoint runs its unfiltered query, so these three
        # must never produce a term that filters everything away.
        for q in (None, "", "   ", '""', '  ""  '):
            assert parse_terms(q) == [], f"{q!r} produced terms"
    check("an empty or all-whitespace query has no terms",
          empty_queries_have_no_terms)

    def the_term_count_is_capped():
        many = " ".join(f"w{i}" for i in range(MAX_TERMS + 5))
        got = parse_terms(many)
        assert len(got) == MAX_TERMS, len(got)
        # The FIRST terms are kept, so a long query still searches what the user
        # typed first rather than an arbitrary tail.
        assert got[0] == "w0" and got[-1] == f"w{MAX_TERMS - 1}", got
    check("the term count is capped, keeping the earliest terms",
          the_term_count_is_capped)

    def a_pasted_question_keeps_every_word():
        # Dropping a term WIDENS the result — it answers a question the user did
        # not ask, and looks like a wrong hit rather than a missing one. The cap
        # therefore has to sit past a pasted half-remembered question, not just
        # past a typical two-word search.
        pasted = ("how many nursing degrees were awarded in Ohio in 2023 "
                  "by private institutions")
        got = parse_terms(pasted)
        assert len(got) == len(pasted.split()), \
            f"a {len(pasted.split())}-word paste lost terms: {got}"
        assert "2023" in got and "institutions" in got, got
    check("a pasted question keeps every word rather than silently widening",
          a_pasted_question_keeps_every_word)

    def a_single_term_is_length_capped():
        # LIKE '%x%' costs O(len(content) x len(term)) on text that keeps
        # partially matching, and the caller controls both sides — their own
        # messages are the content. Uncapped, one request measured 3.6s of CPU
        # on a route that holds a shared threadpool slot and has no rate limit.
        got = parse_terms("y" * (MAX_TERM_LEN + 500))
        assert got == ["y" * MAX_TERM_LEN], len(got[0])
        # The cap is per TERM, so it cannot be dodged by quoting.
        quoted = parse_terms('"' + "z" * (MAX_TERM_LEN + 500) + '"')
        assert quoted == ["z" * MAX_TERM_LEN], len(quoted[0])
    check("one term cannot be longer than MAX_TERM_LEN",
          a_single_term_is_length_capped)

    def control_characters_are_stripped():
        # A NUL truncates a LIKE pattern at the C level, so '%\x00%' reaches
        # SQLite as '%' and matches EVERY row — the exact failure like_pattern
        # exists to prevent, arriving by a route that bypasses its escaping.
        assert parse_terms("\x00") == [], "a NUL survived as a term"
        # Real text either side of one is preserved, minus the control char.
        assert parse_terms("a\x00b c") == ["ab", "c"]
        # Whitespace is NOT a control character here — it separates terms.
        assert parse_terms("a\tb\nc") == ["a", "b", "c"]
    check("control characters are stripped, whitespace is not",
          control_characters_are_stripped)

    def wildcards_are_escaped():
        # Unescaped, each of these is a LIKE metacharacter: '%' matches any run
        # and '_' any single character, so both would match rows that do not
        # contain the typed text at all.
        assert like_pattern("50%") == "%50\\%%"
        assert like_pattern("a_b") == "%a\\_b%"
        # The backslash is escaped FIRST, or escaping the other two would then
        # have their escapes escaped.
        assert like_pattern("c:\\x") == "%c:\\\\x%"
        assert like_pattern("plain") == "%plain%"
    check("LIKE wildcards in a term are escaped to literals",
          wildcards_are_escaped)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL SEARCH-PARSING TESTS PASSED")


if __name__ == "__main__":
    run()
