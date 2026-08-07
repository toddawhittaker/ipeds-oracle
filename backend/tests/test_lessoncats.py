"""Lesson category gate (backend/app/lessoncats.py): the CLOSED, categorical set
the post-answer critic (app/critic.py) classifies every REVISE finding into, and
the sole authority app/routers/chat.py consults before recording an unverified
lesson.

Exists because embedding similarity provably cannot separate a legitimate
aggregation-rule lesson from the rejected "verify figures against the query
result before emitting them" class (measured: within-class cosine 0.625-0.802,
two genuinely different legitimate lessons 0.673 -- no separating threshold
exists). Retrieval similarity can't decide this, so the gate has to be a closed
enum, checked by membership, not a score.

UNGROUNDED_NUMBER and OTHER are deliberately NOT learnable: the former is
already enforced deterministically by app/grounding.py (a retrieved lesson can't
fix a per-turn grounding failure), and OTHER must stay unlearnable or it becomes
the escape hatch -- a model that learns its UNGROUNDED_NUMBER findings are
discarded could just relabel as OTHER and keep getting through.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import lessoncats  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


_EXPECTED = (
    "CIP_ROLLUP", "SECOND_MAJOR", "AWARD_LEVEL", "MAGNITUDE",
    "QUESTION_MISMATCH", "UNGROUNDED_NUMBER", "OTHER",
)
_EXPECTED_LEARNABLE = {
    "CIP_ROLLUP", "SECOND_MAJOR", "AWARD_LEVEL", "MAGNITUDE", "QUESTION_MISMATCH",
}


def test_categories_is_exactly_the_seven_tokens():
    """The closed set, in the order the PR spec derives it from critic._SYSTEM's
    six bullets plus the OTHER fallback. A missing/renamed/extra token here
    desyncs every other module (critic.py's prompt, chat.py's gate) that treats
    this tuple as authoritative."""
    assert tuple(lessoncats.CATEGORIES) == _EXPECTED, lessoncats.CATEGORIES


def test_learnable_is_exactly_the_five_aggregation_categories():
    """THE gate's whole point: UNGROUNDED_NUMBER (already handled
    deterministically by app/grounding.py) and OTHER (the escape-hatch relabel
    target) must never be in LEARNABLE, whatever LEARNABLE is built from."""
    assert frozenset(lessoncats.LEARNABLE) == _EXPECTED_LEARNABLE, lessoncats.LEARNABLE


def test_ungrounded_number_is_not_learnable():
    """THE rejected class this PR exists to stop: a critic finding categorized
    UNGROUNDED_NUMBER must never become a stored lesson -- app/grounding.py
    already enforces it deterministically, and a retrieved lesson can't fix a
    per-turn grounding failure."""
    assert lessoncats.is_learnable("UNGROUNDED_NUMBER") is False


def test_other_is_not_learnable():
    """THE escape hatch: if OTHER were learnable, a model that learns
    UNGROUNDED_NUMBER findings are discarded could just relabel as OTHER and
    keep getting through -- OTHER must be closed off exactly like
    UNGROUNDED_NUMBER, not treated as a harmless miscellaneous bucket."""
    assert lessoncats.is_learnable("OTHER") is False


def test_every_learnable_category_reports_learnable():
    for token in sorted(_EXPECTED_LEARNABLE):
        assert lessoncats.is_learnable(token) is True, token


def test_is_learnable_fails_closed_on_unrecognized_or_missing_input():
    """Fail-closed contract: anything that isn't an exact, recognized token --
    blank, None, an invented label, or a mis-cased/padded real one -- must read
    as NOT learnable. A regression here would let something other than a real
    category slip a lesson through the gate."""
    for bad in ("", None, "NOT_A_REAL_CATEGORY", "cip_rollup", "  CIP_ROLLUP  "):
        assert lessoncats.is_learnable(bad) is False, bad


def test_is_learnable_never_raises_on_unhashable_input():
    """A frozenset membership test on an unhashable value (a list/dict) raises
    TypeError unless is_learnable guards its input type first -- and this
    function is fed a value derived from model-controlled text, so it must
    never crash the turn it's gating."""
    for bad in ([], {}, {"category": "CIP_ROLLUP"}, 12345, object()):
        try:
            result = lessoncats.is_learnable(bad)
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"is_learnable raised on {bad!r}: {e}") from e
        assert result is False, (bad, result)


def test_labels_covers_every_category_with_a_nonempty_string():
    """The admin UI is expected to render LABELS[token]; a token with no entry
    (or a blank one) would show as undefined/blank instead of a readable
    category name."""
    for token in lessoncats.CATEGORIES:
        label = lessoncats.LABELS.get(token)
        assert isinstance(label, str) and label.strip(), (token, label)


def test_bullets_covers_every_category_exactly_once_with_nonempty_prose():
    """critic._SYSTEM is meant to be ASSEMBLED from lessoncats.BULLETS (per the
    PR spec), so every category needs exactly one bullet with real prose, or
    the reviewer's instructions silently omit -- or duplicate -- a category the
    gate still knows about."""
    tokens = [t for t, _prose in lessoncats.BULLETS]
    assert sorted(tokens) == sorted(lessoncats.CATEGORIES), tokens
    assert len(tokens) == len(set(tokens)), "a category must not repeat in BULLETS"
    for token, prose in lessoncats.BULLETS:
        assert isinstance(prose, str) and prose.strip(), (token, prose)


def run():
    print("lesson category gate (app/lessoncats.py):")
    check("CATEGORIES is exactly the seven tokens", test_categories_is_exactly_the_seven_tokens)
    check("LEARNABLE is exactly the five aggregation categories",
          test_learnable_is_exactly_the_five_aggregation_categories)
    check("UNGROUNDED_NUMBER is not learnable", test_ungrounded_number_is_not_learnable)
    check("OTHER is not learnable (the escape-hatch guard)", test_other_is_not_learnable)
    check("every learnable category reports learnable",
          test_every_learnable_category_reports_learnable)
    check("is_learnable fails closed on unrecognized/missing input",
          test_is_learnable_fails_closed_on_unrecognized_or_missing_input)
    check("is_learnable never raises on unhashable input",
          test_is_learnable_never_raises_on_unhashable_input)
    check("LABELS covers every category with a non-empty string",
          test_labels_covers_every_category_with_a_nonempty_string)
    check("BULLETS covers every category exactly once with non-empty prose",
          test_bullets_covers_every_category_exactly_once_with_nonempty_prose)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} lesson-category test(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL LESSON-CATEGORY TESTS PASSED")


if __name__ == "__main__":
    run()
