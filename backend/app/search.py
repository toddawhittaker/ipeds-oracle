"""Turning a person's typed search box into SQL LIKE patterns.

Used by the chat sidebar's conversation search (app/routers/chat.py). The rules
are the ones every search box a user has already met follows:

  * space-separated words are ANDed — `hello world` finds text containing both;
  * a double-quoted run is ONE term — `"hello world"` matches that phrase, and
    does not match a document that says "hello" in one place and "world" in
    another;
  * everything is a literal substring. There are no operators, no wildcards, and
    no stemming. A search for `50%` looks for the characters `50%`.

Deliberately not FTS5: a user's whole history is a few hundred message rows, so
a LIKE scan is cheaper than a second table, a sync trigger, and a migration.
"""
from __future__ import annotations

# The most terms one query may contribute. Each term becomes its own AND'd
# clause with a correlated EXISTS inside it, so an unbounded term count is an
# unbounded amount of SQL generated from one text field.
#
# Terms past the cap are DROPPED, which makes the result a superset of what was
# asked for — the one direction that returns a wrong answer rather than a
# missing one. So the cap is set past any plausible query instead of snugly
# above the typical one: pasting a half-remembered question ("how many nursing
# degrees were awarded in Ohio in 2023 by private institutions" is 13 words)
# has to keep every word, or the search quietly answers a different question.
# Each term is length-capped below, so 24 short clauses cost less than the one
# 1800-character term that used to be allowed.
MAX_TERMS = 24

# The longest a single term may be. LIKE '%x%' costs O(len(content) x len(term))
# when the text keeps partially matching, and a user controls both sides: their
# own messages are the content. Measured against 3.2 MB of deliberately
# repetitive self-authored history, one non-matching term costs 0.05s at 10
# characters, 1.2s at 500 and 3.6s at 1800; this route is a sync def holding one
# of the 40 shared threadpool slots that sign-in and every admin handler also
# use, and it has no rate limit of its own. 100 characters holds the worst case
# to ~0.3s. Ordinary prose never reaches it — matching stops at the first
# mismatched character, so realistic text measures 0.002s at any term length.
MAX_TERM_LEN = 100


def parse_terms(q: str | None) -> list[str]:
    """The search terms in `q`, in order, quoted runs kept whole.

    An UNCLOSED quote runs to the end of the string rather than being dropped or
    raising: someone half-way through typing `"nursing compl` is mid-thought,
    and the useful behaviour is to search for what they have typed so far. That
    also means this function has no failure mode — every string is a valid
    query, so no caller needs to handle a parse error.
    """
    if not q:
        return []
    # Control characters are stripped, NUL above all: a NUL inside a LIKE
    # pattern truncates it at the C level, so `%\x00%` reaches SQLite as `%` and
    # matches every row — silently undoing the escaping below. Harmless today
    # (the caller's own conversations are all it could over-match, and the
    # tenancy scope is a separate bound predicate) but it is the exact failure
    # like_pattern exists to prevent, and it would become real the moment this
    # helper is reused somewhere the scope is inside the pattern.
    # Whitespace is kept (it is what separates terms); NUL and every other
    # control character is not — isprintable() is False for both, and isspace()
    # is False for NUL.
    q = "".join(ch for ch in q if ch.isprintable() or ch.isspace())
    terms: list[str] = []
    buf: list[str] = []
    quoted = False
    for ch in q:
        if ch == '"':
            # A quote always ends the current run: closing one ends the phrase,
            # and opening one ends whatever bare word ran into it.
            if buf:
                terms.append("".join(buf))
                buf = []
            quoted = not quoted
        elif ch.isspace() and not quoted:
            if buf:
                terms.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        terms.append("".join(buf))
    return [t[:MAX_TERM_LEN] for t in terms if t][:MAX_TERMS]


def like_pattern(term: str) -> str:
    """`term` as a contains-pattern for `LIKE ? ESCAPE '\\'`.

    The escaping is the whole point: `%` and `_` are LIKE's own wildcards, so an
    unescaped search for `%` matches every row in the table and reads to the
    user as "search is broken". The backslash goes first, or escaping the other
    two would then escape their escapes.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
