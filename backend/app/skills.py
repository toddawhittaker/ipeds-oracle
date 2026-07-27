"""Self-learning: a library of LESSONS retrieved as guidance, plus a semantic
answer cache.

A "lesson" is a short generalized HEADLINE (the rule title) + a longer
generalized DESCRIPTION (the transferable technique, explained in plain prose)
+ an OPTIONAL commented SQL worked example. The critic is NO LONGER the sole
lesson source: TWO sources feed the same unverified pool. The post-answer
critic (app.critic) mines the MODEL's own mistakes — when it catches a real
one, it phrases the fix as a headline + description, captured via
`record_lesson_from_critic`. The feedback distiller (app.feedback) mines the
USER's corrective feedback on a follow-up turn ("you should have kept the
bachelor's scope", "you could have asked me a clarifying question") the same
way, captured via `record_lesson_from_feedback`. Both land as an UNVERIFIED
lesson pending admin approval before either is ever retrieved. (A 👍/👎
feedback path used to exist but was removed — a "like" is a weak signal, not a
reusable rule; this is a different, generalized-rule-shaped feedback path.)

Retrieval embeds the incoming question and returns the lessons attached to the
most similar past scenarios (ranked against each lesson's headline+description
vector), deduped so near-identical lessons don't crowd the few-shot slots.
Embeddings run locally via fastembed (CPU, no per-call cost); if fastembed
isn't installed, retrieval/dedup degrade gracefully (no-op retrieval,
exact-match dedup) so the app still runs.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

import numpy as np

from app.config import get_settings
from app.db import connect, data_version, get_meta, set_meta
from app.seeds import SEED_EXAMPLES, SEED_LESSON_UPGRADES

log = logging.getLogger("ipeds.skills")
_model = None
_embed_ok = True

# Bumped whenever the embedding SOURCE convention changes (e.g. question ->
# headline+description), so `reembed_skills_if_needed` knows to recompute
# every stored vector once, at startup.
_EMBED_SOURCE_VERSION = "2"

# `meta` key holding the JSON list of SeedLesson slugs this database has been
# given, so `seed_from_schema_examples` ships each shipped lesson exactly once
# — including ones added in a release the deployment upgraded INTO.
_SEED_APPLIED_KEY = "seed_lessons_applied"


def _embedder():
    global _model, _embed_ok
    if _model is None and _embed_ok:
        try:
            from fastembed import TextEmbedding
            _model = TextEmbedding(model_name=get_settings().embed_model)
            log.info("loaded embedding model %s", get_settings().embed_model)
        except Exception as e:  # noqa: BLE001 — optional dependency
            _embed_ok = False
            log.warning("embeddings unavailable (%s); skills/cache disabled", e)
    return _model


def embed(text: str) -> np.ndarray | None:
    m = _embedder()
    if m is None:
        return None
    vec = next(iter(m.embed([text])))
    v = np.asarray(vec, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


def _to_blob(v: np.ndarray) -> bytes:
    return v.astype(np.float32).tobytes()


def _from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


def _cosine(q: np.ndarray, mat: np.ndarray) -> np.ndarray:
    # q and rows of mat are already L2-normalized
    return mat @ q


def _embed_source(headline: str, description: str) -> str:
    """The text actually embedded for a lesson: headline + description — NEVER
    the question. Used on every write, dedup lookup, and re-embed pass, so
    retrieval ranks on the RULE, not on how one past user happened to phrase
    their question."""
    return f"{headline or ''}\n{description or ''}".strip()


# --- Skill retrieval (few-shot) ------------------------------------------------

def _lesson_text(row) -> str:
    """One retrieved lesson: HEADLINE, then the description (lesson/notes),
    then an optional commented SQL worked example. Returns '' when everything
    is empty."""
    headline = (row["headline"] or "").strip()
    description = (row["lesson"] or row["notes"] or "").strip()
    sql = (row["canonical_sql"] or "").strip()
    parts = []
    if headline:
        parts.append(f"LESSON: {headline}")
        if description:
            parts.append(description)
    elif description:
        parts.append(f"LESSON: {description}")
    if sql:
        parts.append(f"SQL (inline comments explain each field):\n{sql}")
    return "\n".join(parts)


def retrieve_skills_block(question: str) -> tuple[str, list[int]]:
    """Return (guidance text, skill_ids) — the lessons attached to the verified
    scenarios most similar to `question`. Empty when disabled, unconfigured
    (no embeddings), or nothing clears the similarity floor."""
    s = get_settings()
    if not s.skills_enabled:
        return "", []
    q = embed(question)
    if q is None:
        return "", []
    con = connect()
    try:
        rows = con.execute(
            "SELECT id, question, canonical_sql, notes, lesson, headline, embedding "
            "FROM skills WHERE verified=1 AND embedding IS NOT NULL").fetchall()
    finally:
        con.close()
    if not rows:
        return "", []
    mat = np.vstack([_from_blob(r["embedding"]) for r in rows])
    sims = _cosine(q, mat)
    order = np.argsort(-sims)[: s.skill_retrieve_k]
    picked = [(rows[i], float(sims[i])) for i in order if sims[i] >= s.skill_similarity_floor]
    if not picked:
        return "", []
    blocks, ids = [], []
    for r, _sim in picked:
        text = _lesson_text(r)
        if text:
            ids.append(r["id"])
            blocks.append(text)
    return "\n\n".join(blocks), ids


def bump_hits(skill_ids: list[int]) -> None:
    if not skill_ids:
        return
    con = connect()
    try:
        con.executemany("UPDATE skills SET hits=hits+1 WHERE id=?",
                        [(i,) for i in skill_ids])
        con.commit()
    finally:
        con.close()


# --- Skill authoring -----------------------------------------------------------

def save_skill(question: str, canonical_sql: str, *, headline: str = "",
               notes: str = "", lesson: str = "", created_by: str = "system",
               verified: bool = False, tags: str = "") -> int:
    source = _embed_source(headline, lesson)
    v = embed(source) if source else None
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO skills(question, canonical_sql, notes, lesson, headline, "
            "embedding, tags, verified, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (question, canonical_sql, notes, lesson, headline or None,
             _to_blob(v) if v is not None else None,
             tags, int(verified), created_by, time.time()))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _find_duplicate(con, qvec: np.ndarray | None, question: str,
                    canonical_sql: str, source: str) -> int | None:
    """Id of an UNVERIFIED lesson from the SAME source to upvote instead of
    inserting, else None.

    Restricting the search to (verified=0, same created_by) is deliberate: a new
    pending candidate must never collapse into — or inflate the upvotes of — an
    already-APPROVED lesson or one from a DIFFERENT source. Without that filter, a
    critic-discovered rule (say, award-level mixing) whose *question* is similar
    to a verified seed (about CIP '99') would be silently discarded and the seed
    spuriously upvoted, corrupting the admin's ranking signal. Prefers embedding
    cosine over the same scenario (the headline+description vector); falls back
    to an exact (question, SQL) match ONLY when embeddings are unavailable
    system-wide (qvec is None) — a true repeat still dedups via cosine
    (identical headline+description → similarity ~1.0), while two genuinely
    distinct rules on the same (question, SQL) scenario survive as separate
    pending rows instead of being over-collapsed into one. Blobs whose
    dimension doesn't match the current embed model are skipped (robust to an
    embed_model change)."""
    if qvec is not None:
        rows = con.execute(
            "SELECT id, embedding FROM skills "
            "WHERE verified=0 AND created_by=? AND embedding IS NOT NULL",
            (source,)).fetchall()
        dim = qvec.shape[0]
        floor = get_settings().skill_dedup_threshold
        best_id, best_sim = None, floor
        for r in rows:
            vec = _from_blob(r["embedding"])
            if vec.shape[0] != dim:  # stale blob from a prior embed model — skip
                continue
            sim = float(vec @ qvec)
            if sim >= best_sim:
                best_id, best_sim = r["id"], sim
        return best_id
    # Exact-match fallback: embeddings are unavailable system-wide (fastembed
    # not installed), so cosine matching isn't possible at all — fall back to a
    # verbatim (question, SQL) match from the same source.
    row = con.execute(
        "SELECT id FROM skills WHERE verified=0 AND created_by=? "
        "AND question=? AND canonical_sql=?",
        (source, question, canonical_sql)).fetchone()
    return row["id"] if row else None


def _upvote_or_save(question: str, canonical_sql: str, *, headline: str = "",
                    lesson: str, source: str) -> None:
    """Dedup gate shared by every lesson-writing path: bump an existing
    same-source unverified near-duplicate's upvotes (backfilling its headline
    and rule if it had none, so no lesson text is lost), else insert a new
    UNVERIFIED lesson pending admin review (retrieve_skills_block only returns
    verified=1 rows). The embedding used for the dedup lookup is the
    headline+description vector (the RULE, not the question)."""
    embed_source = _embed_source(headline, lesson)
    v = embed(embed_source) if embed_source else None
    con = connect()
    try:
        dup = _find_duplicate(con, v, question, canonical_sql, source)
        if dup is not None:
            if headline or lesson:  # preserve a rule: backfill onto a rule-less match
                con.execute(
                    "UPDATE skills SET headline=?, lesson=? WHERE id=? "
                    "AND (headline IS NULL OR headline='') "
                    "AND (lesson IS NULL OR lesson='')",
                    (headline, lesson, dup))
            con.execute("UPDATE skills SET upvotes=upvotes+1 WHERE id=?", (dup,))
            con.commit()
            return
    finally:
        con.close()
    save_skill(question, canonical_sql, headline=headline, lesson=lesson,
              created_by=source, verified=False)


def record_lesson_from_critic(question: str, canonical_sql: str, headline: str,
                              description: str) -> None:
    """The post-answer critic caught a likely mistake and forced a revision; its
    finding IS the rule that fixes it — a generalized headline + description.
    Capture it as an UNVERIFIED lesson (deduped only against other pending
    critic candidates) pending admin review — this is the real self-learning
    signal, a mistake the model actually made rather than an answer a user
    happened to like. No-op if both headline and description are blank."""
    headline = (headline or "").strip()
    description = (description or "").strip()
    if not headline and not description:
        return
    _upvote_or_save(question, canonical_sql or "", headline=headline,
                    lesson=description, source="critic")


def record_lesson_from_feedback(question_context: str, headline: str,
                                description: str) -> None:
    """The feedback distiller (app.feedback) judged the user's follow-up message
    to carry generalizable corrective feedback about a prior answer; its finding
    IS the rule that fixes it, in the same headline + description shape the
    critic emits. Capture it as an UNVERIFIED lesson (deduped only against other
    pending user-feedback candidates — never a critic or seed row on the same
    scenario) pending admin review. No-op if both headline and description are
    blank. There is no SQL to attach (the user is correcting the ASSISTANT's
    behavior, not one query), so canonical_sql is always empty."""
    headline = (headline or "").strip()
    description = (description or "").strip()
    if not headline and not description:
        return
    _upvote_or_save(question_context, "", headline=headline,
                    lesson=description, source="user-feedback")


# --- Semantic answer cache -----------------------------------------------------

def cache_lookup(question: str, user_id: int) -> dict | None:
    """Return a cached {final_sql, answer_md, figure} for a near-identical question
    THIS USER asked at the current data_version, else None. Gated by skills_enabled
    (like lesson retrieval) so SKILLS_ENABLED=0 gives a clean, self-learning-off
    A/B baseline — otherwise a cache hit would short-circuit the 'off' arm.

    Scoped per user (migration 29). Without the `user_id` predicate, colleague B
    asking within `cache_similarity_threshold` of colleague A's question was
    served A's stored answer PROSE verbatim — an invisible flow of one person's
    question phrasing to another, and the same attributable leak that
    /api/admin/usage deliberately prevents by never returning question text.
    The cost is a lower hit rate: a popular question is now answered once per
    person rather than once per deployment."""
    s = get_settings()
    if not s.skills_enabled:
        return None
    q = embed(question)
    if q is None:
        return None
    con = connect()
    try:
        dv = data_version(con)
        rows = con.execute(
            "SELECT question, final_sql, answer_md, figure, suggestions, results, "
            "results_truncated, embedding "
            "FROM query_cache WHERE data_version=? AND user_id=? "
            "AND embedding IS NOT NULL",
            (dv, user_id)).fetchall()
        if not rows:
            return None
        mat = np.vstack([_from_blob(r["embedding"]) for r in rows])
        sims = _cosine(q, mat)
        i = int(np.argmax(sims))
        if sims[i] >= s.cache_similarity_threshold:
            return {"final_sql": rows[i]["final_sql"],
                    "answer_md": rows[i]["answer_md"],
                    "figure": json.loads(rows[i]["figure"]) if rows[i]["figure"] else None,
                    "suggestions": (json.loads(rows[i]["suggestions"])
                                    if rows[i]["suggestions"] else None),
                    # The rows that BACK this answer (migration 31). Replayed onto
                    # the cached message so a later turn in the conversation can
                    # ground a recited number against them — without this a cache
                    # hit broke the chain for every turn after it.
                    "results": (json.loads(rows[i]["results"])
                                if rows[i]["results"] else None),
                    "results_truncated": bool(rows[i]["results_truncated"]),
                    "matched_question": rows[i]["question"],
                    "similarity": float(sims[i])}
    finally:
        con.close()
    return None


def _prune_cache(con) -> int:
    """Drop cache rows past the retention window and past the row ceiling.

    The cache had NO bound of any kind: the only DELETE anywhere was
    invalidate_cache()'s wholesale wipe on a data import, so a deployment that
    loads its years once and then runs accumulates a row per distinct first-turn
    question forever — while cache_lookup vstacks and matmuls the WHOLE table on
    every first-turn question, synchronously, before the agent starts. Latency
    and memory grew linearly and permanently.

    Mirrors logbuffer._prune, including its OFFSET-based row cap (id arithmetic
    drifts once AUTOINCREMENT leaves gaps) and the incremental-vacuum reclaim.
    A non-positive setting disables that half, matching log_retention_days /
    log_max_rows."""
    s = get_settings()
    deleted = 0
    if s.cache_retention_days > 0:
        cutoff = time.time() - s.cache_retention_days * 86400
        deleted += con.execute(
            "DELETE FROM query_cache WHERE created_at < ?", (cutoff,)).rowcount
    if s.cache_max_rows > 0:
        deleted += con.execute(
            "DELETE FROM query_cache WHERE id < "
            "(SELECT id FROM query_cache ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (s.cache_max_rows - 1,)).rowcount
    return deleted


def cache_store(question: str, final_sql: str, answer_md: str,
                figure: dict | None = None, suggestions: list | None = None,
                results: list | None = None, results_truncated: bool = False,
                *, user_id: int) -> None:
    v = embed(question)
    if v is None:
        return
    con = connect()
    try:
        con.execute(
            "INSERT INTO query_cache(question, embedding, final_sql, answer_md, "
            "figure, suggestions, results, results_truncated, data_version, "
            "created_at, user_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (question, _to_blob(v), final_sql, answer_md,
             json.dumps(figure) if figure else None,
             json.dumps(suggestions) if suggestions else None,
             # Already capped by the caller's _results_for_storage, so this adds
             # no new size risk beyond what messages.results already carries.
             json.dumps(results) if results else None,
             int(bool(results_truncated)),
             data_version(con), time.time(), user_id))
        # Opportunistic, on the write path only — a read must stay cheap.
        if _prune_cache(con) > 0:
            try:
                # .fetchall() is load-bearing: the pragma frees one page per step.
                con.execute("PRAGMA incremental_vacuum").fetchall()
            except sqlite3.Error:
                pass  # reclaiming space must never break answering
        con.commit()
    finally:
        con.close()


def invalidate_cache() -> None:
    """Called after a data import bumps data_version — old cache no longer matches."""
    con = connect()
    try:
        con.execute("DELETE FROM query_cache")
        con.commit()
    finally:
        con.close()


def _applied_seed_keys(con) -> set[str] | None:
    """The seed slugs this database has already been given, or None if it has
    never recorded any (i.e. it predates key tracking and needs the backfill)."""
    raw = get_meta(con, _SEED_APPLIED_KEY)
    if raw is None:
        return None
    try:
        return set(json.loads(raw))
    except (ValueError, TypeError):  # hand-edited/corrupt marker — re-derive it
        log.warning("unreadable %s marker; re-deriving from existing seed rows",
                    _SEED_APPLIED_KEY)
        return None


def _backfill_applied_seed_keys(con) -> set[str]:
    """One-time bridge for a database seeded before keys existed: a seed whose
    headline OR question already appears on a `seed` row is treated as applied.

    Matching on either half is deliberate — an admin may have edited one of them
    (the Skills tab allows it), and the cost of a miss is a duplicate row someone
    deletes, whereas the cost of a false match is a lesson silently never
    shipped. `question` in particular is what makes this independent of
    `upgrade_seed_lessons`: a database still carrying pre-migration-6 rows has
    NULL headlines, but no upgrade path has ever rewritten a question, so those
    rows are still recognized whichever order the two run in."""
    rows = con.execute(
        "SELECT headline, question FROM skills WHERE created_by='seed'").fetchall()
    headlines = {r["headline"] for r in rows if r["headline"]}
    questions = {r["question"] for r in rows if r["question"]}
    return {s.slug for s in SEED_EXAMPLES
            if s.headline in headlines or s.question in questions}


def seed_from_schema_examples() -> int:
    """Ship any seed lesson this database has not been given yet.

    The seed data (key, question, headline, description, commented SQL) lives in
    app.seeds, a dependency-free leaf module shared with db migration 6 so a
    fresh install and an upgraded one carry identical lesson text.

    Per-LESSON, not all-or-nothing. This used to bail whenever the `skills`
    table held any row at all, which meant a seed added in a later release
    reached fresh installs only: every existing deployment had rows (its
    original seeds, plus critic/feedback lessons), so the gate was closed
    forever and new exemplars silently never arrived. Found in the wild on
    0.2.0.

    A seed ships at most ONCE per database. The keys applied so far are
    recorded in `meta`, so an admin who deletes a seed from the Skills tab has
    made a decision the next boot respects — re-deriving "missing" from the
    table contents would resurrect it on every restart. `save_skill` does no
    dedup of its own (that is `_upvote_or_save`, and only for unverified
    same-source rows), so the marker is the only thing standing between an
    upgrade and a pile of duplicate seeds."""
    n = 0
    con = connect()
    try:
        applied = _applied_seed_keys(con)
        if applied is None:
            applied = _backfill_applied_seed_keys(con)
        missing = [s for s in SEED_EXAMPLES if s.slug not in applied]
    finally:
        con.close()

    for s in missing:
        save_skill(s.question, s.commented_sql, headline=s.headline,
                  lesson=s.description, notes="", created_by="seed", verified=True)
        n += 1

    # Written even when nothing was inserted: recording the backfill is what
    # makes the NEXT call cheap and, more importantly, what makes a later
    # deletion stick. Unknown keys in `applied` are preserved so a rollback to
    # an image with fewer seeds doesn't forget what a newer one shipped.
    con = connect()
    try:
        set_meta(con, _SEED_APPLIED_KEY,
                 json.dumps(sorted(applied | {s.slug for s in SEED_EXAMPLES})))
        con.commit()
    finally:
        con.close()
    return n


def upgrade_seed_lessons() -> int:
    """Idempotent startup backfill: upgrade any 'seed' row still bearing a
    frozen v1 description (the text migration 6 rewrote a terse original INTO,
    on a database that predates this PR) to the new generalized headline +
    description + commented SQL. Matches on created_by='seed' AND the exact
    v1 lesson text, so an admin-edited seed row is left untouched — same
    safety convention as migration 6. Returns the number of rows upgraded."""
    n = 0
    con = connect()
    try:
        for v1_description, v2 in SEED_LESSON_UPGRADES:
            cur = con.execute(
                "UPDATE skills SET headline=?, lesson=?, canonical_sql=? "
                "WHERE created_by='seed' AND lesson=?",
                (v2.headline, v2.description, v2.commented_sql, v1_description))
            n += cur.rowcount
        con.commit()
    finally:
        con.close()
    return n


def reembed_skills_if_needed() -> int:
    """Idempotent startup backfill: recompute every skill's embedding from
    _embed_source(headline, lesson-or-notes) if the stored embeddings still
    derive from a stale source convention (tracked in `meta`
    skills_embed_source_version). Gated on fastembed: if embed() is
    unavailable, this is a no-op and the version marker is left UNSET so a
    later startup (once fastembed is available) retries. A row with no rule
    text at all (empty headline+lesson+notes) has nothing to embed against —
    its existing embedding is left UNTOUCHED (not blanked to NULL), so a
    pre-existing rule-less lesson doesn't silently drop out of retrieval.
    Returns the number of rows actually re-embedded."""
    con = connect()
    try:
        current = get_meta(con, "skills_embed_source_version")
        if current == _EMBED_SOURCE_VERSION:
            return 0
        rows = con.execute("SELECT id, headline, lesson, notes FROM skills").fetchall()
        n = 0
        for r in rows:
            description = r["lesson"] or r["notes"] or ""
            source = _embed_source(r["headline"] or "", description)
            if not source:
                continue  # no rule text — leave its existing embedding as-is
            v = embed(source)
            if v is None:
                # embed() is unavailable — bail without advancing the marker so
                # a later startup (once fastembed loads) retries from scratch.
                con.rollback()
                return 0
            con.execute("UPDATE skills SET embedding=? WHERE id=?",
                       (_to_blob(v), r["id"]))
            n += 1
        set_meta(con, "skills_embed_source_version", _EMBED_SOURCE_VERSION)
        con.commit()
        return n
    finally:
        con.close()
