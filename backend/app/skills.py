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

# `meta` key holding the app version whose code produced the CURRENT contents of
# query_cache. See invalidate_cache_if_version_changed.
_CACHE_VERSION_KEY = "cache_app_version"

# `meta` key holding the JSON list of app.lessoncats tokens an admin has muted
# (A2: lesson-rejection memory) — state, not config, so it lives beside
# _SEED_APPLIED_KEY rather than in a table: at most a handful of elements,
# admin-mutable at runtime. Mirrors _applied_seed_keys' fail-open convention on
# a corrupt/unreadable marker (see muted_categories below).
_MUTED_CATEGORIES_KEY = "muted_lesson_categories"


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
               verified: bool = False, tags: str = "",
               category: str | None = None) -> int:
    source = _embed_source(headline, lesson)
    v = embed(source) if source else None
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO skills(question, canonical_sql, notes, lesson, headline, "
            "embedding, tags, verified, created_by, category, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (question, canonical_sql, notes, lesson, headline or None,
             _to_blob(v) if v is not None else None,
             tags, int(verified), created_by, category, time.time()))
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
    #
    # Deliberately still an `else`, unlike _check_tombstone's and
    # _find_suppressor's. Those two key on the LESSON's own identity
    # (headline+description), so an exact match there IS the same lesson and
    # the arm is safe to run whenever the vector arm misses. This one keys on
    # (question, canonical_sql), which is coarser than the thing being deduped
    # — two genuinely distinct rules can share a question and its SQL. Making
    # it additional collapsed them, caught by
    # `a distinct rule on the same (question, SQL) is NOT deduped`.
    #
    # KNOWN, ACCEPTED: a row saved with embedding = NULL (fastembed down at the
    # time) therefore stays invisible to dedup once embeddings recover, so a
    # repeat of it inserts a second pending row instead of upvoting the first.
    # Mild — an admin sees two similar rows in the review queue — where the
    # same shape in _check_tombstone meant a REJECTED lesson was re-proposed
    # forever. Fixing it needs a second arm keyed on headline+description, not
    # a reshuffle of this one.
    row = con.execute(
        "SELECT id FROM skills WHERE verified=0 AND created_by=? "
        "AND question=? AND canonical_sql=?",
        (source, question, canonical_sql)).fetchone()
    return row["id"] if row else None


def _find_suppressor(con, qvec: np.ndarray | None, headline: str,
                     description: str, source: str, *,
                     include_pending_other_source: bool) -> int | None:
    """Id of a lesson a new candidate is near-identical to, from OUTSIDE
    `_find_duplicate`'s (verified=0, same source) upvote scope — a match here
    means "suppress, insert nothing, upvote nothing", never an upvote, since
    writing to a curated/verified row or a different source's pending row
    from an unreviewed candidate would corrupt the admin's ranking signal.

    Two arms, asymmetric ON PURPOSE — `include_pending_other_source` picks
    which:

    - VERIFIED, for every caller/source (always searched). An approved lesson
      is already live guidance in the prompt; a fresh candidate restating it
      — from ANY source — adds nothing to suppress.
    - unverified + a DIFFERENT source, only when `include_pending_other_source`
      is True (today: the critic path only). A user's corrective feedback and
      the model's own self-critique are DIFFERENT EVIDENCE about the same
      scenario, and the review queue should be able to show both rather than
      let one silently swallow the other — pinned by
      test_feedback_lesson_not_collapsed_into_a_critic_row_same_scenario.
      record_lesson_from_feedback therefore passes False here: its dedup
      reach for a PENDING row stays exactly _find_duplicate's own-source-only
      scope, unchanged from before this widening existed.

    `IFNULL(created_by,'') != ?` is required on the different-source arm, not
    `created_by != ?`: `created_by` is nullable, and SQL's
    `NULL != 'critic'` evaluates to NULL (not true), which a WHERE clause
    reads as false — silently excluding every NULL-source row from
    suppression. Same dimension-mismatch skip guard as `_find_duplicate`, so
    a row embedded under a stale embed_model can't crash the dot product."""
    if include_pending_other_source:
        predicate = "(verified=1 OR IFNULL(created_by,'') != ?)"
        params: tuple = (source,)
    else:
        predicate = "verified=1"
        params = ()
    if qvec is not None:
        rows = con.execute(
            f"SELECT id, embedding FROM skills WHERE embedding IS NOT NULL AND {predicate}",
            params).fetchall()
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
        if best_id is not None:
            return best_id
    # Exact-match arm — ADDITIONAL, not an `else`, mirroring _find_duplicate
    # and _check_tombstone. A VERIFIED lesson stored with embedding = NULL
    # (fastembed down at the time) would otherwise stop suppressing restatements
    # of itself once embeddings recovered, even though it is already live
    # guidance in the prompt.
    row = con.execute(
        f"SELECT id FROM skills WHERE headline=? AND lesson=? AND {predicate}",
        (headline, description, *params)).fetchone()
    return row["id"] if row else None


def _check_tombstone(con, qvec: np.ndarray | None, headline: str,
                     description: str) -> bool:
    """True if a near-identical lesson was already rejected (a row in
    lesson_rejections), bumping its `hits` counter. Unrestricted by source or
    verified status — an admin's rejection is a judgment about the IDEA, not
    about who is proposing it this time, so it must suppress a repeat from ANY
    source. Same dimension-mismatch skip guard as _find_duplicate/_find_suppressor,
    and the same exact-text fallback for when embeddings are unavailable
    system-wide."""
    best_id = None
    if qvec is not None:
        rows = con.execute(
            "SELECT id, embedding FROM lesson_rejections "
            "WHERE embedding IS NOT NULL").fetchall()
        dim = qvec.shape[0]
        best_sim = get_settings().skill_dedup_threshold
        for r in rows:
            vec = _from_blob(r["embedding"])
            if vec.shape[0] != dim:
                continue
            sim = float(vec @ qvec)
            if sim >= best_sim:
                best_id, best_sim = r["id"], sim
    # The exact-text match is an ADDITIONAL arm, not an `else`, and that is the
    # whole point. A tombstone written while embeddings were unavailable has
    # embedding = NULL -- delete_skill reuses the skill's stored vector and
    # re-embedding returns None for the same reason it was NULL, since
    # _embedder swallows everything. The vector arm filters those rows out, so
    # the moment fastembed recovers that rejection becomes PERMANENTLY
    # invisible and the identical lesson re-queues forever: exactly the failure
    # this table exists to end, silent in both directions (a suppression leaves
    # no row, a missed suppression leaves no trace).
    if best_id is None:
        row = con.execute(
            "SELECT id FROM lesson_rejections WHERE headline=? AND description=?",
            (headline, description)).fetchone()
        best_id = row["id"] if row else None
    if best_id is None:
        return False
    con.execute("UPDATE lesson_rejections SET hits=hits+1 WHERE id=?", (best_id,))
    return True


def muted_categories(con) -> set[str]:
    """The set of app.lessoncats tokens an admin has muted. Fails OPEN (empty
    set, logged) on a corrupt/unreadable marker — mirrors
    `_applied_seed_keys`'s convention, and the direction matters: reading a
    corrupt marker as "everything muted" would silently keep suppressing an
    admin-visible control that's supposed to be reversible, while reading it
    as "nothing muted" just re-queues candidates for review, the safe
    direction for a corrupt marker to fail in."""
    raw = get_meta(con, _MUTED_CATEGORIES_KEY)
    if raw is None:
        return set()
    try:
        return set(json.loads(raw))
    except (ValueError, TypeError):
        log.warning("unreadable %s marker; treating as no categories muted",
                    _MUTED_CATEGORIES_KEY)
        return set()


def _set_category_muted_on(con, category: str, muted: bool) -> None:
    """Read-modify-write the muted-categories marker on an EXISTING
    connection, inside the CALLER's transaction (no commit here) — lets
    admin.delete_skill fold a mute into the same atomic tombstone+delete
    request. Preserves any token this build doesn't recognize, so a rollback
    to an image with fewer categories doesn't forget a newer mute."""
    current = muted_categories(con)
    if muted:
        current.add(category)
    else:
        current.discard(category)
    set_meta(con, _MUTED_CATEGORIES_KEY, json.dumps(sorted(current)))


def set_category_muted(category: str, muted: bool) -> None:
    con = connect()
    try:
        _set_category_muted_on(con, category, muted)
        con.commit()
    finally:
        con.close()


def _upvote_or_save(question: str, canonical_sql: str, *, headline: str = "",
                    lesson: str, source: str, category: str | None = None) -> None:
    """Dedup gate shared by every lesson-writing path, in a FIXED order (each
    step's position is deliberate — see the inline notes):

      1. (muted-category gate lives in the CALLER, record_lesson_from_critic,
         before this is even reached — see there)
      2. embed the candidate's headline+description
      3. tombstone check (_check_tombstone) — a rejected idea must not recur
      4. _find_duplicate's existing same-source unverified upvote check
      5. _find_suppressor's widened (verified OR different-source) check
      6. insert a new UNVERIFIED lesson

    Step 3 runs BEFORE step 4: a rejected idea must not inflate an existing
    pending row's upvotes either — checking the upvote target first would let
    a tombstoned candidate "vote" for a row an admin never actually approved.

    Step 5 runs AFTER step 4, not before: _find_suppressor's predicate is the
    COMPLEMENT of _find_duplicate's (verified=1 OR a DIFFERENT source), so the
    two searches never both match the same row — but the ORDER still matters
    because they can each independently find a DIFFERENT row for the same
    candidate (e.g. an old same-source pending duplicate AND a newer verified
    near-identical lesson from someone else). Checking the widened net first
    would suppress on the verified match and never reach the same-source
    upvote — silently dropping the everyday "this exact rule came up again"
    case instead of upvoting it, destroying the recurrence signal the review
    queue depends on. Upvoting the ordinary repeat first is the more useful
    outcome whenever both exist."""
    embed_source = _embed_source(headline, lesson)
    v = embed(embed_source) if embed_source else None
    con = connect()
    try:
        if _check_tombstone(con, v, headline, lesson):
            con.commit()
            log.info("lesson suppressed (tombstoned): %s",
                    headline or (lesson[:80] if lesson else "(untitled)"))
            return
        dup = _find_duplicate(con, v, question, canonical_sql, source)
        if dup is not None:
            if headline or lesson:  # preserve a rule: backfill onto a rule-less match
                con.execute(
                    "UPDATE skills SET headline=?, lesson=? WHERE id=? "
                    "AND (headline IS NULL OR headline='') "
                    "AND (lesson IS NULL OR lesson='')",
                    (headline, lesson, dup))
            if category:  # backfill a NULL category, never overwrite a set one
                con.execute(
                    "UPDATE skills SET category=? WHERE id=? AND category IS NULL",
                    (category, dup))
            con.execute("UPDATE skills SET upvotes=upvotes+1 WHERE id=?", (dup,))
            con.commit()
            return
        # _find_suppressor runs for EVERY source, but its two arms are
        # asymmetric (see that function's docstring for the full reasoning):
        #
        # - VERIFIED lessons are searched regardless of source. An approved
        #   rule is already live guidance; a feedback candidate that's
        #   genuinely near-identical to one is redundant by definition (Todd:
        #   "before a new skill is suggested, it should check to see if
        #   something similar already exists or is already queued") — that's
        #   exactly the review-queue noise this PR exists to cut.
        # - the unverified + DIFFERENT-source arm is CRITIC-ONLY.
        #   record_lesson_from_feedback's dedup scope for a PENDING row stays
        #   exactly _find_duplicate's own-source-only reach, unchanged from
        #   before this widening existed — pinned by
        #   test_feedback_lesson_not_collapsed_into_a_critic_row_same_scenario,
        #   which needs an IDENTICAL feedback+critic pair on the same scenario
        #   to survive as two separate rows: a user's correction and the
        #   model's own self-critique are different EVIDENCE about the same
        #   scenario, and the review queue should show both.
        sup = _find_suppressor(con, v, headline, lesson, source,
                              include_pending_other_source=(source == "critic"))
        if sup is not None:
            con.commit()
            log.info("lesson suppressed (duplicate-of-%s): %s", sup,
                    headline or (lesson[:80] if lesson else "(untitled)"))
            return
    finally:
        con.close()
    save_skill(question, canonical_sql, headline=headline, lesson=lesson,
              created_by=source, verified=False, category=category)


def record_lesson_from_critic(question: str, canonical_sql: str, headline: str,
                              description: str, category: str | None = None) -> None:
    """The post-answer critic caught a likely mistake and forced a revision; its
    finding IS the rule that fixes it — a generalized headline + description.
    Capture it as an UNVERIFIED lesson (deduped only against other pending
    critic candidates) pending admin review — this is the real self-learning
    signal, a mistake the model actually made rather than an answer a user
    happened to like. No-op if both headline and description are blank.

    The muted-category gate (A2) runs HERE, first, before any embed call — the
    only step that still works when embeddings are unavailable, and cheap
    enough to check unconditionally."""
    headline = (headline or "").strip()
    description = (description or "").strip()
    if not headline and not description:
        return
    if category:
        con = connect()
        try:
            muted = category in muted_categories(con)
        finally:
            con.close()
        if muted:
            log.info("lesson suppressed (muted-category): %s [%s]",
                    headline or description[:80], category)
            return
    _upvote_or_save(question, canonical_sql or "", headline=headline,
                    lesson=description, source="critic", category=category)


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
             # Capped by the caller's _results_for_storage, so this adds no size
             # risk beyond what messages.results already carries. That was once
             # only HALF true and this comment was the reason nobody looked: the
             # caller's drop-largest loop was guarded by `len(blobs) > 1` and so
             # never measured a LONE result, which reached here uncapped. It now
             # shrinks the survivor to fit, so the claim finally holds.
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


def invalidate_cache_if_version_changed() -> int:
    """Wipe the answer cache when the APP has changed since it was written.
    Returns how many rows were dropped (0 = same version, nothing to do).

    A cached answer is a verbatim replay of prose produced by a particular
    build, under a particular system prompt and a particular SCHEMA.md. When
    those change the stored answer can become simply WRONG, and until now
    nothing noticed: `invalidate_cache` had exactly one caller — importer.py,
    after a data import — so an app upgrade left every row in place.

    Found while verifying #326, which is the part worth remembering. That PR
    corrected a false award-level rule in SCHEMA.md that had made the agent
    double-count short certificates. Re-asking the question on the fixed build
    replayed the OLD wrong total from cache (`model_used='cache'`, no SQL
    events) and the fix reached nobody who had already asked. With
    `cache_retention_days` at 30 and a 0.93 similarity threshold, that is a
    wide door.

    **A MISSING marker counts as changed, and that is the load-bearing case.**
    Every deployment upgrading INTO the release that adds this arrives with no
    marker and a populated cache — exactly the situation this exists for.
    Reading "no marker" as "already current" would make the feature miss its own
    first release, which is the bug `seed_from_schema_examples` shipped (it
    bailed whenever the table held any row, so lessons added in a later release
    reached fresh installs only). A fresh database simply has nothing to delete
    and records the marker on the way past.

    Version-keyed rather than content-keyed on purpose: this wipes once per
    upgrade even when nothing relevant moved, costing one cache miss per
    question. Keying on a fingerprint of SCHEMA.md + the prompt would invalidate
    precisely when the thing that determines the answer changes, which is the
    better design and a bigger one (it needs the hash stored per row). The
    conservative direction is the safe one here: a needless miss costs a query,
    a stale hit ships a wrong answer.

    `app_version` is "dev" outside Docker, so local development wipes at most
    once and then never again."""
    version = get_settings().app_version
    con = connect()
    try:
        if get_meta(con, _CACHE_VERSION_KEY) == version:
            return 0
        n = con.execute("DELETE FROM query_cache").rowcount or 0
        set_meta(con, _CACHE_VERSION_KEY, version)
        con.commit()
        return n
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
