// Admin → Skills: the learned-lesson pool and its verify/delete actions.
// Split out of Admin.jsx unchanged.
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { loadErrorMessage } from "../authcopy.js";
import SqlBlock from "../SqlBlock.jsx";
import { useToast } from "../Toast.jsx";
import { useConfirm } from "../ConfirmModal.jsx";
import { ruleName } from "./format.js";
import { canMuteCategory, categoryLabel, rejectionCountLabel } from "./lessoncats.js";

export default function Skills({ onAttentionChanged }) {
  const toast = useToast();
  const confirm = useConfirm();
  const refreshAttention = onAttentionChanged || (() => {});
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState("");
  // A2 (lesson-rejection memory): the closed category set (with live
  // muted/pending state) and the rejection-tombstone list, each loaded
  // independently of the lesson list above.
  const [categories, setCategories] = useState([]);
  const [rejections, setRejections] = useState([]);
  const [rejectionsErr, setRejectionsErr] = useState("");
  const rejectedSummaryRef = useRef(null);   // focus target after an Undo
  const [editingId, setEditingId] = useState(null);   // at most one card at a time
  const [draft, setDraft] = useState({ headline: "", lesson: "", canonical_sql: "" });
  // Focus returns to the "edit" button when the editor closes (a11y). We can't
  // hold the clicked node like Chat.jsx does: opening the editor unmounts that
  // button, so the captured node is detached and focusing it silently no-ops.
  // Keep a per-lesson ref map instead and re-find the freshly mounted button.
  const editBtnRefs = useRef({});   // skill id -> its "edit" button node
  const headlineRef = useRef(null);
  const headingRef = useRef(null);  // focus target after a card is deleted
  // No .catch at all before this: a failed load was an unhandled rejection and
  // rendered the "No lessons yet" empty state, which is byte-identical to a
  // healthy-but-empty library.
  const load = () => api.skills()
    .then((d) => { setRows(d); setErr(""); })
    .catch((e) => setErr(loadErrorMessage("the lessons", e?.detail)));
  // Categories: no visible error state on a load failure — canMuteCategory
  // and categoryLabel both fail CLOSED on an empty/missing list (no pill, no
  // "Reject & mute" action offered), which is the safe direction for this
  // one, unlike the rejections list below. `Array.isArray` guards against a
  // non-array response (e.g. a broad `**/skills/*` test fixture, or any other
  // unexpected shape) crashing the whole panel on `.filter`/`.find`.
  const loadCategories = () => api.skillCategories()
    .then((d) => setCategories(Array.isArray(d) ? d : []))
    .catch(() => setCategories([]));
  // Rejections: a load failure must render a VISIBLE error, never "Rejected
  // (0)" — the deniedError precedent (Allowlist.jsx): a failed load must
  // never be indistinguishable from "confirmed nothing rejected".
  const loadRejections = () => api.skillRejections()
    .then((d) => { setRejections(Array.isArray(d) ? d : []); setRejectionsErr(""); })
    .catch((e) => setRejectionsErr(loadErrorMessage("rejected lessons", e?.detail)));
  useEffect(() => { load(); loadCategories(); loadRejections(); }, []);

  // Which "edit" button to focus once a save's reload has COMMITTED, as a fresh
  // `{ id }` object each time so the layout effect re-fires per save (even re-saving
  // the same id) without a set-state-in-effect (an error under this repo's lint).
  // The effect runs after the DOM commit, so the freshly-mounted button is focused
  // deterministically — never a bare rAF racing load()'s setRows (which remounts the
  // button under the just-focused node and drops focus to <body>).
  const [pendingEditFocus, setPendingEditFocus] = useState(null);
  useLayoutEffect(() => {
    if (!pendingEditFocus) return;
    editBtnRefs.current[pendingEditFocus.id]?.focus?.();
  }, [pendingEditFocus]);
  const pending = rows.filter((r) => !r.verified).length;

  // Action outcomes go to the app-wide toast (visible + announced once) — the
  // Skills tab previously had only an sr-only status region, so sighted admins
  // got no confirmation at all. Focus management (editBtnRefs) is independent
  // and unchanged below.
  const announce = (text, kind = "") => toast(text, kind);

  const setVerified = (s, verified) =>
    api.patchSkill(s.id, { verified }).then(() => {
      announce(verified ? "Lesson verified." : "Lesson moved back to unverified.", "ok");
      load();
      refreshAttention();  // verified count changed → update the Skills badge now
    // Without this a 4xx/5xx or a dropped connection produced an unhandled
    // rejection and NOTHING else: no toast, no message, no state change, so the
    // admin pressed Verify and the button simply did nothing. Every sibling
    // mutation in this file already toasts on failure; this is the most-used
    // action in it.
    }).catch(() => announce("Couldn't change that lesson's status.", "error"));

  function startEdit(s) {
    setEditingId(s.id);
    setDraft({
      headline: s.headline || "",
      lesson: s.lesson || s.notes || "",
      canonical_sql: s.canonical_sql || "",
    });
    requestAnimationFrame(() => headlineRef.current?.focus?.());
  }
  function closeEdit(id) {
    setEditingId(null);
    // rAF runs after React has committed the re-render, so the ref map now
    // holds the newly mounted button rather than the one we just tore down.
    requestAnimationFrame(() => editBtnRefs.current[id]?.focus?.());
  }
  // A lesson with neither headline nor description has nothing to embed against,
  // so retrieval could never surface it — block the save rather than store a
  // rule that's dead on arrival.
  const draftIsEmpty = !draft.headline.trim() && !draft.lesson.trim();
  function saveEdit(s) {
    // Reachable now that Save is aria-disabled rather than disabled: land the
    // user on the field that unblocks them instead of doing nothing.
    if (draftIsEmpty) { headlineRef.current?.focus?.(); return; }
    const description = draft.lesson.trim();
    api.patchSkill(s.id, {
      headline: draft.headline.trim(),
      // lesson and notes are written together, the way migration 6 does it
      // (app/db.py). Every reader resolves the description as
      // `lesson or notes`, so writing lesson alone would let a stale notes
      // resurrect text the admin just deleted — back into the card AND into
      // the model's prompt, while the embedding no longer matches it.
      lesson: description,
      notes: description,
      canonical_sql: draft.canonical_sql.trim(),
    }).then(async () => {
      announce("Lesson updated.");
      setEditingId(null);
      // Restore focus only AFTER the list reload has committed. Requesting it via
      // pendingEditFocus (consumed by the layout effect post-commit) is
      // deterministic — a bare rAF here races the reload's setRows, which remounts
      // the edit button under the just-focused node and drops focus to <body>
      // (the focus-restore-vs-reload race; a single rAF isn't enough under load).
      await load();
      setPendingEditFocus({ id: s.id });
    }).catch(() => announce("Couldn't save that lesson — nothing was changed.", "error"));
  }
  const focusHeading = () => requestAnimationFrame(() => headingRef.current?.focus?.());
  const reject = (s) => {
    // Confirm only when there's curated/used data to lose — a fresh unreviewed
    // proposal (verified=false, no votes/hits) can be dismissed without nagging.
    const risky = s.verified || s.upvotes > 0 || s.hits > 0;
    if (!risky) {
      // No modal, but still route the outcome through a toast + move focus off
      // the card that's about to unmount (it previously dropped to <body>).
      api.deleteSkill(s.id)
        .then(() => { announce("Lesson rejected.", "ok"); return load(); })
        .then(() => { focusHeading(); refreshAttention(); })
        .catch(() => announce("Couldn't delete that lesson.", "error"));
      return;
    }
    confirm({
      variant: "danger",
      title: `Delete this ${s.verified ? "verified " : ""}lesson?`,
      body: "This permanently deletes the lesson. This action cannot be undone.",
      details: ruleName(s),
      confirmLabel: "Delete lesson",
      onConfirm: () => api.deleteSkill(s.id),
      successToast: "Lesson rejected.",
      errorToast: "Couldn't delete that lesson.",
      onSuccess: async () => { await load(); focusHeading(); refreshAttention(); },
    });
  };

  // "Reject & mute <category>" — a change to FUTURE behaviour (every future
  // proposal in that category is suppressed until unmuted), never the
  // disposable-single-proposal shortcut reject() takes above: this ALWAYS
  // confirms, whatever the lesson's own verified/votes/hits state.
  const rejectAndMute = (s) => {
    const label = categoryLabel(s.category, categories);
    confirm({
      variant: "danger",
      title: `Reject this lesson and mute "${label}"?`,
      body: `This deletes the lesson AND stops the assistant proposing any `
          + `future "${label}" lesson until you unmute it.`,
      details: ruleName(s),
      confirmLabel: "Mute this category",
      onConfirm: () => api.deleteSkill(s.id, { muteCategory: true }),
      successToast: `Lesson rejected. "${label}" lessons are now muted.`,
      errorToast: "Couldn't reject and mute that category.",
      onSuccess: async () => {
        await Promise.all([load(), loadCategories(), loadRejections()]);
        focusHeading();
        refreshAttention();
      },
    });
  };

  const unmuteCategory = (cat) =>
    api.muteSkillCategory(cat.token, false)
      .then(() => { announce(`"${cat.label}" lessons are unmuted.`, "ok"); return loadCategories(); })
      .catch(() => announce("Couldn't unmute that category.", "error"));

  // Removed OPTIMISTICALLY from local state rather than re-fetched: the server
  // already confirmed the delete, and re-fetching adds nothing but latency
  // (and a second request that could itself fail after the first succeeded).
  const undoRejection = (r) =>
    api.deleteSkillRejection(r.id)
      .then(() => {
        announce("Rejection cleared.", "ok");
        setRejections((rs) => rs.filter((x) => x.id !== r.id));
        requestAnimationFrame(() => rejectedSummaryRef.current?.focus?.());
      })
      .catch(() => announce("Couldn't clear that rejection.", "error"));

  const clearRejections = () =>
    api.clearSkillRejections()
      .then(() => {
        announce("Cleared every rejected-lesson record.", "ok");
        setRejections([]);
        requestAnimationFrame(() => rejectedSummaryRef.current?.focus?.());
      })
      .catch(() => announce("Couldn't clear the rejected-lesson list.", "error"));

  const mutedCategories = categories.filter((c) => c.muted);

  return (
    <div className="panel">
      <h2 ref={headingRef} tabIndex={-1}>Learned lessons ({rows.length})</h2>
      <p className="muted small">
        Rules the assistant applies as guidance. The post-answer critic proposes a
        lesson when it catches a mistake, and a user’s corrective feedback on a
        follow-up turn proposes one too — each a short headline plus a longer
        description — and it starts <strong>unverified</strong> until you approve
        it here.
        {pending > 0 && ` ${pending} awaiting review.`}
      </p>
      {err && <p className="denied-error" role="alert">{err}</p>}
      {!err && rows.length === 0 && (
        <p className="muted small">
          No lessons yet — they’ll appear here as the critic or a user’s
          corrective feedback proposes them.
        </p>
      )}
      {rows.map((s) => {
        const description = s.lesson || s.notes || "";
        const headline = s.headline || description;
        const showDescription = description && description !== headline;
        return (
        <div key={s.id} className="skill">
          <div className="skill-head">
            <span className="lesson-rule">
              {headline || <em className="muted">(no rule text)</em>}
            </span>
            <span className="tags">
              {s.verified
                ? <span className="tag ok">verified</span>
                : <span className="tag warn">unverified</span>}
              <span className="tag">from {s.created_by || "?"}</span>
              {/* sr-only text rather than aria-label: an aria-label on a
                  roleless <span> is PROHIBITED and simply ignored, so the counts
                  reached a screen reader as bare ▲/▼ glyphs or not at all (axe
                  aria-prohibited-attr, serious — found the moment the scans
                  started covering admin pages). role="img" would also be valid
                  but prunes descendants, and this repo has been bitten by that
                  once already on the chart figure. */}
              <span className="tag">
                <span aria-hidden="true">▲{s.upvotes} ▼{s.downvotes}</span>
                <span className="sr-only">
                  {s.upvotes} upvotes, {s.downvotes} downvotes
                </span>
              </span>
              <span className="tag">hits {s.hits}</span>
              {/* A2: category pill. categoryLabel fails closed (returns "")
                  for a NULL/unrecognized category or a not-yet-loaded
                  category list, so a pre-existing/seed/feedback row (whose
                  category is always NULL) renders no pill at all. */}
              {categoryLabel(s.category, categories) && (
                <span className="tag">{categoryLabel(s.category, categories)}</span>
              )}
            </span>
          </div>
          {editingId === s.id ? (
            <div className="lesson-edit" role="group"
                 aria-label={`Edit lesson: ${ruleName(s)}`}
                 onKeyDown={(e) => { if (e.key === "Escape") closeEdit(s.id); }}>
              <label className="lesson-field">
                <span className="muted small">Headline</span>
                <input ref={headlineRef} type="text" maxLength={300} value={draft.headline}
                       onChange={(e) => { const v = e.target.value; setDraft((d) => ({ ...d, headline: v })); }} />
              </label>
              <label className="lesson-field">
                <span className="muted small">Description</span>
                <textarea rows={4} maxLength={4000} value={draft.lesson}
                          onChange={(e) => { const v = e.target.value; setDraft((d) => ({ ...d, lesson: v })); }} />
              </label>
              <label className="lesson-field">
                <span className="muted small">Example query</span>
                <textarea rows={6} maxLength={8000} className="mono" value={draft.canonical_sql}
                          onChange={(e) => { const v = e.target.value; setDraft((d) => ({ ...d, canonical_sql: v })); }} />
              </label>
              <div className="msg-actions">
                {/* aria-disabled rather than disabled: a disabled button is
                    unfocusable, so a screen-reader user who empties both
                    fields just finds Save gone with no explanation, and any
                    aria-describedby on it would never be read. saveEdit
                    early-returns, so the click is a safe no-op. */}
                <button className="btn-verify" aria-disabled={draftIsEmpty}
                        aria-describedby={draftIsEmpty ? `save-hint-${s.id}` : undefined}
                        onClick={() => saveEdit(s)}>Save</button>
                <button className="link" onClick={() => closeEdit(s.id)}>Cancel</button>
                {draftIsEmpty && (
                  <span id={`save-hint-${s.id}`} className="muted small">
                    Give it a headline or description to save.
                  </span>
                )}
              </div>
            </div>
          ) : (
          <>
          {showDescription && (
            <details className="lesson-desc">
              <summary className="muted small">Details</summary>
              <p>{description}</p>
            </details>
          )}
          {s.canonical_sql && (
            <details className="lesson-example">
              <summary className="muted small">Example query</summary>
              {s.question && <div className="muted small qtext">{s.question}</div>}
              <SqlBlock code={s.canonical_sql} />
            </details>
          )}
          <div className="msg-actions">
            {s.verified ? (
              <button className="link" aria-label={`Unverify lesson: ${ruleName(s)}`}
                      onClick={() => setVerified(s, false)}>unverify</button>
            ) : (
              <button className="btn-verify" aria-label={`Verify lesson: ${ruleName(s)}`}
                      onClick={() => setVerified(s, true)}>Verify</button>
            )}
            <button className="link" aria-label={`Edit lesson: ${ruleName(s)}`}
                    ref={(el) => {
                      if (el) editBtnRefs.current[s.id] = el;
                      else delete editBtnRefs.current[s.id];
                    }}
                    onClick={() => startEdit(s)}>edit</button>
            <button className="link danger" aria-label={`Reject lesson: ${ruleName(s)}`}
                    onClick={() => reject(s)}>reject</button>
            {canMuteCategory(s, categories) && (
              <button className="link danger"
                      aria-label={`Reject & mute ${categoryLabel(s.category, categories)}: ${ruleName(s)}`}
                      onClick={() => rejectAndMute(s)}>
                Reject &amp; mute {categoryLabel(s.category, categories)}
              </button>
            )}
          </div>
          </>
          )}
        </div>
        );
      })}

      {/* A2: muted categories -- collapsed by default, an Unmute action per
          row. Always rendered (even at zero) so the count is a live status,
          not something that appears only once something is muted. */}
      <details className="lesson-desc">
        <summary>{`Muted categories (${mutedCategories.length})`}</summary>
        {mutedCategories.length === 0 ? (
          <p className="muted small">No categories are muted.</p>
        ) : (
          mutedCategories.map((cat) => (
            <div key={cat.token} className="skill">
              <div className="skill-head">
                <span className="lesson-rule">{cat.label}</span>
                <button className="link" aria-label={`Unmute category: ${cat.label}`}
                        onClick={() => unmuteCategory(cat)}>Unmute</button>
              </div>
            </div>
          ))
        )}
      </details>

      {/* A2: rejected-lesson tombstones -- collapsed by default, per-row Undo.
          A load failure renders a visible error INSTEAD of the section (never
          "Rejected (0)", which would read as a confirmed empty result). */}
      {rejectionsErr ? (
        <p className="denied-error" role="alert">{rejectionsErr}</p>
      ) : (
        <details className="lesson-desc">
          {/* NO tabIndex={-1}: a <summary> is natively focusable, and
              removing it from the tab order made this the only way to
              open a section that is collapsed by default — so Undo and
              Clear all were mouse-only (WCAG 2.1.1). The ref still
              works for the post-Undo focus move; programmatic focus
              does not need a negative tabindex on a focusable element. */}
          <summary ref={rejectedSummaryRef}>
            {rejectionCountLabel(rejections, "")}
          </summary>
          {rejections.length === 0 ? (
            <p className="muted small">No lessons have been rejected yet.</p>
          ) : (
            <>
              {rejections.map((r) => {
                const rHeadline = r.headline || r.description || "(no rule text)";
                const rDescription = r.description && r.description !== r.headline
                  ? r.description : "";
                return (
                  <div key={r.id} className="skill">
                    <div className="skill-head">
                      <span className="lesson-rule">{rHeadline}</span>
                      <button className="link" aria-label={`Undo rejection: ${rHeadline}`}
                              onClick={() => undoRejection(r)}>Undo</button>
                    </div>
                    {rDescription && <p className="muted small">{rDescription}</p>}
                  </div>
                );
              })}
              <button className="link" onClick={clearRejections}>Clear all</button>
            </>
          )}
        </details>
      )}
    </div>
  );
}

