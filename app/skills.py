"""Self-learning: a library of LESSONS retrieved as guidance, plus a semantic
answer cache.

A "lesson" is a short human-readable RULE ("for a national degree total filter
cipcode='99'; never sum across CIP codes — ~4x overcount") — the transferable
knowledge — with the original question as the retrieval key and an OPTIONAL SQL
worked example. Lessons come from three sources: `seed` (SCHEMA.md examples),
`feedback` (a user 👍), and `critic` (the post-answer critic caught a real
mistake and phrased the fix as a rule). Feedback/critic lessons start UNVERIFIED
and must be approved in the admin UI before they are ever retrieved.

Retrieval embeds the incoming question and returns the rules attached to the
most similar past scenarios, deduped so near-identical lessons don't crowd the
few-shot slots. Embeddings run locally via fastembed (CPU, no per-call cost); if
fastembed isn't installed, retrieval/dedup degrade gracefully (no-op retrieval,
exact-match dedup) so the app still runs.
"""
from __future__ import annotations

import logging
import time

import numpy as np

from app.config import get_settings
from app.db import connect, data_version

log = logging.getLogger("ipeds.skills")
_model = None
_embed_ok = True


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


# --- Skill retrieval (few-shot) ------------------------------------------------

def _lesson_text(row) -> str:
    """One retrieved lesson: lead with the RULE, then an optional worked example."""
    lesson = (row["lesson"] or row["notes"] or "").strip()
    sql = (row["canonical_sql"] or "").strip()
    parts = []
    if lesson:
        parts.append(f"LESSON: {lesson}")
    if row["question"] and sql:
        prefix = "e.g. " if lesson else ""
        parts.append(f"{prefix}Q: {row['question']}\nSQL:\n{sql}")
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
            "SELECT id, question, canonical_sql, notes, lesson, embedding FROM skills "
            "WHERE verified=1 AND embedding IS NOT NULL").fetchall()
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

def save_skill(question: str, canonical_sql: str, *, notes: str = "",
               lesson: str = "", created_by: str = "system", verified: bool = False,
               tags: str = "") -> int:
    v = embed(question)
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO skills(question, canonical_sql, notes, lesson, embedding, "
            "tags, verified, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (question, canonical_sql, notes, lesson,
             _to_blob(v) if v is not None else None,
             tags, int(verified), created_by, time.time()))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _find_duplicate(con, qvec: np.ndarray | None, question: str,
                    canonical_sql: str) -> int | None:
    """Id of a near-duplicate lesson to upvote instead of inserting, else None.
    Prefers embedding similarity (same scenario, even if reworded); falls back to
    an exact (question, SQL) match when embeddings are unavailable."""
    if qvec is not None:
        rows = con.execute(
            "SELECT id, embedding FROM skills WHERE embedding IS NOT NULL").fetchall()
        floor = get_settings().skill_dedup_threshold
        best_id, best_sim = None, floor
        for r in rows:
            sim = float(_from_blob(r["embedding"]) @ qvec)
            if sim >= best_sim:
                best_id, best_sim = r["id"], sim
        return best_id
    row = con.execute("SELECT id FROM skills WHERE question=? AND canonical_sql=?",
                      (question, canonical_sql)).fetchone()
    return row["id"] if row else None


def _upvote_or_save(question: str, canonical_sql: str, *, lesson: str,
                    created_by: str) -> None:
    """Dedup gate shared by feedback + critic promotion: bump an existing
    near-duplicate's upvotes, else insert a new UNVERIFIED lesson pending admin
    review (retrieve_skills_block only returns verified=1 rows)."""
    v = embed(question)
    con = connect()
    try:
        dup = _find_duplicate(con, v, question, canonical_sql)
        if dup is not None:
            con.execute("UPDATE skills SET upvotes=upvotes+1 WHERE id=?", (dup,))
            con.commit()
            return
    finally:
        con.close()
    save_skill(question, canonical_sql, lesson=lesson,
               created_by=created_by, verified=False)


def promote_from_message(question: str, sql: str) -> None:
    """A 👍 on an answer promotes its (question, last SQL) into an unverified
    lesson, or upvotes a near-duplicate."""
    if not sql:
        return
    _upvote_or_save(question, sql, lesson="", created_by="feedback")


def record_lesson_from_critic(question: str, canonical_sql: str, issue: str) -> None:
    """The post-answer critic caught a likely mistake and forced a revision; its
    finding IS the rule that fixes it. Capture it as an UNVERIFIED lesson (deduped)
    pending admin review — this is the real self-learning signal, a mistake the
    model actually made rather than an answer a user happened to like."""
    issue = (issue or "").strip()
    if not issue:
        return
    _upvote_or_save(question, canonical_sql or "", lesson=issue, created_by="critic")


# --- Semantic answer cache -----------------------------------------------------

def cache_lookup(question: str) -> dict | None:
    """Return a cached {final_sql, answer_md} for a near-identical question at the
    current data_version, else None."""
    q = embed(question)
    if q is None:
        return None
    s = get_settings()
    con = connect()
    try:
        dv = data_version(con)
        rows = con.execute(
            "SELECT question, final_sql, answer_md, embedding FROM query_cache "
            "WHERE data_version=? AND embedding IS NOT NULL", (dv,)).fetchall()
        if not rows:
            return None
        mat = np.vstack([_from_blob(r["embedding"]) for r in rows])
        sims = _cosine(q, mat)
        i = int(np.argmax(sims))
        if sims[i] >= s.cache_similarity_threshold:
            return {"final_sql": rows[i]["final_sql"],
                    "answer_md": rows[i]["answer_md"],
                    "matched_question": rows[i]["question"],
                    "similarity": float(sims[i])}
    finally:
        con.close()
    return None


def cache_store(question: str, final_sql: str, answer_md: str) -> None:
    v = embed(question)
    if v is None:
        return
    con = connect()
    try:
        con.execute(
            "INSERT INTO query_cache(question, embedding, final_sql, answer_md, "
            "data_version, created_at) VALUES (?,?,?,?,?,?)",
            (question, _to_blob(v), final_sql, answer_md, data_version(con), time.time()))
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


def seed_from_schema_examples() -> int:
    """Seed the skill library with the SCHEMA.md §8 / README worked examples."""
    seeds = [
        ("Top 20 institutions granting Associate's degrees in Registered Nursing "
         "(CIP 51.3801) per year over the last 3 years",
         "WITH grads AS (\n"
         "  SELECT year, unitid, SUM(ctotalt) AS awards FROM c_a\n"
         "  WHERE cipcode='51.3801' AND awlevel=3 AND majornum=1\n"
         "    AND year > (SELECT MAX(year)-3 FROM _years)\n"
         "  GROUP BY year, unitid)\n"
         ",ranked AS (SELECT *, RANK() OVER (PARTITION BY year ORDER BY awards DESC)"
         " rk FROM grads)\n"
         "SELECT r.year, r.rk, ic.instnm, ic.stabbr, r.awards\n"
         "FROM ranked r JOIN institutions_current ic USING (unitid)\n"
         "WHERE r.rk<=20 ORDER BY r.year DESC, r.rk;",
         "Exact 6-digit CIP; constant year bound; RANK per year."),
        ("How many bachelor's degrees in Computer Science (11.0701) did California "
         "public universities award in the most recent year?",
         "SELECT SUM(c.ctotalt) AS cs_bachelors FROM c_a c\n"
         "JOIN hd h ON h.unitid=c.unitid AND h.year=c.year\n"
         "WHERE c.cipcode='11.0701' AND c.awlevel=5 AND c.majornum=1\n"
         "  AND c.year=(SELECT MAX(year) FROM _years) AND h.stabbr='CA' AND h.control=1;",
         "Year-matched hd join; control=1 public; awlevel=5 bachelor's."),
        ("National total of associate's degrees per year, all programs",
         "SELECT year, SUM(ctotalt) AS associates FROM c_a\n"
         "WHERE awlevel=3 AND majornum=1 AND cipcode='99'\n"
         "GROUP BY year ORDER BY year;",
         "Use grand-total CIP '99' — never sum all cipcodes (overcounts ~4x)."),
    ]
    n = 0
    con = connect()
    try:
        have = con.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    finally:
        con.close()
    if have:
        return 0
    for q, sql, note in seeds:
        # `note` is the rule → it's the lesson; the SQL is the worked example.
        save_skill(q, sql, notes=note, lesson=note, created_by="seed", verified=True)
        n += 1
    return n
