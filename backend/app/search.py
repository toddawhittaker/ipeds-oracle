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
# unbounded amount of SQL generated from one text field. Eight is far past any
# real search (three words is a lot) and far below anything that costs.
MAX_TERMS = 8


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
    return [t for t in terms if t][:MAX_TERMS]


def like_pattern(term: str) -> str:
    """`term` as a contains-pattern for `LIKE ? ESCAPE '\\'`.

    The escaping is the whole point: `%` and `_` are LIKE's own wildcards, so an
    unescaped search for `%` matches every row in the table and reads to the
    user as "search is broken". The backslash goes first, or escaping the other
    two would then escape their escapes.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
