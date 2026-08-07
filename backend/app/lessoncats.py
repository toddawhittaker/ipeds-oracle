"""The closed set of categories the post-answer critic (app/critic.py) sorts a
REVISE finding into, and the sole authority app/routers/chat.py consults before
recording an unverified lesson.

Dependency-free leaf module (imports nothing from the rest of the app, same
precedent as app/seeds.py): app/critic.py, app/llm.py, and app/routers/chat.py
all need this enum, and letting app/skills.py reach it through app/critic.py
would drag httpx (critic.py's transport) into the skills import graph for what
is otherwise a constant.

Exists because embedding similarity provably cannot separate a legitimate
aggregation-rule lesson from the rejected "verify figures against the query
result before emitting them" class (measured: within-class cosine 0.625-0.802,
two genuinely different legitimate lessons 0.673 -- no separating threshold
exists). Retrieval similarity can't decide this, so the gate has to be a
closed enum, checked by exact membership, never a score.

UNGROUNDED_NUMBER and OTHER are deliberately NOT in LEARNABLE:
- UNGROUNDED_NUMBER is already enforced deterministically, per-turn, by
  app/grounding.py -- a retrieved lesson at query time can't fix a grounding
  failure the same turn already checks. Storing it just repeats, forever, the
  exact "verify figures" class this module exists to stop.
- OTHER must stay unlearnable too, or it becomes the escape hatch: a model
  that has learned its UNGROUNDED_NUMBER findings are discarded could simply
  relabel the same finding OTHER and keep getting through. Closing off both
  is what makes the gate a real fence instead of a one-hop detour.

Both categories still force the critic's revision round like any other (see
app/critic.py / app/llm.py) -- only the LEARNING step is gated. Losing that
revision would remove the one thing app/grounding.py cannot do on its own:
make the model re-query and fix the number before the user sees it.
"""
from __future__ import annotations

# Order matters only for readability (BULLETS/critic._SYSTEM follow it); the
# gate itself is exact-membership, not positional.
CATEGORIES: tuple[str, ...] = (
    "CIP_ROLLUP",
    "SECOND_MAJOR",
    "AWARD_LEVEL",
    "MAGNITUDE",
    "QUESTION_MISMATCH",
    "UNGROUNDED_NUMBER",
    "OTHER",
)

LEARNABLE: frozenset[str] = frozenset({
    "CIP_ROLLUP", "SECOND_MAJOR", "AWARD_LEVEL", "MAGNITUDE", "QUESTION_MISMATCH",
})

LABELS: dict[str, str] = {
    "CIP_ROLLUP": "CIP rollup double-count",
    "SECOND_MAJOR": "Second-major double-count",
    "AWARD_LEVEL": "Award-level mixing",
    "MAGNITUDE": "Implausible magnitude",
    "QUESTION_MISMATCH": "Answer doesn't match the question",
    "UNGROUNDED_NUMBER": "Number not in the data",
    "OTHER": "Other",
}

# (token, prose) — critic._SYSTEM is ASSEMBLED from this, one bullet per
# category tagged "[TOKEN]", so the prompt and this enum cannot drift apart
# the way a hand-duplicated bullet list eventually does.
BULLETS: list[tuple[str, str]] = [
    ("CIP_ROLLUP",
     "CIP rollup double counting: in the completions table c_a, cipcode exists "
     "at 2-/4-/6-digit levels PLUS a '99' grand-total row that each sum to the "
     "same total, so `cipcode LIKE '51.%'` or a SUM with no CIP filter and no "
     "GROUP BY cipcode overcounts (~4x)."),
    ("SECOND_MAJOR",
     "Second-major double counting: summing c_a without majornum=1 counts "
     "double-majors twice."),
    ("AWARD_LEVEL",
     "Award-level mixing: awlevel rollup codes summed together with real "
     "levels."),
    ("MAGNITUDE",
     "Implausible magnitude: the U.S. awards roughly 1M associate's, 2M "
     "bachelor's, 0.85M master's degrees per year across ALL programs; a "
     "single program's national total in the millions, or one institution "
     "awarding tens of thousands of a single degree, is suspect."),
    ("QUESTION_MISMATCH",
     "Wrong answer to the question: wrong CIP/award code, wrong year, wrong "
     "state/control filter, or an answer that doesn't match what was asked."),
    ("UNGROUNDED_NUMBER",
     "A number that isn't in the data: you are given the actual RESULT ROWS "
     "the query returned. Check that the figures quoted in the answer are "
     "present in those rows, or correctly derived from them (a sum, an "
     "average, a percentage change, a share of the total). A headline number "
     "that appears nowhere in the rows and follows from no such derivation is "
     "an error, even if it looks plausible. When the rows are marked "
     "truncated, treat any claimed TOTAL over them as suspect."),
    ("OTHER",
     "Some other likely substantive data or aggregation mistake that doesn't "
     "fit any category above -- use this only when none of them apply."),
]


def is_learnable(token: object) -> bool:
    """Fail-closed membership test: anything that isn't an exact, recognized,
    LEARNABLE token reads as False -- a blank/None/invented/mis-cased label,
    or a value of the wrong type entirely (this is fed model-controlled text,
    so it must never raise). The isinstance check alone is what keeps an
    unhashable value (a list/dict) from ever reaching the frozenset `in`
    check, where it would raise TypeError."""
    return isinstance(token, str) and token in LEARNABLE
