"""Self-learning "lessons" (backend/app/skills.py): a lesson is a short HEADLINE + a
longer generalized DESCRIPTION + a commented SQL worked example. The critic is
the sole lesson source (thumbs-up feedback was removed); saves dedup against
near-duplicates; retrieval leads with the headline and is gated by the
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
from app.db import connect, get_meta, init_db  # noqa: E402
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


def _reset():
    con = connect()
    con.execute("DELETE FROM skills")
    con.execute("DELETE FROM meta WHERE key='skills_embed_source_version'")
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
    assert len(SEED_EXAMPLES) == 3, len(SEED_EXAMPLES)
    for ex in SEED_EXAMPLES:
        assert ex.headline, f"seed missing a headline: {ex!r}"
        assert len(ex.headline) <= 110, f"headline should be short: {ex.headline!r}"
        assert len(ex.description) >= 80, \
            f"description too short to be a generalized sentence: {ex.description!r}"
        assert ex.description.endswith("."), \
            f"description should end with a period: {ex.description!r}"
        assert any(w.islower() and len(w) > 2 for w in ex.description.split()), \
            f"description doesn't read as prose: {ex.description!r}"
        assert "--" in ex.commented_sql, \
            f"commented_sql must carry inline comments explaining the fields: {ex.commented_sql!r}"


def test_seed_lesson_upgrades_consistent_with_seed_examples():
    # Drift guard: every SEED_LESSON_UPGRADES target must be the SAME SeedLesson
    # object shipped in SEED_EXAMPLES, or a future edit to one without the other
    # would desync a fresh install's seeds from an upgraded live db.
    assert len(SEED_LESSON_UPGRADES) == len(SEED_EXAMPLES), len(SEED_LESSON_UPGRADES)
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
    # by migration 6 (Todd's live db) is exactly what upgrade_seed_lessons() matches.
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


def test_seed_from_schema_examples_is_noop_when_table_not_empty():
    _reset()
    _with_embed(lambda: skills.seed_from_schema_examples())
    n = _with_embed(lambda: skills.seed_from_schema_examples())  # table is no longer empty
    assert n == 0, "seeding must only ever run once, on an empty table"
    assert _count() == len(SEED_EXAMPLES), "a second seed call must not add rows"


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
        assert skills.cache_lookup("anything") is None, \
            "cache must be gated by skills_enabled for a clean A/B baseline"
    finally:
        skills.get_settings = orig


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
    check("seed_from_schema_examples is a no-op when the table isn't empty",
          test_seed_from_schema_examples_is_noop_when_table_not_empty)
    check("upgrade_seed_lessons: v1->v2, admin-edit-safe, idempotent",
          test_upgrade_seed_lessons_upgrades_v1_leaves_admin_edit_alone_idempotent)
    check("reembed_skills_if_needed: stale marker re-embeds all rows + advances",
          test_reembed_skills_if_needed_stale_marker_reembeds_all_and_advances)
    check("reembed_skills_if_needed: embed() unavailable -> no-op, marker unset",
          test_reembed_skills_if_needed_noop_and_marker_unset_when_embed_unavailable)
    check("cache lookup is gated by skills_enabled", test_cache_lookup_disabled_when_skills_off)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} lesson test(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL SKILLS/LESSON TESTS PASSED")


if __name__ == "__main__":
    run()
