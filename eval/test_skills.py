"""Self-learning "lessons" (app/skills.py): lessons store a human-readable RULE
with the SQL as an optional example; the critic emits unverified lessons; saves
dedup against near-duplicates; retrieval leads with the rule and is gated by the
skills_enabled flag.

Embeddings (fastembed) aren't available in CI, so `skills.embed` is patched with
a deterministic bag-of-words vector where needed — this exercises the cosine
dedup/retrieval paths reproducibly, and also covers the no-embeddings fallbacks.
"""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["APP_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "app.db")
os.environ["COOKIE_SECURE"] = "false"

import numpy as np  # noqa: E402

from app import skills  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, init_db  # noqa: E402

get_settings.cache_clear()
init_db()
FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _reset():
    con = connect()
    con.execute("DELETE FROM skills")
    con.commit()
    con.close()


def _fake_embed(text):
    """Deterministic bag-of-words vector (8 dims, L2-normalized). Identical text →
    identical vector (cosine 1.0); disjoint word sets → near-orthogonal."""
    v = np.zeros(8, dtype=np.float32)
    for w in text.lower().split():
        b = int(hashlib.md5(w.encode()).hexdigest(), 16) % 8
        v[b] += 1.0
    n = np.linalg.norm(v)
    return (v / n) if n else v


def _with_embed(fn, embed=_fake_embed):
    orig = skills.embed
    skills.embed = embed
    try:
        return fn()
    finally:
        skills.embed = orig


def _count(created_by=None):
    con = connect()
    try:
        if created_by:
            return con.execute("SELECT COUNT(*) FROM skills WHERE created_by=?",
                               (created_by,)).fetchone()[0]
        return con.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    finally:
        con.close()


# --- _lesson_text (pure) -------------------------------------------------------

def test_lesson_text_leads_with_rule_then_example():
    row = {"lesson": "Use cipcode='99' for national totals.", "notes": "",
           "question": "national total", "canonical_sql": "SELECT 1"}
    t = skills._lesson_text(row)
    assert t.startswith("LESSON: Use cipcode='99'"), t
    assert "e.g. Q: national total" in t and "SELECT 1" in t, t


def test_lesson_text_rule_only():
    row = {"lesson": "Always filter majornum=1.", "notes": "",
           "question": "", "canonical_sql": ""}
    assert skills._lesson_text(row) == "LESSON: Always filter majornum=1.", skills._lesson_text(row)


def test_lesson_text_falls_back_to_notes():
    row = {"lesson": None, "notes": "old note rule", "question": "", "canonical_sql": ""}
    assert skills._lesson_text(row) == "LESSON: old note rule"


# --- retrieval -----------------------------------------------------------------

def test_retrieve_leads_with_lesson():
    _reset()
    _with_embed(lambda: skills.save_skill(
        "nursing associate degrees nationwide", "SELECT 1",
        lesson="Filter cipcode='51.3801' exactly.", created_by="seed", verified=True))
    block, ids = _with_embed(lambda: skills.retrieve_skills_block(
        "nursing associate degrees nationwide"))
    assert ids, "expected a retrieved lesson"
    assert block.startswith("LESSON: Filter cipcode='51.3801'"), block


def test_retrieve_disabled_returns_empty():
    _reset()
    _with_embed(lambda: skills.save_skill(
        "q words here", "SELECT 1", lesson="rule", verified=True))
    orig = skills.get_settings
    skills.get_settings = lambda: type("S", (), {"skills_enabled": False})()
    try:
        block, ids = _with_embed(lambda: skills.retrieve_skills_block("q words here"))
    finally:
        skills.get_settings = orig
    assert block == "" and ids == [], (block, ids)


def test_retrieve_without_embeddings_is_noop():
    _reset()
    block, ids = _with_embed(lambda: skills.retrieve_skills_block("anything"),
                             embed=lambda _t: None)
    assert block == "" and ids == [], (block, ids)


def test_unverified_lessons_are_not_retrieved():
    _reset()
    _with_embed(lambda: skills.save_skill(
        "unique alpha beta gamma", "SELECT 1", lesson="secret", verified=False))
    _, ids = _with_embed(lambda: skills.retrieve_skills_block("unique alpha beta gamma"))
    assert ids == [], "unverified lessons must never be retrieved"


# --- dedup ---------------------------------------------------------------------

def test_promote_dedups_near_duplicate_via_embedding():
    _reset()
    q = "how many bachelor degrees in nursing"
    _with_embed(lambda: skills.promote_from_message(q, "SELECT 1"))
    _with_embed(lambda: skills.promote_from_message(q, "SELECT 1"))  # same scenario
    assert _count() == 1, "a near-duplicate must upvote, not insert a second row"
    con = connect()
    up = con.execute("SELECT upvotes FROM skills").fetchone()[0]
    con.close()
    assert up == 1, f"expected 1 upvote on the deduped row, got {up}"


def test_distinct_scenario_inserts_new_row():
    _reset()
    _with_embed(lambda: skills.promote_from_message("apple banana cherry", "SELECT 1"))
    _with_embed(lambda: skills.promote_from_message("xylophone yak zebra", "SELECT 2"))
    assert _count() == 2, "distinct scenarios must each be stored"


def test_exact_match_dedup_without_embeddings():
    _reset()
    def _no_embed(_t):
        return None
    _with_embed(lambda: skills.promote_from_message("same q", "SELECT 9"), embed=_no_embed)
    _with_embed(lambda: skills.promote_from_message("same q", "SELECT 9"), embed=_no_embed)
    assert _count() == 1, "exact (question, sql) must dedup when embeddings are off"


# --- critic emission -----------------------------------------------------------

def test_record_lesson_from_critic_is_unverified():
    _reset()
    _with_embed(lambda: skills.record_lesson_from_critic(
        "national bachelor total", "SELECT SUM(x) FROM c_a",
        "no majornum=1 filter — double-counts second majors"))
    con = connect()
    r = con.execute("SELECT created_by, verified, lesson, canonical_sql FROM skills").fetchone()
    con.close()
    assert r["created_by"] == "critic", r["created_by"]
    assert r["verified"] == 0, "critic lessons must start unverified"
    assert "majornum" in r["lesson"], r["lesson"]
    assert r["canonical_sql"] == "SELECT SUM(x) FROM c_a"


def test_record_lesson_empty_issue_is_noop():
    _reset()
    _with_embed(lambda: skills.record_lesson_from_critic("q", "SELECT 1", "   "))
    assert _count() == 0, "an empty critic issue must not create a lesson"


def test_critic_lesson_dedups_against_existing():
    _reset()
    q = "delta epsilon zeta scenario"
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 1", "rule one"))
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 1", "rule one again"))
    assert _count() == 1, "a repeat critic finding on the same scenario must dedup"


def test_critic_lesson_not_collapsed_into_verified_seed():
    # The HIGH review bug: a new critic finding on a question similar to an
    # already-VERIFIED lesson must NOT be discarded into it (nor upvote it) — it's
    # a distinct rule and must be stored as its own pending candidate.
    _reset()
    q = "national total associate degrees per year"
    _with_embed(lambda: skills.save_skill(
        q, "SELECT 1", lesson="use cipcode='99'", created_by="seed", verified=True))
    _with_embed(lambda: skills.record_lesson_from_critic(
        q, "SELECT 2", "award-level rollup mixing — filter awlevel to real codes"))
    assert _count() == 2, "a distinct critic rule must not collapse into a verified seed"
    con = connect()
    seed = con.execute("SELECT upvotes FROM skills WHERE created_by='seed'").fetchone()[0]
    con.close()
    assert seed == 0, "the verified seed's upvotes must not be inflated by dedup"


def test_dedup_does_not_cross_sources():
    # A feedback 👍 and a critic finding on the same scenario are different signals
    # and must not dedup into each other.
    _reset()
    q = "same scenario two sources"
    _with_embed(lambda: skills.promote_from_message(q, "SELECT 1"))      # feedback
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 1", "a rule"))  # critic
    assert _count() == 2, "different sources on the same scenario must not dedup"


def test_dedup_backfills_empty_lesson():
    # If the matched pending row has no rule yet, a later same-source finding with
    # a rule backfills it instead of losing the text.
    _reset()
    q = "backfill scenario words"
    _with_embed(lambda: skills.save_skill(
        q, "SELECT 1", lesson="", created_by="critic", verified=False))
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 1", "the real rule"))
    assert _count() == 1, "same-source same-scenario must dedup"
    con = connect()
    lesson = con.execute("SELECT lesson FROM skills").fetchone()[0]
    con.close()
    assert lesson == "the real rule", f"empty rule should be backfilled, got {lesson!r}"


def test_cache_lookup_disabled_when_skills_off():
    orig = skills.get_settings
    skills.get_settings = lambda: type("S", (), {"skills_enabled": False})()
    try:
        assert skills.cache_lookup("anything") is None, \
            "cache must be gated by skills_enabled for a clean A/B baseline"
    finally:
        skills.get_settings = orig


def run():
    print("self-learning lessons:")
    check("_lesson_text leads with the rule then the example",
          test_lesson_text_leads_with_rule_then_example)
    check("_lesson_text rule-only", test_lesson_text_rule_only)
    check("_lesson_text falls back to notes", test_lesson_text_falls_back_to_notes)
    check("retrieval leads with the LESSON rule", test_retrieve_leads_with_lesson)
    check("retrieval is empty when skills disabled", test_retrieve_disabled_returns_empty)
    check("retrieval is a no-op without embeddings", test_retrieve_without_embeddings_is_noop)
    check("unverified lessons are never retrieved", test_unverified_lessons_are_not_retrieved)
    check("promotion dedups a near-duplicate (embedding)",
          test_promote_dedups_near_duplicate_via_embedding)
    check("distinct scenarios each insert a row", test_distinct_scenario_inserts_new_row)
    check("exact-match dedup without embeddings", test_exact_match_dedup_without_embeddings)
    check("critic-emitted lesson is unverified", test_record_lesson_from_critic_is_unverified)
    check("empty critic issue is a no-op", test_record_lesson_empty_issue_is_noop)
    check("repeat critic finding dedups", test_critic_lesson_dedups_against_existing)
    check("distinct critic rule not collapsed into a verified seed",
          test_critic_lesson_not_collapsed_into_verified_seed)
    check("dedup does not cross sources", test_dedup_does_not_cross_sources)
    check("dedup backfills an empty rule", test_dedup_backfills_empty_lesson)
    check("cache lookup is gated by skills_enabled", test_cache_lookup_disabled_when_skills_off)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} lesson test(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL SKILLS/LESSON TESTS PASSED")


if __name__ == "__main__":
    run()
