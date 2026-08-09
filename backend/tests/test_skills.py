"""Self-learning "lessons" (backend/app/skills.py): a lesson is a short HEADLINE + a
longer generalized DESCRIPTION + a commented SQL worked example. There are TWO
lesson sources: the post-answer critic (mining the model's own mistakes,
`created_by="critic"`) and the feedback distiller (mining a user's corrective
feedback on a follow-up turn, `created_by="user-feedback"`) — the old
thumbs-up feedback path was removed, but this is a distinct, newer mechanism,
not its return. Saves dedup against near-duplicates SCOPED TO THE SAME SOURCE
(a critic finding never dedups into a user-feedback row or vice versa, nor into
a verified seed); retrieval leads with the headline and is gated by the
skills_enabled flag. The embedding source is headline+description — NEVER the
question — on every write, dedup lookup, and re-embed pass.

Dedup is decided PURELY by cosine similarity of that headline+description
vector whenever embeddings are available; the exact-(question, canonical_sql,
source) match is a fallback that applies ONLY when embed() returns None
system-wide (no fastembed). So a "true repeat" test must reuse the IDENTICAL
headline+description text (identical embedding, cosine 1.0) — a merely
SIMILAR rule on the same scenario must NOT collapse into an existing row.

Embeddings (fastembed) aren't available in CI, so `skills.embed` is patched with
a deterministic bag-of-words vector where needed — this exercises the cosine
dedup/retrieval paths reproducibly, and also covers the no-embeddings fallbacks.
"""
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["APP_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "app.db")
os.environ["COOKIE_SECURE"] = "false"

import numpy as np  # noqa: E402

from app import skills  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, get_meta, init_db, set_meta  # noqa: E402
from app.seeds import SEED_EXAMPLES, SEED_LESSON_REWRITES, SEED_LESSON_UPGRADES  # noqa: E402

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


def check_pending(name, fn):
    """Like check(), but for the A2 (lesson-rejection memory) block below, whose
    target surface (skills.muted_categories/set_category_muted/_find_suppressor,
    the tombstone check in _upvote_or_save, category on save_skill) doesn't
    exist yet. check() only catches AssertionError -- deliberately, so an
    EXISTING test's genuinely unexpected exception crashes loudly rather than
    reading as one more failure line. Calling not-yet-implemented API instead
    raises AttributeError/TypeError, which check() would let escape and crash
    the whole file, hiding every other new test's red status behind the first
    one reached. Scoped to just this block so it changes nothing about how any
    existing test is graded."""
    try:
        fn()
        print(f"  ✓ {name}")
    except Exception as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {type(e).__name__}: {e}")


def _reset():
    con = connect()
    con.execute("DELETE FROM skills")
    con.execute("DELETE FROM meta WHERE key='skills_embed_source_version'")
    con.execute("DELETE FROM meta WHERE key=?", (skills._SEED_APPLIED_KEY,))
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


# --- A2 helpers: lesson-rejection tombstones + muted categories (migration 35) -

def _clear_rejections():
    con = connect()
    con.execute("DELETE FROM lesson_rejections")
    con.commit()
    con.close()


def _clear_muted_categories():
    con = connect()
    con.execute("DELETE FROM meta WHERE key='muted_lesson_categories'")
    con.commit()
    con.close()


def _insert_tombstone(headline, description, *, category=None, created_by=None,
                      skill_id=None, was_verified=0, embed_fn=_fake_embed):
    """Write a lesson_rejections row directly (bypassing the not-yet-written
    admin.delete_skill), embedding headline+description the same way
    skills._embed_source does for a real lesson -- so a candidate near-identical
    to this tombstone is findable by cosine, exactly as a real rejection would be."""
    source = skills._embed_source(headline, description)
    v = embed_fn(source) if (embed_fn and source) else None
    con = connect()
    con.execute(
        "INSERT INTO lesson_rejections(headline, description, embedding, category, "
        "created_by, skill_id, was_verified, hits, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (headline, description, skills._to_blob(v) if v is not None else None,
         category, created_by, skill_id, int(was_verified), 0, time.time()))
    con.commit()
    con.close()


# --- _lesson_text (pure) -------------------------------------------------------

def test_lesson_text_leads_with_headline_then_description_then_sql():
    row = {"headline": "Use cipcode='99' for national totals.",
           "lesson": "Summing individual CIP codes overcounts; the '99' row is "
                     "already the grand total.",
           "notes": "", "question": "national total", "canonical_sql": "SELECT 1 -- x"}
    t = skills._lesson_text(row)
    assert t.startswith("LESSON: Use cipcode='99' for national totals."), t
    assert "Summing individual CIP codes overcounts" in t, t
    assert "SQL (inline comments explain each field):" in t, t
    assert "SELECT 1 -- x" in t, t
    assert "e.g. Q:" not in t, t


def test_lesson_text_no_question_echo():
    row = {"headline": "A headline.", "lesson": "A description.", "notes": "",
           "question": "should never appear", "canonical_sql": "SELECT 1"}
    t = skills._lesson_text(row)
    assert "should never appear" not in t, t
    assert "Q:" not in t, t


def test_lesson_text_no_headline_falls_back_to_lesson_prefixed():
    row = {"headline": "", "lesson": "Always filter majornum=1 for every completions total.",
           "notes": "", "question": "", "canonical_sql": ""}
    assert skills._lesson_text(row) == \
        "LESSON: Always filter majornum=1 for every completions total.", skills._lesson_text(row)


def test_lesson_text_falls_back_to_notes_when_lesson_empty():
    row = {"headline": "", "lesson": None, "notes": "old legacy note rule",
           "question": "", "canonical_sql": ""}
    assert skills._lesson_text(row) == "LESSON: old legacy note rule"


def test_lesson_text_all_empty_is_empty_string():
    row = {"headline": "", "lesson": "", "notes": "", "question": "", "canonical_sql": ""}
    assert skills._lesson_text(row) == "", skills._lesson_text(row)


# --- _embed_source (pure) -------------------------------------------------------

def test_embed_source_is_headline_newline_description():
    assert skills._embed_source("Headline.", "Description.") == "Headline.\nDescription."


def test_embed_source_strips_outer_whitespace():
    headline, description = "  H  ", "  D  "
    expected = (headline + "\n" + description).strip()
    assert skills._embed_source(headline, description) == expected


def test_embed_source_empty_both_is_empty_string():
    assert skills._embed_source("", "") == ""


# --- retrieval -----------------------------------------------------------------

def test_retrieve_leads_with_headline():
    _reset()
    _with_embed(lambda: skills.save_skill(
        "nursing associate degrees nationwide", "SELECT 1",
        headline="Filter cipcode='51.3801' exactly.",
        lesson="Never use a prefix match on cipcode.",
        created_by="seed", verified=True))
    block, ids = _with_embed(lambda: skills.retrieve_skills_block(
        "nursing associate degrees nationwide"))
    assert ids, "expected a retrieved lesson"
    assert block.startswith("LESSON: Filter cipcode='51.3801'"), block


def test_retrieve_disabled_returns_empty():
    _reset()
    _with_embed(lambda: skills.save_skill(
        "q words here", "SELECT 1", headline="h", lesson="rule", verified=True))
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
        "unique alpha beta gamma", "SELECT 1", headline="h", lesson="secret", verified=False))
    _, ids = _with_embed(lambda: skills.retrieve_skills_block("unique alpha beta gamma"))
    assert ids == [], "unverified lessons must never be retrieved"


# --- dedup (migrated off promote_from_message onto record_lesson_from_critic) --

def test_critic_dedups_a_true_repeat_via_embedding():
    # With embeddings available, dedup is decided PURELY by cosine similarity
    # (the exact-match fallback only kicks in when embed() returns None
    # system-wide) — so a TRUE repeat here means the identical headline AND
    # description text, which embeds to an identical (cosine 1.0) vector.
    _reset()
    q = "how many bachelor degrees in nursing"
    headline = "Add majornum=1 for every completions total."
    description = "no majornum=1 filter; double-counts second majors"
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 1", headline, description))
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 1", headline, description))
    assert _count() == 1, "an identical repeat must upvote, not insert a second row"
    con = connect()
    up = con.execute("SELECT upvotes FROM skills").fetchone()[0]
    con.close()
    assert up == 1, f"expected 1 upvote on the deduped row, got {up}"


def test_critic_distinct_rule_same_scenario_is_not_deduped():
    # Review-driven fix: the exact-(question, canonical_sql) fallback is
    # restricted to the no-embeddings case ONLY. With embeddings available, a
    # genuinely DIFFERENT rule (different headline+description) on the SAME
    # (question, SQL) scenario must survive as its own pending row instead of
    # being over-collapsed by an identical-scenario shortcut. The two rule
    # texts below share no words, so under the deterministic bag-of-words test
    # embedder they land far apart (well under the dedup threshold).
    _reset()
    q = "same scenario distinct rules"
    sql = "SELECT 1"
    _with_embed(lambda: skills.record_lesson_from_critic(
        q, sql, "Filter to an exact leaf CIP code.",
        "Never sum rollup rows together with the leaf level."))
    _with_embed(lambda: skills.record_lesson_from_critic(
        q, sql, "Join hd on unitid and year.",
        "Match state and control filters to the correct collection year."))
    assert _count() == 2, \
        "two genuinely distinct rules on the same (question, SQL) must NOT dedup"


def test_distinct_scenario_inserts_new_row():
    _reset()
    _with_embed(lambda: skills.record_lesson_from_critic(
        "apple banana cherry", "SELECT 1", "H1", "rule one"))
    _with_embed(lambda: skills.record_lesson_from_critic(
        "xylophone yak zebra", "SELECT 2", "H2", "rule two"))
    assert _count() == 2, "distinct scenarios must each be stored"


def test_exact_match_dedup_without_embeddings():
    _reset()
    def _no_embed(_t):
        return None
    _with_embed(lambda: skills.record_lesson_from_critic("same q", "SELECT 9", "H", "r"),
               embed=_no_embed)
    _with_embed(lambda: skills.record_lesson_from_critic("same q", "SELECT 9", "H", "r"),
               embed=_no_embed)
    assert _count() == 1, "exact (question, sql) must dedup when embeddings are off"


# --- critic emission -----------------------------------------------------------

def test_record_lesson_from_critic_is_unverified_with_headline_and_description():
    _reset()
    _with_embed(lambda: skills.record_lesson_from_critic(
        "national bachelor total", "SELECT SUM(x) FROM c_a",
        "Add majornum=1 for every completions total.",
        "no majornum=1 filter — double-counts second majors"))
    con = connect()
    r = con.execute(
        "SELECT created_by, verified, headline, lesson, canonical_sql FROM skills").fetchone()
    con.close()
    assert r["created_by"] == "critic", r["created_by"]
    assert r["verified"] == 0, "critic lessons must start unverified"
    assert r["headline"] == "Add majornum=1 for every completions total.", r["headline"]
    assert "majornum" in r["lesson"], r["lesson"]
    assert r["canonical_sql"] == "SELECT SUM(x) FROM c_a"


def test_record_lesson_both_blank_is_noop():
    _reset()
    _with_embed(lambda: skills.record_lesson_from_critic("q", "SELECT 1", "   ", "   "))
    assert _count() == 0, "a blank headline AND description must not create a lesson"


def test_record_lesson_headline_only_is_not_noop():
    _reset()
    _with_embed(lambda: skills.record_lesson_from_critic("q", "SELECT 1", "A headline only.", ""))
    assert _count() == 1, "a non-blank headline alone must still record a lesson"


def test_critic_lesson_not_collapsed_into_verified_seed():
    # The HIGH review bug: a new critic finding on a question similar to an
    # already-VERIFIED lesson must NOT be discarded into it (nor upvote it) — it's
    # a distinct rule and must be stored as its own pending candidate.
    _reset()
    q = "national total associate degrees per year"
    _with_embed(lambda: skills.save_skill(
        q, "SELECT 1", headline="Use cipcode='99'.", lesson="use the grand-total row",
        created_by="seed", verified=True))
    _with_embed(lambda: skills.record_lesson_from_critic(
        q, "SELECT 2", "Filter awlevel to real codes.",
        "award-level rollup mixing — filter awlevel to real codes"))
    assert _count() == 2, "a distinct critic rule must not collapse into a verified seed"
    con = connect()
    seed = con.execute("SELECT upvotes FROM skills WHERE created_by='seed'").fetchone()[0]
    con.close()
    assert seed == 0, "the verified seed's upvotes must not be inflated by dedup"



# --- feedback-distiller emission (mirrors the critic emission tests above) -----
# The feedback distiller (app/feedback.py) mines the USER's corrective feedback
# (rather than the critic mining the model's own mistake) into the SAME
# unverified-lesson pool via a THIN wrapper, `record_lesson_from_feedback`,
# reusing `_upvote_or_save(..., source="user-feedback")` — the whole dedup ->
# unverified -> admin-approve pipeline unchanged, just a distinct `created_by`.

def test_record_lesson_from_feedback_is_unverified_with_user_feedback_source():
    _reset()
    _with_embed(lambda: skills.record_lesson_from_feedback(
        "which undergraduate major produces the most graduates?",
        "Ask a clarifying question before assuming an award-level scope.",
        "when a request doesn't specify an award level, ask instead of silently "
        "assuming bachelor's-only"))
    con = connect()
    r = con.execute(
        "SELECT created_by, verified, headline, lesson, canonical_sql FROM skills").fetchone()
    con.close()
    assert r["created_by"] == "user-feedback", r["created_by"]
    assert r["verified"] == 0, "a feedback-distilled lesson must start unverified"
    assert r["headline"] == \
        "Ask a clarifying question before assuming an award-level scope.", r["headline"]
    # This phrase lives ONLY in the lesson/description text passed above (the
    # headline has no "bachelor's-only" text) -- pins that `lesson` is really the
    # description arg stored verbatim, not the headline or some other field.
    assert "silently assuming bachelor's-only" in r["lesson"], r["lesson"]


def test_record_lesson_from_feedback_both_blank_is_noop():
    _reset()
    _with_embed(lambda: skills.record_lesson_from_feedback("q", "   ", "   "))
    assert _count() == 0, "a blank headline AND description must not create a lesson"


def test_feedback_dedups_a_true_repeat_via_embedding():
    _reset()
    q = "which undergraduate major produces the most graduates?"
    headline = "Ask a clarifying question before assuming an award-level scope."
    description = "don't silently assume bachelor's-only when the level is unstated"
    _with_embed(lambda: skills.record_lesson_from_feedback(q, headline, description))
    _with_embed(lambda: skills.record_lesson_from_feedback(q, headline, description))
    assert _count() == 1, "an identical repeat must upvote, not insert a second row"
    con = connect()
    up = con.execute("SELECT upvotes FROM skills").fetchone()[0]
    con.close()
    assert up == 1, f"expected 1 upvote on the deduped row, got {up}"


def test_feedback_lesson_not_collapsed_into_a_critic_row_same_scenario():
    # The dedup-scoping requirement from the spec: a feedback-distilled lesson
    # must dedup ONLY against other PENDING user-feedback candidates, never a
    # critic (or seed) row — even one with the IDENTICAL headline+description on
    # the SAME scenario. Distinct source, so it must survive as its own row and
    # must not inflate the critic row's upvotes.
    _reset()
    q = "same scenario, two sources"
    headline = "Filter on an exact 6-digit CIP code, not a rollup."
    description = "cipcode LIKE '51.%' double counts across the 2-/4-/6-digit rollup rows"
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 1", headline, description))
    _with_embed(lambda: skills.record_lesson_from_feedback(q, headline, description))
    assert _count() == 2, \
        "a feedback lesson must not collapse into a critic row on the same scenario"
    con = connect()
    critic_upvotes = con.execute(
        "SELECT upvotes FROM skills WHERE created_by='critic'").fetchone()[0]
    con.close()
    assert critic_upvotes == 0, \
        "the critic row's upvotes must not be inflated by a feedback-source dedup"


def test_feedback_lesson_not_collapsed_into_verified_seed():
    _reset()
    q = "national total associate degrees per year"
    _with_embed(lambda: skills.save_skill(
        q, "SELECT 1", headline="Use cipcode='99'.", lesson="use the grand-total row",
        created_by="seed", verified=True))
    _with_embed(lambda: skills.record_lesson_from_feedback(
        q, "Keep the scope established by an earlier turn.",
        "a follow-up question should inherit the award level/year set earlier "
        "in the conversation unless the user changes it"))
    assert _count() == 2, "a distinct feedback lesson must not collapse into a verified seed"
    con = connect()
    seed_upvotes = con.execute(
        "SELECT upvotes FROM skills WHERE created_by='seed'").fetchone()[0]
    con.close()
    assert seed_upvotes == 0, "the verified seed's upvotes must not be inflated by dedup"


def test_dedup_is_scoped_to_same_source():
    # An unverified row from a DIFFERENT source on the same scenario must not
    # dedup into (or be dedupped by) a critic finding — dedup is source-scoped.
    _reset()
    q = "same scenario two sources"
    _with_embed(lambda: skills.save_skill(
        q, "SELECT 1", headline="", lesson="", created_by="manual", verified=False))
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 1", "H", "a rule"))
    assert _count() == 2, "different sources on the same scenario must not dedup"


def test_dedup_backfills_empty_headline_and_lesson_without_embeddings():
    # A rule-less pending row (headline+lesson both empty) has a NULL
    # embedding (save_skill never embeds an empty source), so it can only
    # ever be found by the exact-(question, canonical_sql) fallback — which
    # now applies ONLY when embed() returns None system-wide. Exercise that
    # path explicitly and confirm the backfill-onto-empty-rule behavior it
    # protects still works.
    _reset()
    q = "backfill scenario words"
    def _no_embed(_t):
        return None
    _with_embed(lambda: skills.save_skill(
        q, "SELECT 1", headline="", lesson="", created_by="critic", verified=False),
        embed=_no_embed)
    _with_embed(lambda: skills.record_lesson_from_critic(
        q, "SELECT 1", "The real headline.", "the real description"), embed=_no_embed)
    assert _count() == 1, "same-source same-scenario must dedup (exact match, no embeddings)"
    con = connect()
    row = con.execute("SELECT headline, lesson FROM skills").fetchone()
    con.close()
    assert row["headline"] == "The real headline.", row["headline"]
    assert row["lesson"] == "the real description", row["lesson"]


# --- seed data (app.seeds) ------------------------------------------------------

def test_seed_lessons_have_headline_and_readable_description():
    assert len(SEED_EXAMPLES) == 8, len(SEED_EXAMPLES)
    for ex in SEED_EXAMPLES:
        assert ex.headline, f"seed missing a headline: {ex!r}"
        assert len(ex.headline) <= 110, f"headline should be short: {ex.headline!r}"
        assert len(ex.description) >= 80, \
            f"description too short to be a generalized sentence: {ex.description!r}"
        assert ex.description.endswith("."), \
            f"description should end with a period: {ex.description!r}"
        assert any(w.islower() and len(w) > 2 for w in ex.description.split()), \
            f"description doesn't read as prose: {ex.description!r}"
        # A worked SQL example must carry inline comments explaining its fields.
        # A purely conversational lesson (e.g. "clarify the year") legitimately
        # ships no query, so the comment rule only applies when SQL is present.
        if ex.commented_sql:
            assert "--" in ex.commented_sql, \
                f"commented_sql needs inline comments: {ex.commented_sql!r}"


def test_seed_lesson_upgrades_consistent_with_seed_examples():
    # Drift guard: every SEED_LESSON_UPGRADES target must be the SAME SeedLesson
    # object shipped in SEED_EXAMPLES, or a future edit to one without the other
    # would desync a fresh install's seeds from an upgraded live db. Upgrades
    # cover ONLY the original migration-6 seeds (which shipped in a terse form);
    # seeds promoted later never had a v1, so they carry no upgrade entry — hence
    # <= rather than ==, and the upgrades must align with the FIRST N seeds.
    assert len(SEED_LESSON_UPGRADES) <= len(SEED_EXAMPLES), len(SEED_LESSON_UPGRADES)
    for i, (_v1_description, v2) in enumerate(SEED_LESSON_UPGRADES):
        assert v2 == SEED_EXAMPLES[i], \
            f"SEED_LESSON_UPGRADES[{i}][1] must equal SEED_EXAMPLES[{i}]"


def test_seed_lesson_rewrites_are_frozen_literals():
    # The OLD (pre-migration-6, terse) strings and the v1 (post-migration-6,
    # readable-but-not-yet-generalized) strings are BOTH frozen migration match
    # keys now — hard-coded here so nobody can accidentally change them, which
    # would break migration 6 (old->v1) on an already-seeded database.
    frozen_olds = [
        "Exact 6-digit CIP; constant year bound; RANK per year.",
        "Year-matched hd join; control=1 public; awlevel=5 bachelor's.",
        "Use grand-total CIP '99' — never sum all cipcodes (overcounts ~4x).",
    ]
    frozen_v1_descriptions = [
        "Match an exact 6-digit CIP code (here 51.3801, Registered Nursing) so the "
        "2- and 4-digit rollup rows that also live in c_a aren't double-counted. "
        "Express \"the last N years\" as a constant bound — "
        "year > (SELECT MAX(year)-3 FROM _years) — instead of joining to a list of "
        "years, which would force a slow full scan. Rank within each year using "
        "RANK() OVER (PARTITION BY year ORDER BY awards DESC).",
        "Bachelor's degrees are awlevel=5 and Computer Science is CIP 11.0701. To "
        "filter by state or by public vs. private, join each c_a completions row to "
        "the hd institution-directory table on BOTH unitid and year, then use "
        "control=1 for public institutions and stabbr for the state. Joining on year "
        "as well keeps each school's attributes aligned with the degree's collection "
        "year.",
        "For a national or all-programs total, filter cipcode='99' — the "
        "pre-aggregated grand-total row — rather than summing across individual CIP "
        "codes. c_a stores 2-, 4-, and 6-digit CIP rollups that each re-sum to the "
        "same total, so adding them together overcounts by roughly 4x. Also keep "
        "majornum=1 so a student's second major isn't counted twice.",
    ]
    actual_olds = [old for old, _new in SEED_LESSON_REWRITES]
    actual_news = [new for _old, new in SEED_LESSON_REWRITES]
    assert actual_olds == frozen_olds, actual_olds
    assert actual_news == frozen_v1_descriptions, actual_news

    # SEED_LESSON_UPGRADES' match key (the frozen v1 description) must be the
    # SAME text migration 6 rewrites terse rows INTO, so a db already upgraded
    # by migration 6 (a live/production db) is exactly what upgrade_seed_lessons() matches.
    v1_match_keys = [v1 for v1, _v2 in SEED_LESSON_UPGRADES]
    assert v1_match_keys == frozen_v1_descriptions, v1_match_keys


def test_save_skill_embeds_headline_and_description_not_question():
    _reset()
    captured = {}
    def _capturing(text):
        captured["text"] = text
        return _fake_embed(text)
    _with_embed(lambda: skills.save_skill(
        "some unrelated question text nobody should embed", "SELECT 1",
        headline="Do X, not Y.", lesson="Because Y silently double-counts, always do X instead.",
        created_by="seed", verified=True), embed=_capturing)
    assert captured["text"] == skills._embed_source(
        "Do X, not Y.", "Because Y silently double-counts, always do X instead."), captured
    assert "unrelated question" not in captured["text"], captured


def test_save_skill_null_embedding_when_headline_and_lesson_both_empty():
    _reset()
    called = {"n": 0}
    def _would_embed(text):
        called["n"] += 1
        return _fake_embed(text or "x")
    _with_embed(lambda: skills.save_skill(
        "some question", "SELECT 1", headline="", lesson="",
        created_by="system", verified=False), embed=_would_embed)
    con = connect()
    emb = con.execute("SELECT embedding FROM skills").fetchone()[0]
    con.close()
    assert emb is None, "embedding must be NULL when headline+description are both empty"
    assert called["n"] == 0, "embed() must not even be called for an empty embedding source"


def test_seed_from_schema_examples_inserts_verified_seed_rows():
    _reset()
    n = _with_embed(lambda: skills.seed_from_schema_examples())
    assert n == len(SEED_EXAMPLES), n
    assert _count(created_by="seed") == len(SEED_EXAMPLES)
    con = connect()
    rows = con.execute(
        "SELECT headline, lesson, canonical_sql, verified, embedding FROM skills "
        "WHERE created_by='seed' ORDER BY id").fetchall()
    con.close()
    assert len(rows) == len(SEED_EXAMPLES)
    for r, ex in zip(rows, SEED_EXAMPLES, strict=True):
        assert r["headline"] == ex.headline, (r["headline"], ex.headline)
        assert r["lesson"] == ex.description, (r["lesson"], ex.description)
        assert r["canonical_sql"] == ex.commented_sql
        assert r["verified"] == 1, "seed rows must start verified"
        got = skills._from_blob(r["embedding"])
        want = _fake_embed(skills._embed_source(ex.headline, ex.description))
        assert np.allclose(got, want), "seed embedding must derive from headline+description"


def test_seed_from_schema_examples_is_idempotent():
    _reset()
    _with_embed(lambda: skills.seed_from_schema_examples())
    n = _with_embed(lambda: skills.seed_from_schema_examples())
    assert n == 0, "every seed was already applied — the second call must insert nothing"
    assert _count() == len(SEED_EXAMPLES), "a second seed call must not add rows"


def test_seeding_ships_a_lesson_added_in_a_later_release():
    """THE upgrade bug (found live on 0.2.0): seeding used to bail whenever the
    skills table held ANY row, so a seed added in a later release reached fresh
    installs only. Every existing deployment had rows, so the gate never
    reopened and new exemplars silently never arrived."""
    _reset()
    # A deployment seeded by an older release that shipped all but the last
    # lesson, and which has since accumulated a critic lesson of its own.
    shipped_then = SEED_EXAMPLES[:-1]
    new_in_this_release = SEED_EXAMPLES[-1]
    for s in shipped_then:
        _with_embed(lambda s=s: skills.save_skill(
            s.question, s.commented_sql, headline=s.headline, lesson=s.description,
            created_by="seed", verified=True))
    _with_embed(lambda: skills.save_skill(
        "a question a user asked", "SELECT 1", headline="A rule the critic found.",
        lesson="Some generalized description.", created_by="critic", verified=False))

    n = _with_embed(lambda: skills.seed_from_schema_examples())

    assert n == 1, f"exactly the one new lesson should ship, got {n}"
    con = connect()
    headlines = [r[0] for r in con.execute(
        "SELECT headline FROM skills WHERE created_by='seed'")]
    con.close()
    assert new_in_this_release.headline in headlines, "the new seed never arrived"
    assert len(headlines) == len(SEED_EXAMPLES), \
        f"the already-present seeds must not be duplicated: {len(headlines)}"


def test_seeding_an_already_seeded_db_adds_nothing_and_records_the_backfill():
    """A database seeded before slug tracking existed has no marker at all. It
    must be recognized from its existing rows, not re-seeded from scratch —
    save_skill does no dedup, so a miss here means 8 duplicate lessons."""
    _reset()
    for s in SEED_EXAMPLES:
        _with_embed(lambda s=s: skills.save_skill(
            s.question, s.commented_sql, headline=s.headline, lesson=s.description,
            created_by="seed", verified=True))
    con = connect()  # the marker-less state a pre-upgrade db is actually in
    con.execute("DELETE FROM meta WHERE key=?", (skills._SEED_APPLIED_KEY,))
    con.commit()
    con.close()

    n = _with_embed(lambda: skills.seed_from_schema_examples())

    assert n == 0, f"an already-seeded db must gain nothing, got {n} duplicate(s)"
    assert _count(created_by="seed") == len(SEED_EXAMPLES)
    con = connect()
    marker = get_meta(con, skills._SEED_APPLIED_KEY)
    con.close()
    assert marker is not None, "the backfill must be recorded, or it re-runs every boot"
    assert set(json.loads(marker)) == {s.slug for s in SEED_EXAMPLES}, marker


def test_a_deleted_seed_is_not_resurrected_on_the_next_boot():
    """An admin deleting a seed from the Skills tab is a decision, not drift.
    Deriving "what's missing" from the table alone would undo it every restart."""
    _reset()
    _with_embed(lambda: skills.seed_from_schema_examples())
    con = connect()
    con.execute("DELETE FROM skills WHERE headline=?", (SEED_EXAMPLES[0].headline,))
    con.commit()
    con.close()

    n = _with_embed(lambda: skills.seed_from_schema_examples())

    assert n == 0, "a deliberately deleted seed must stay deleted"
    assert _count(created_by="seed") == len(SEED_EXAMPLES) - 1


def test_backfill_matches_a_seed_row_an_admin_edited():
    """The backfill matches on headline OR question because the Skills tab lets
    an admin rewrite either. Matching on headline alone would re-insert an
    edited lesson beside the admin's own version of it."""
    _reset()
    for s in SEED_EXAMPLES:
        _with_embed(lambda s=s: skills.save_skill(
            s.question, s.commented_sql, headline=s.headline, lesson=s.description,
            created_by="seed", verified=True))
    con = connect()
    con.execute("UPDATE skills SET headline=? WHERE headline=?",
                ("An admin's own wording of this rule.", SEED_EXAMPLES[0].headline))
    con.execute("UPDATE skills SET question=? WHERE question=?",
                ("an admin's own scenario", SEED_EXAMPLES[1].question))
    con.execute("DELETE FROM meta WHERE key=?", (skills._SEED_APPLIED_KEY,))
    con.commit()
    con.close()

    n = _with_embed(lambda: skills.seed_from_schema_examples())

    assert n == 0, f"an admin-edited seed must not be re-inserted, got {n}"
    assert _count(created_by="seed") == len(SEED_EXAMPLES)


def test_backfill_recognizes_a_v1_row_without_the_upgrade_running_first():
    """A database still carrying pre-migration-6 seed rows has NULL headlines,
    so the backfill would miss them if headline were its only match key —
    inserting duplicates beside them. It matches `question` too, which NO
    upgrade path rewrites (migration 6 and upgrade_seed_lessons touch only
    lesson/notes/headline/canonical_sql), so recognition does not depend on
    lifespan running the upgrade first."""
    _reset()
    v1_description, v2 = SEED_LESSON_UPGRADES[0]
    con = connect()
    con.execute(
        "INSERT INTO skills(question, canonical_sql, notes, lesson, headline, "
        "created_by, verified, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (v2.question, "OLD SQL EXAMPLE", v1_description, v1_description, None,
         "seed", 1, 0))
    con.commit()
    con.close()

    n = _with_embed(lambda: skills.seed_from_schema_examples())

    assert n == len(SEED_EXAMPLES) - 1, \
        f"the v1 row's own lesson must not be re-inserted, got {n} inserts"
    con = connect()
    dupes = con.execute(
        "SELECT COUNT(*) FROM skills WHERE question=?", (v2.question,)).fetchone()[0]
    con.close()
    assert dupes == 1, f"the v1 seed was duplicated ({dupes} rows for one lesson)"


def test_seed_slugs_are_unique_and_stable():
    """A slug is a seed's only durable identity across text rewrites; a duplicate
    or empty one silently collapses two lessons into one applied-marker entry."""
    slugs = [s.slug for s in SEED_EXAMPLES]
    assert all(slugs), f"every seed needs a slug: {slugs}"
    assert len(set(slugs)) == len(slugs), f"seed slugs must be unique: {slugs}"
    for k in slugs:
        assert k == k.lower().strip(), f"slug should be lowercase: {k!r}"
        assert " " not in k, f"slug should not contain spaces: {k!r}"


# --- seed/embedding backfills ----------------------------------------------------

def test_upgrade_seed_lessons_upgrades_v1_leaves_admin_edit_alone_idempotent():
    _reset()
    v1_description, v2 = SEED_LESSON_UPGRADES[0]
    con = connect()
    # A row already at v1 (e.g. via migration 6 on a live db before this PR).
    con.execute(
        "INSERT INTO skills(question, canonical_sql, notes, lesson, headline, "
        "created_by, verified, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("v1 row question", "OLD SQL EXAMPLE", v1_description, v1_description, None,
         "seed", 1, 0))
    # An admin-edited seed row whose lesson isn't the frozen v1 text — must be
    # left untouched, same safety convention as migration 6.
    con.execute(
        "INSERT INTO skills(question, canonical_sql, notes, lesson, headline, "
        "created_by, verified, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("edited row question", "SQL", "an admin's own words", "an admin's own words", None,
         "seed", 1, 0))
    con.commit()
    con.close()

    n = skills.upgrade_seed_lessons()
    assert n == 1, f"expected exactly 1 row upgraded, got {n}"

    con = connect()
    upgraded = con.execute(
        "SELECT headline, lesson, canonical_sql FROM skills WHERE question='v1 row question'"
    ).fetchone()
    edited = con.execute(
        "SELECT headline, lesson FROM skills WHERE question='edited row question'").fetchone()
    con.close()
    assert upgraded["headline"] == v2.headline, upgraded["headline"]
    assert upgraded["lesson"] == v2.description, upgraded["lesson"]
    assert upgraded["canonical_sql"] == v2.commented_sql, upgraded["canonical_sql"]
    assert edited["lesson"] == "an admin's own words", "admin-edited seed row must be untouched"
    assert edited["headline"] is None, "admin-edited seed row's headline must be untouched"

    n2 = skills.upgrade_seed_lessons()
    assert n2 == 0, "a second call must be a no-op (the row no longer matches the v1 key)"


def test_reembed_skills_if_needed_stale_marker_reembeds_all_and_advances():
    _reset()
    _with_embed(lambda: skills.save_skill(
        "q1", "SELECT 1", headline="H1", lesson="D1", created_by="seed", verified=True))
    _with_embed(lambda: skills.save_skill(
        "q2", "SELECT 2", headline="H2", lesson="D2", created_by="critic", verified=False))

    n = _with_embed(lambda: skills.reembed_skills_if_needed())
    assert n == 2, n

    con = connect()
    marker = get_meta(con, "skills_embed_source_version")
    rows = con.execute("SELECT headline, lesson, embedding FROM skills ORDER BY id").fetchall()
    con.close()
    assert marker == "2", marker
    for r in rows:
        got = skills._from_blob(r["embedding"])
        want = _fake_embed(skills._embed_source(r["headline"], r["lesson"]))
        assert np.allclose(got, want), (got, want)

    n2 = _with_embed(lambda: skills.reembed_skills_if_needed())
    assert n2 == 0, "a fresh (already-current) marker must make the next call a no-op"


def test_reembed_skills_if_needed_noop_and_marker_unset_when_embed_unavailable():
    _reset()
    _with_embed(lambda: skills.save_skill(
        "q1", "SELECT 1", headline="H1", lesson="D1", created_by="seed", verified=True))

    n = _with_embed(lambda: skills.reembed_skills_if_needed(), embed=lambda _t: None)
    assert n == 0, n
    con = connect()
    marker = get_meta(con, "skills_embed_source_version")
    con.close()
    assert marker is None, \
        "the marker must stay unset when embed() is unavailable, so a later startup retries"


def test_cache_lookup_disabled_when_skills_off():
    orig = skills.get_settings
    skills.get_settings = lambda: type("S", (), {"skills_enabled": False})()
    try:
        assert skills.cache_lookup("anything", 1) is None, \
            "cache must be gated by skills_enabled for a clean A/B baseline"
    finally:
        skills.get_settings = orig


def _cache_row_count(con) -> int:
    return con.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0]


def test_cache_is_scoped_to_the_user_who_asked():
    """THE REGRESSION: cache_lookup had no user predicate, so colleague B asking
    within cache_similarity_threshold of colleague A's question was served A's
    stored answer PROSE verbatim — an invisible flow of one person's question
    phrasing and answer to another, and exactly the attributable leak
    /api/admin/usage refuses to make."""
    if skills._embedder() is None:
        print("    ⚠ fastembed not installed — cache scoping test skipped")
        return
    q = "how many nursing bachelor's degrees were awarded in 2024"
    skills.cache_store(q, "SELECT 1", "ALICE-ONLY-ANSWER", user_id=101)

    hit = skills.cache_lookup(q, 101)
    assert hit and hit["answer_md"] == "ALICE-ONLY-ANSWER", \
        "the author must still get their own cached answer"
    assert skills.cache_lookup(q, 202) is None, \
        "another user was served this user's cached answer text"


def _with_app_version(version):
    """Swap skills.get_settings for one reporting `version`, returning the
    original so the caller can restore it."""
    orig = skills.get_settings
    skills.get_settings = lambda: type("S", (), {"app_version": version})()
    return orig


def _seed_one_cache_row(con):
    con.execute(
        "INSERT INTO query_cache(question, final_sql, answer_md, data_version, "
        "created_at, user_id) VALUES ('q','SELECT 1','stale answer',1,0,1)")
    con.commit()


def test_an_upgrade_wipes_the_answer_cache():
    """THE REGRESSION (found while VERIFYING #326, which is the point): the
    answer cache outlives the code that justified it. `invalidate_cache()` had
    exactly one caller — importer.py, after a data import — so nothing cleared
    the cache when the APP changed. #326 corrected a false award-level rule in
    SCHEMA.md that had produced a wrong total; on a deployment that pulls the
    fix, anyone who asked that question within cache_retention_days (30) and
    0.93 cosine keeps being served the OLD wrong answer verbatim, and the fix
    never reaches them.

    Observed exactly that during verification: the re-asked question replayed
    10,592 with model_used='cache' and no SQL events."""
    con = skills.connect()
    try:
        con.execute("DELETE FROM query_cache")
        set_meta(con, skills._CACHE_VERSION_KEY, "0.3.0")
        con.commit()
        _seed_one_cache_row(con)
        assert _cache_row_count(con) == 1
    finally:
        con.close()

    orig = _with_app_version("0.4.0")
    try:
        n = skills.invalidate_cache_if_version_changed()
    finally:
        skills.get_settings = orig
    assert n == 1, n

    con = skills.connect()
    try:
        assert _cache_row_count(con) == 0, "the stale answer survived the upgrade"
        assert get_meta(con, skills._CACHE_VERSION_KEY) == "0.4.0"
    finally:
        con.close()


def test_the_SAME_version_leaves_the_cache_alone():
    """A restart is not an upgrade. Without this the fix is satisfiable by
    wiping on every boot, which would throw away the cache's whole purpose."""
    con = skills.connect()
    try:
        con.execute("DELETE FROM query_cache")
        set_meta(con, skills._CACHE_VERSION_KEY, "0.4.0")
        con.commit()
        _seed_one_cache_row(con)
    finally:
        con.close()

    orig = _with_app_version("0.4.0")
    try:
        assert skills.invalidate_cache_if_version_changed() == 0
    finally:
        skills.get_settings = orig

    con = skills.connect()
    try:
        assert _cache_row_count(con) == 1, "an ordinary restart discarded the cache"
    finally:
        con.close()


def test_a_database_with_no_marker_is_treated_as_an_upgrade():
    """The load-bearing case, and the one easy to get backwards. Every existing
    deployment reaches this release with NO marker and a populated cache — that
    is precisely the situation the feature exists for. Reading a missing marker
    as 'already current' would make the feature miss its own first release,
    which is the bug seed_from_schema_examples shipped (it bailed whenever the
    table held any row, so new seeds reached fresh installs only)."""
    con = skills.connect()
    try:
        con.execute("DELETE FROM query_cache")
        con.execute("DELETE FROM meta WHERE key=?", (skills._CACHE_VERSION_KEY,))
        con.commit()
        _seed_one_cache_row(con)
    finally:
        con.close()

    orig = _with_app_version("0.4.0")
    try:
        assert skills.invalidate_cache_if_version_changed() == 1
    finally:
        skills.get_settings = orig

    con = skills.connect()
    try:
        assert _cache_row_count(con) == 0
        assert get_meta(con, skills._CACHE_VERSION_KEY) == "0.4.0"
    finally:
        con.close()


def test_cache_round_trips_the_result_rows_that_back_the_answer():
    """THE REGRESSION: query_cache stored the answer but not its RESULTS, so a
    cache hit persisted messages.results=NULL and every LATER turn in that
    conversation had nothing to ground a recited number against — it silently
    graded `unchecked`, denting a rate the project steers by with no visible
    failure anywhere.

    The rows are legitimate evidence for the cached answer: the prose replayed is
    byte-identical to the turn that produced them, so they ARE that answer's
    backing data, not a stand-in for it.
    """
    if skills._embedder() is None:
        print("    ⚠ fastembed not installed — cache results round-trip skipped")
        return
    q = "how many associate degrees in nursing were awarded nationally"
    results = [{"columns": ["year", "awards"], "rows": [[2024, 61234], [2025, 62001]]}]
    skills.cache_store(q, "SELECT 1", "CACHED-ANSWER", None, None,
                       results, True, user_id=404)

    hit = skills.cache_lookup(q, 404)
    assert hit is not None, "the author must get their own cached answer"
    assert hit["results"] == results, \
        f"the answer's backing rows must survive the cache: {hit['results']!r}"
    assert hit["results_truncated"] is True, \
        "a cached truncated result must still report itself truncated"


def test_cache_without_results_reports_none_not_a_crash():
    """A cached turn that retained no rows (or a row written before migration 31)
    must come back as None — the caller persists NULL, which is what it did
    before. The fix must not make the absent case throw."""
    if skills._embedder() is None:
        print("    ⚠ fastembed not installed — cache empty-results test skipped")
        return
    q = "a question whose turn retained no result rows at all"
    skills.cache_store(q, "SELECT 1", "NO-ROWS-ANSWER", user_id=505)

    hit = skills.cache_lookup(q, 505)
    assert hit is not None and hit["results"] is None, hit
    assert hit["results_truncated"] is False, hit


def test_legacy_rows_without_a_user_are_unreachable():
    """Rows written before migration 29 have user_id NULL. They must fail CLOSED
    (reachable by nobody) rather than being treated as shared-by-default."""
    if skills._embedder() is None:
        print("    ⚠ fastembed not installed — legacy cache row test skipped")
        return
    q = "a question cached before the per-user migration"
    skills.cache_store(q, "SELECT 1", "LEGACY-ANSWER", user_id=303)
    con = connect()
    try:
        con.execute("UPDATE query_cache SET user_id=NULL WHERE user_id=303")
        con.commit()
    finally:
        con.close()
    assert skills.cache_lookup(q, 303) is None, "a NULL-user row must match nobody"


def test_cache_store_prunes_past_the_row_cap():
    """THE REGRESSION: the cache had no bound at all — the only DELETE anywhere
    was the wholesale wipe on a data import — while every first-turn question
    vstacks and matmuls the WHOLE table before the agent starts. Latency and
    memory grew with uptime, permanently."""
    if skills._embedder() is None:
        print("    ⚠ fastembed not installed — cache prune test skipped")
        return
    con = connect()
    try:
        con.execute("DELETE FROM query_cache")
        con.commit()
    finally:
        con.close()

    orig = skills.get_settings
    base = orig()

    class _S:
        skills_enabled = True
        cache_similarity_threshold = base.cache_similarity_threshold
        cache_retention_days = 0      # age sweep off; isolate the row cap
        cache_max_rows = 3

    skills.get_settings = lambda: _S()
    try:
        for i in range(6):
            skills.cache_store(f"question number {i}", "SELECT 1", f"answer {i}", user_id=7)
        con = connect()
        try:
            n = _cache_row_count(con)
            newest = con.execute(
                "SELECT answer_md FROM query_cache ORDER BY id DESC LIMIT 1").fetchone()[0]
        finally:
            con.close()
        assert n == 3, f"row cap not enforced: {n} rows remain"
        assert newest == "answer 5", "the cap dropped the NEWEST rows instead of the oldest"
    finally:
        skills.get_settings = orig


def test_a_non_positive_cap_disables_the_sweep():
    """Matches the log_retention_days / log_max_rows convention."""
    if skills._embedder() is None:
        print("    ⚠ fastembed not installed — cache prune-off test skipped")
        return
    con = connect()
    try:
        con.execute("DELETE FROM query_cache")
        con.commit()
    finally:
        con.close()

    orig = skills.get_settings
    base = orig()

    class _S:
        skills_enabled = True
        cache_similarity_threshold = base.cache_similarity_threshold
        cache_retention_days = 0
        cache_max_rows = 0

    skills.get_settings = lambda: _S()
    try:
        for i in range(4):
            skills.cache_store(f"unbounded question {i}", "SELECT 1", f"a{i}", user_id=7)
        con = connect()
        try:
            assert _cache_row_count(con) == 4, "a non-positive cap must disable the sweep"
        finally:
            con.close()
    finally:
        skills.get_settings = orig


# ---------------------------------------------------------------------------
# A2: lesson-rejection memory (migration 35: skills.category + lesson_rejections)
#
# Two gaps A1 left open: rejecting a lesson is a hard DELETE with no trace, so
# _find_duplicate can never suppress the same proposal recurring; and dedup is
# scoped to (verified=0, same source) ONLY, so a candidate near-identical to an
# already-VERIFIED lesson, or one queued from a DIFFERENT source, is saved as a
# fresh duplicate. This block pins the NEW surface:
#   skills.muted_categories(con) / skills.set_category_muted(token, muted)
#   skills._find_suppressor(con, qvec, headline, description, source) -- the
#     COMPLEMENT of _find_duplicate's scope: verified=1 OR a DIFFERENT source
#     (IFNULL-safe against a nullable created_by)
#   a tombstone check in _upvote_or_save, run BEFORE _find_duplicate's upvote
#     check (or a rejected idea would inflate an existing row's upvotes) and
#     AFTER it (or a same-source queued row would be silently dropped instead
#     of upvoted, destroying the recurrence signal the queue depends on)
#   category threaded through save_skill/_upvote_or_save, backfilled onto a
#     NULL-category upvote target, never overwriting an already-set one
#
# Uses check_pending (not check): this surface doesn't exist yet (TDD red).
# ---------------------------------------------------------------------------

def test_tombstone_precedes_upvote_check_in_ordering():
    """THE ORDERING REGRESSION: the tombstone check (step 3) must run BEFORE
    _find_duplicate's same-source upvote check (step 4). Both a matching
    tombstone AND a matching same-source unverified row are seeded here, so a
    version that checks upvote-eligibility first would upvote the existing row
    instead of dropping the candidate outright -- asserting on `upvotes`, not
    just row count, is what catches that (a count-only assertion passes on the
    buggy ordering too, since neither ordering inserts a new row)."""
    _reset()
    _clear_rejections()
    q = "national total ordering probe"
    headline = "Add majornum=1 for every completions total."
    description = "no majornum=1 filter — double-counts second majors"
    _with_embed(lambda: skills.save_skill(
        q, "SELECT 1", headline=headline, lesson=description,
        created_by="critic", verified=False))
    _insert_tombstone(headline, description, created_by="critic")

    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 1", headline, description))

    assert _count() == 1, "nothing new must be inserted when a tombstone matches"
    con = connect()
    up = con.execute("SELECT upvotes FROM skills WHERE created_by='critic'").fetchone()[0]
    con.close()
    assert up == 0, \
        f"a tombstoned candidate must NOT inflate the existing pending row's upvotes, got {up}"


def test_same_source_unverified_near_duplicate_still_upvotes():
    """Scoping-preserved check: with the widened _find_suppressor now also live,
    a same-source unverified near-duplicate (the ordinary repeat-finding case)
    must still upvote, not get caught by the widened predicate. Catches a
    too-wide suppressor that fails to exclude the candidate's own source."""
    _reset()
    _clear_rejections()
    q = "same source still upvotes probe"
    headline = "Filter awlevel to real codes, not rollups."
    description = "award-level rollup mixing — filter awlevel to real codes only"
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 1", headline, description))
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 1", headline, description))
    assert _count() == 1, "an identical same-source repeat must still upvote, not duplicate"
    con = connect()
    up = con.execute("SELECT upvotes FROM skills WHERE created_by='critic'").fetchone()[0]
    con.close()
    assert up == 1, f"expected exactly 1 upvote on the deduped row, got {up}"


def test_suppression_against_verified_lesson_no_insert_no_upvote():
    """THE HIGH-VALUE dedup-scoping widening: a candidate near-identical to an
    already-VERIFIED lesson must be suppressed outright -- no new row, and the
    verified row's upvotes must NOT be inflated (upvoting a verified/curated row
    from an unreviewed candidate would corrupt the admin's ranking signal, the
    same reason _find_duplicate never touched verified rows either)."""
    _reset()
    _clear_rejections()
    q = "verified suppression probe"
    headline = "Use cipcode='99' for national totals."
    description = "the '99' row is already the grand total; never sum leaf codes"
    _with_embed(lambda: skills.save_skill(
        q, "SELECT 1", headline=headline, lesson=description,
        created_by="seed", verified=True))
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 2", headline, description))
    assert _count() == 1, \
        "a near-identical VERIFIED lesson must suppress a new duplicate, not insert one"
    con = connect()
    up = con.execute("SELECT upvotes FROM skills WHERE created_by='seed'").fetchone()[0]
    con.close()
    assert up == 0, f"suppression must not inflate the verified lesson's upvotes, got {up}"


def test_feedback_candidate_suppressed_against_an_already_verified_lesson():
    """The VERIFIED arm of _find_suppressor applies to EVERY source, not just
    critic -- an already-approved lesson is already active in the retrieval
    prompt, so a feedback candidate restating it adds nothing whichever
    pipeline happens to notice it second. This is deliberately asymmetric with
    the DIFFERENT-SOURCE-PENDING arm, which stays critic-only (see
    test_feedback_lesson_not_collapsed_into_a_critic_row_same_scenario): a
    user's own corrective feedback and the model's self-critique on the SAME
    scenario are DIFFERENT evidence, and the review queue should surface both
    for an admin to weigh -- unlike an already-verified rule, which needs no
    second vote to become effective.

    Uses an IDENTICAL rule against the verified row (unlike
    test_feedback_lesson_not_collapsed_into_verified_seed's DIFFERENT rule,
    which never exercises this arm at all -- that test stays green precisely
    because nothing here should change its outcome)."""
    _reset()
    _clear_rejections()
    q = "national total associate degrees per year"
    headline = "Use cipcode='99'."
    description = "use the grand-total row"
    _with_embed(lambda: skills.save_skill(
        q, "SELECT 1", headline=headline, lesson=description,
        created_by="seed", verified=True))
    _with_embed(lambda: skills.record_lesson_from_feedback(q, headline, description))
    assert _count() == 1, \
        "a feedback candidate near-identical to an already-verified lesson must " \
        "suppress, not insert a duplicate"
    con = connect()
    up = con.execute("SELECT upvotes FROM skills WHERE created_by='seed'").fetchone()[0]
    con.close()
    assert up == 0, f"suppression must not inflate the verified lesson's upvotes, got {up}"


def test_suppression_against_different_source_unverified_row():
    """The other half of the widening: a candidate near-identical to an
    UNVERIFIED row from a DIFFERENT source (a real, non-empty rule -- unlike
    test_dedup_is_scoped_to_same_source's empty-rule row, which has no
    embedding and so is unreachable by any cosine check) must also suppress."""
    _reset()
    _clear_rejections()
    q = "cross source suppression probe"
    headline = "Join hd on unitid and year."
    description = "match state and control filters to the correct collection year"
    _with_embed(lambda: skills.save_skill(
        q, "SELECT 1", headline=headline, lesson=description,
        created_by="user-feedback", verified=False))
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 2", headline, description))
    assert _count() == 1, \
        "a near-identical row from a DIFFERENT source must suppress, not duplicate"
    con = connect()
    up = con.execute(
        "SELECT upvotes FROM skills WHERE created_by='user-feedback'").fetchone()[0]
    con.close()
    assert up == 0, f"suppression must not inflate the different-source row's upvotes, got {up}"


def test_muted_learnable_category_blocks_recording_then_unmute_restores_it():
    _reset()
    _clear_rejections()
    _clear_muted_categories()
    q = "muted category probe"
    headline = "Add majornum=1 for every completions total."
    description = "no majornum=1 filter — double-counts second majors"

    skills.set_category_muted("SECOND_MAJOR", True)
    con = connect()
    muted = skills.muted_categories(con)
    con.close()
    assert "SECOND_MAJOR" in muted, muted

    _with_embed(lambda: skills.record_lesson_from_critic(
        q, "SELECT 1", headline, description, category="SECOND_MAJOR"))
    assert _count() == 0, "a muted learnable category must record nothing"

    skills.set_category_muted("SECOND_MAJOR", False)
    con = connect()
    muted_after = skills.muted_categories(con)
    con.close()
    assert "SECOND_MAJOR" not in muted_after, muted_after

    _with_embed(lambda: skills.record_lesson_from_critic(
        q, "SELECT 1", headline, description, category="SECOND_MAJOR"))
    assert _count() == 1, "unmuting must restore recording for that category"


def test_feedback_candidate_with_no_category_still_records():
    """Proves the muted-category gate lives ONLY in record_lesson_from_critic:
    the feedback distiller never carries a category (feedback rows stay NULL
    per spec) and must be unaffected by anything muted. Deliberately does not
    touch lesson_rejections (migration 35) at all -- this must keep passing
    unmodified whether or not that table exists yet."""
    _reset()
    _with_embed(lambda: skills.record_lesson_from_feedback(
        "a feedback scenario the mute gate must not touch",
        "Ask a clarifying question before assuming an award-level scope.",
        "when a request doesn't specify an award level, ask instead of silently "
        "assuming bachelor's-only"))
    assert _count() == 1, "a feedback candidate (no category) must still record"


def test_tombstone_fallback_exact_text_without_embeddings():
    _reset()
    _clear_rejections()
    def _no_embed(_t):
        return None
    headline = "Filter to an exact leaf CIP code."
    description = "never sum rollup rows together with the leaf level"
    _insert_tombstone(headline, description, created_by="critic", embed_fn=_no_embed)

    _with_embed(lambda: skills.record_lesson_from_critic(
        "exact fallback probe", "SELECT 1", headline, description), embed=_no_embed)
    assert _count() == 0, "an exact-text tombstone match must suppress without embeddings"

    _with_embed(lambda: skills.record_lesson_from_critic(
        "exact fallback probe two", "SELECT 2", "A completely different headline.",
        "a completely different rule text, sharing nothing with the tombstone"),
        embed=_no_embed)
    assert _count() == 1, "a non-matching candidate must still insert when embeddings are off"


def test_a_null_embedding_tombstone_still_suppresses_once_embeddings_recover():
    """THE PERMANENTLY-INVISIBLE TOMBSTONE.

    The exact-text match used to be the `else` of the vector arm, so it only
    ran when the CANDIDATE could not be embedded. A tombstone written while
    fastembed was down has embedding = NULL (delete_skill reuses the skill's
    stored vector, and re-embedding returns None for the same reason it was
    NULL), and the vector arm filters those rows out with
    `WHERE embedding IS NOT NULL`. So the moment embeddings recovered, that
    rejection became invisible and the identical lesson re-queued forever —
    the exact failure lesson_rejections exists to end, silent in both
    directions.

    Sequence, which is the only way to reach it: reject with embeddings DOWN,
    then propose the same lesson with embeddings UP."""
    _reset()
    _clear_rejections()
    headline = "Bound a recent-years filter with a constant, never a join."
    description = ("a DISTINCT-year subquery joined against c_a full-scans "
                   "the whole table and effectively hangs")

    # Rejected while embeddings were unavailable -> tombstone has no vector.
    _insert_tombstone(headline, description, created_by="critic",
                      embed_fn=lambda _t: None)
    con = connect()
    got = con.execute(
        "SELECT embedding FROM lesson_rejections WHERE headline=?",
        (headline,)).fetchone()
    con.close()
    assert got is not None and got["embedding"] is None, \
        "fixture must produce a NULL-embedding tombstone or this proves nothing"

    # ...and now embeddings work again. The same idea must still be suppressed.
    _with_embed(lambda: skills.record_lesson_from_critic(
        "null-embedding tombstone probe", "SELECT 1", headline, description))
    assert _count() == 0, \
        "a tombstone written without an embedding must still suppress once " \
        "embeddings recover — otherwise the rejection is permanent-invisible"


def test_tombstone_dimension_mismatch_is_skipped_not_fatal():
    """Reuses _find_duplicate's skip-on-dimension-mismatch guard: a tombstone
    embedded under a stale/different embed_model must never crash the dot
    product, only be treated as non-matching."""
    _reset()
    _clear_rejections()
    con = connect()
    con.execute(
        "INSERT INTO lesson_rejections(headline, description, embedding, category, "
        "created_by, skill_id, was_verified, hits, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("stale-dimension headline", "stale-dimension description",
         np.zeros(3, dtype=np.float32).tobytes(), None, "critic", None, 0, 0, time.time()))
    con.commit()
    con.close()

    _with_embed(lambda: skills.record_lesson_from_critic(
        "dimension mismatch probe", "SELECT 1", "A real headline.",
        "a real description that shares nothing with the stale tombstone"))
    assert _count() == 1, \
        "a dimension-mismatched tombstone must be skipped, not crash or wrongly suppress"


def test_suppression_reaches_a_null_created_by_row():
    """THE IFNULL REGRESSION: created_by is nullable, and SQL's `NULL != 'critic'`
    evaluates to NULL (not true) in a WHERE clause, which reads as false --
    silently excluding every NULL-source row from suppression without an
    explicit IFNULL wrap. Seeds a NULL-created_by row that a critic candidate is
    near-identical to; it must still be suppressed."""
    _reset()
    _clear_rejections()
    q = "null source suppression probe"
    headline = "Express recent years as a constant bound."
    description = "year > (SELECT MAX(year)-3 FROM _years), never a DISTINCT year join"
    con = connect()
    v = _fake_embed(skills._embed_source(headline, description))
    con.execute(
        "INSERT INTO skills(question, canonical_sql, headline, lesson, embedding, "
        "verified, created_by, created_at) VALUES (?,?,?,?,?,0,NULL,?)",
        (q, "SELECT 1", headline, description, skills._to_blob(v), time.time()))
    con.commit()
    con.close()

    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 2", headline, description))
    assert _count() == 1, \
        "a NULL created_by row must still be reachable by suppression (IFNULL check)"
    con = connect()
    up = con.execute("SELECT upvotes FROM skills WHERE created_by IS NULL").fetchone()[0]
    con.close()
    assert up == 0, "suppression must not upvote the NULL-source row either"


def test_category_stored_on_save():
    _reset()
    _with_embed(lambda: skills.save_skill(
        "category storage probe", "SELECT 1", headline="H", lesson="L",
        created_by="critic", verified=False, category="MAGNITUDE"))
    con = connect()
    cat = con.execute("SELECT category FROM skills").fetchone()[0]
    con.close()
    assert cat == "MAGNITUDE", cat


def test_category_backfilled_onto_null_target_never_overwritten():
    _reset()
    _clear_rejections()
    q = "category backfill probe"
    headline = "Award-level mixing: filter to real codes."
    description = "awlevel rollup codes summed together with real levels"
    # First candidate carries no category (mirrors an older call site / a
    # feedback-sourced row upvoting a critic row's scenario).
    _with_embed(lambda: skills.record_lesson_from_critic(q, "SELECT 1", headline, description))
    con = connect()
    row = con.execute("SELECT category FROM skills WHERE created_by='critic'").fetchone()
    con.close()
    assert row["category"] is None, row["category"]

    # A repeat WITH a category upvotes the same row and backfills its category.
    _with_embed(lambda: skills.record_lesson_from_critic(
        q, "SELECT 1", headline, description, category="AWARD_LEVEL"))
    con = connect()
    row2 = con.execute(
        "SELECT category, upvotes FROM skills WHERE created_by='critic'").fetchone()
    con.close()
    assert row2["category"] == "AWARD_LEVEL", row2["category"]
    assert row2["upvotes"] == 1, row2["upvotes"]
    assert _count() == 1, "must upvote the same row, not insert a second one"

    # A THIRD repeat carrying a DIFFERENT category must NOT overwrite the
    # now-set one.
    _with_embed(lambda: skills.record_lesson_from_critic(
        q, "SELECT 1", headline, description, category="MAGNITUDE"))
    con = connect()
    row3 = con.execute("SELECT category FROM skills WHERE created_by='critic'").fetchone()
    con.close()
    assert row3["category"] == "AWARD_LEVEL", \
        f"an existing category must never be overwritten, got {row3['category']}"


def test_muted_categories_corrupt_json_fails_open():
    """Mirrors skills._applied_seed_keys' fail-open convention: a corrupt marker
    must re-queue (never permanently silence) — reading it as "everything is
    muted" would be the wrong failure direction for an admin-visible control."""
    con = connect()
    from app.db import set_meta
    set_meta(con, "muted_lesson_categories", "{not valid json[")
    con.commit()
    result = skills.muted_categories(con)
    con.close()
    assert result == set(), \
        f"corrupt muted-categories JSON must fail OPEN (empty set), got {result}"


def test_muted_category_suppression_logs_the_reason():
    """Suppression is invisible by construction (no row appears anywhere), so
    this INFO line is the only way an admin can ever learn the feature is
    over-reaching -- worth pinning on its own. Production code must not
    configure logging just to make that observable: app/skills.py sets no
    logger level of its own (the one operator-overriding `setLevel` call that
    used to exist here was removed), so `skills.log`'s EFFECTIVE level in this
    standalone script is WARNING (nothing configures the root logger either) —
    an attached handler alone would never see an INFO record, since Python's
    logging filters at the logger level before a record ever reaches a
    handler. The test sets the level itself, scoped to its own try/finally, so
    it stays correct regardless of whatever the ambient logging config is."""
    _reset()
    _clear_rejections()
    _clear_muted_categories()
    skills.set_category_muted("QUESTION_MISMATCH", True)
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = _H()
    orig_level = skills.log.level
    skills.log.setLevel(logging.INFO)
    skills.log.addHandler(h)
    try:
        _with_embed(lambda: skills.record_lesson_from_critic(
            "muted logging probe", "SELECT 1", "H", "a rule",
            category="QUESTION_MISMATCH"))
    finally:
        skills.log.removeHandler(h)
        skills.log.setLevel(orig_level)
        skills.set_category_muted("QUESTION_MISMATCH", False)
    assert any("muted-category" in m for m in records), records


def run():
    print("self-learning lessons:")
    check("_lesson_text leads with headline, then description, then SQL",
          test_lesson_text_leads_with_headline_then_description_then_sql)
    check("_lesson_text never echoes the question", test_lesson_text_no_question_echo)
    check("_lesson_text falls back to LESSON: description with no headline",
          test_lesson_text_no_headline_falls_back_to_lesson_prefixed)
    check("_lesson_text falls back to notes when lesson is empty",
          test_lesson_text_falls_back_to_notes_when_lesson_empty)
    check("_lesson_text returns '' when everything is empty",
          test_lesson_text_all_empty_is_empty_string)
    check("_embed_source is headline + newline + description",
          test_embed_source_is_headline_newline_description)
    check("_embed_source strips outer whitespace", test_embed_source_strips_outer_whitespace)
    check("_embed_source of two empty strings is ''", test_embed_source_empty_both_is_empty_string)
    check("retrieval leads with the headline", test_retrieve_leads_with_headline)
    check("retrieval is empty when skills disabled", test_retrieve_disabled_returns_empty)
    check("retrieval is a no-op without embeddings", test_retrieve_without_embeddings_is_noop)
    check("unverified lessons are never retrieved", test_unverified_lessons_are_not_retrieved)
    check("critic finding dedups a TRUE repeat (identical rule) via embedding",
          test_critic_dedups_a_true_repeat_via_embedding)
    check("a distinct rule on the same (question, SQL) is NOT deduped (embeddings on)",
          test_critic_distinct_rule_same_scenario_is_not_deduped)
    check("distinct scenarios each insert a row", test_distinct_scenario_inserts_new_row)
    check("exact-match dedup without embeddings", test_exact_match_dedup_without_embeddings)
    check("critic-emitted lesson is unverified, headline + description populated",
          test_record_lesson_from_critic_is_unverified_with_headline_and_description)
    check("blank headline AND description is a no-op", test_record_lesson_both_blank_is_noop)
    check("a headline alone is NOT a no-op", test_record_lesson_headline_only_is_not_noop)
    check("distinct critic rule not collapsed into a verified seed",
          test_critic_lesson_not_collapsed_into_verified_seed)
    check("record_lesson_from_feedback is unverified, created_by='user-feedback'",
          test_record_lesson_from_feedback_is_unverified_with_user_feedback_source)
    check("record_lesson_from_feedback: blank headline AND description is a no-op",
          test_record_lesson_from_feedback_both_blank_is_noop)
    check("feedback finding dedups a TRUE repeat (identical rule) via embedding",
          test_feedback_dedups_a_true_repeat_via_embedding)
    check("a feedback lesson is not collapsed into a critic row on the same scenario",
          test_feedback_lesson_not_collapsed_into_a_critic_row_same_scenario)
    check("a feedback lesson is not collapsed into a verified seed",
          test_feedback_lesson_not_collapsed_into_verified_seed)
    check("dedup is scoped to the same source", test_dedup_is_scoped_to_same_source)
    check("dedup backfills an empty headline + lesson (exact match, no embeddings)",
          test_dedup_backfills_empty_headline_and_lesson_without_embeddings)
    check("seed lessons have a headline + readable description + commented SQL",
          test_seed_lessons_have_headline_and_readable_description)
    check("SEED_LESSON_UPGRADES targets match SEED_EXAMPLES (drift guard)",
          test_seed_lesson_upgrades_consistent_with_seed_examples)
    check("SEED_LESSON_REWRITES + upgrade match keys are frozen literals",
          test_seed_lesson_rewrites_are_frozen_literals)
    check("save_skill embeds headline+description, never the question",
          test_save_skill_embeds_headline_and_description_not_question)
    check("save_skill stores a NULL embedding when headline+lesson are empty",
          test_save_skill_null_embedding_when_headline_and_lesson_both_empty)
    check("seed_from_schema_examples inserts verified seed rows (headline+embedding)",
          test_seed_from_schema_examples_inserts_verified_seed_rows)
    check("seed_from_schema_examples is idempotent",
          test_seed_from_schema_examples_is_idempotent)
    check("a seed added in a later release reaches an upgraded deployment",
          test_seeding_ships_a_lesson_added_in_a_later_release)
    check("an already-seeded db gains nothing and records the backfill",
          test_seeding_an_already_seeded_db_adds_nothing_and_records_the_backfill)
    check("a deleted seed is not resurrected on the next boot",
          test_a_deleted_seed_is_not_resurrected_on_the_next_boot)
    check("the backfill matches a seed row an admin edited",
          test_backfill_matches_a_seed_row_an_admin_edited)
    check("the backfill recognizes a v1 row without the upgrade running first",
          test_backfill_recognizes_a_v1_row_without_the_upgrade_running_first)
    check("seed slugs are unique and non-empty", test_seed_slugs_are_unique_and_stable)
    check("upgrade_seed_lessons: v1->v2, admin-edit-safe, idempotent",
          test_upgrade_seed_lessons_upgrades_v1_leaves_admin_edit_alone_idempotent)
    check("reembed_skills_if_needed: stale marker re-embeds all rows + advances",
          test_reembed_skills_if_needed_stale_marker_reembeds_all_and_advances)
    check("reembed_skills_if_needed: embed() unavailable -> no-op, marker unset",
          test_reembed_skills_if_needed_noop_and_marker_unset_when_embed_unavailable)
    check("cache lookup is gated by skills_enabled", test_cache_lookup_disabled_when_skills_off)
    check("the cache is scoped to the user who asked", test_cache_is_scoped_to_the_user_who_asked)
    check("legacy NULL-user rows are unreachable", test_legacy_rows_without_a_user_are_unreachable)
    check("the cache round-trips the rows backing its answer",
          test_cache_round_trips_the_result_rows_that_back_the_answer)
    check("a cached turn with no rows reports none, not a crash",
          test_cache_without_results_reports_none_not_a_crash)
    check("cache_store prunes past the row cap", test_cache_store_prunes_past_the_row_cap)
    check("a non-positive cap disables the sweep", test_a_non_positive_cap_disables_the_sweep)

    print("A2: lesson-rejection memory (migration 35 -- not yet implemented):")
    check_pending("tombstone check precedes the same-source upvote check (ordering)",
                  test_tombstone_precedes_upvote_check_in_ordering)
    check_pending("a same-source unverified near-duplicate still upvotes (scoping preserved)",
                  test_same_source_unverified_near_duplicate_still_upvotes)
    check_pending("suppression against an already-verified near-identical lesson",
                  test_suppression_against_verified_lesson_no_insert_no_upvote)
    check_pending("a feedback candidate is suppressed against an already-verified lesson too",
                  test_feedback_candidate_suppressed_against_an_already_verified_lesson)
    check_pending("suppression against a different-source unverified row",
                  test_suppression_against_different_source_unverified_row)
    check_pending("a muted learnable category records nothing; unmuting restores it",
                  test_muted_learnable_category_blocks_recording_then_unmute_restores_it)
    check("a feedback candidate (no category) still records",
          test_feedback_candidate_with_no_category_still_records)
    check_pending("tombstone suppression falls back to exact text without embeddings",
                  test_tombstone_fallback_exact_text_without_embeddings)
    check_pending("a NULL-embedding tombstone suppresses once embeddings recover",
                  test_a_null_embedding_tombstone_still_suppresses_once_embeddings_recover)
    check_pending("a dimension-mismatched tombstone is skipped, not fatal",
                  test_tombstone_dimension_mismatch_is_skipped_not_fatal)
    check_pending("suppression reaches a NULL created_by row (IFNULL)",
                  test_suppression_reaches_a_null_created_by_row)
    check_pending("category is stored on save", test_category_stored_on_save)
    check_pending("category backfills onto a NULL target, never overwrites a set one",
                  test_category_backfilled_onto_null_target_never_overwritten)
    check_pending("muted_categories fails open on corrupt JSON",
                  test_muted_categories_corrupt_json_fails_open)
    check_pending("a muted-category suppression logs the reason",
                  test_muted_category_suppression_logs_the_reason)
    check("an upgrade wipes the answer cache",
          test_an_upgrade_wipes_the_answer_cache)
    check("the same version leaves the cache alone",
          test_the_SAME_version_leaves_the_cache_alone)
    check("a database with no marker is treated as an upgrade",
          test_a_database_with_no_marker_is_treated_as_an_upgrade)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} lesson test(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL SKILLS/LESSON TESTS PASSED")


if __name__ == "__main__":
    run()
