import React, { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { api, streamChat } from "./api.js";
import { IconClose, IconEdit, IconRerun, IconSend, IconTrash,
         IconChevronLeft, IconChevronRight, IconPlus, IconWarning } from "./icons.jsx";
import Markdown from "./Markdown.jsx";
import MarkdownTextarea from "./MarkdownTextarea.jsx";
import Figure from "./Figure.jsx";
import Suggestions from "./Suggestions.jsx";
import Clarify from "./Clarify.jsx";
import SqlBlock from "./SqlBlock.jsx";
import CopyMenu from "./CopyMenu.jsx";
import { COPY_FAILED, DELETE_FAILED, deleteAnnouncement } from "./announce.js";
import { copyText } from "./clipboard.js";
import { formatStamp, thoughtLabel } from "./datetime.js";
import { useConfirm } from "./ConfirmModal.jsx";
import { useToast } from "./Toast.jsx";
import { turnErrorMessage } from "./authcopy.js";
import { editConfirmBody, editConfirmLabel, laterTurnsLost } from "./turns.js";
import { inflight } from "./inflight.js";
import { tableTrustNote } from "./tabletruth.js";
import { shouldRedirectTyping, targetInfo } from "./typeahead.js";
import { collectionYearRange } from "./years.js";
import { messageFieldsFromDone } from "./donefields.js";

// Mirrors MAX_QUESTION_LEN in backend/app/routers/chat.py — the browser stops an
// over-long question at the composer so the server's 400 stays a backstop.
const MAX_QUESTION_LEN = 4000;

// Clickable starter prompts ("query slips") shown on the empty chat screen.
// Each carries a small mono tag naming the kind of record it pulls, which
// quietly teaches the data model; `q` is the question the button fills in.
const EXAMPLES = [
  { tag: "Completions · trend",
    q: "How have Computer Science bachelor's degrees changed nationwide over the last five years?" },
  { tag: "Completions · ranking",
    q: "Which undergraduate major produces the most graduates each year?" },
  { tag: "Completions · share",
    q: "What share of bachelor's degrees go to women nationwide?" },
  { tag: "Enrollment · trend",
    q: "Is community college undergraduate enrollment rising or falling?" },
  { tag: "Grad rates · national",
    q: "What's the national six-year college graduation rate?" },
  { tag: "Completions · national",
    q: "How many Registered Nursing degrees did U.S. colleges award last year?" },
];

// Sidebar is user-resizable (drag or arrow keys); width persists in localStorage.
const SIDEBAR_MIN = 200;
const SIDEBAR_MAX = 480;
const SIDEBAR_DEFAULT = 288;
const clampWidth = (w) => Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, Math.round(w)));

// Copy a rendered node as rich HTML (so pasting into email/Word keeps the
// table). Tries the async Clipboard API, then falls back to selecting the node
// and execCommand, which preserves formatting even without a secure context.
// Clone the answer DOM and replace each live chart (Recharts SVG + wrapper divs
// + type buttons, which paste as garbage) with its rasterized PNG, so it lands
// cleanly in Word/Outlook/Docs. A chart with no PNG yet is dropped.
function cleanCloneForCopy(node) {
  const clone = node.cloneNode(true);
  clone.querySelectorAll("figure.chart").forEach((fig) => {
    const exp = fig.querySelector("img.chart-export-img");
    const src = exp?.getAttribute("src");
    // The exported PNG already includes the title (drawn into the SVG), so we
    // just swap the whole figure for the image.
    if (src && src.startsWith("data:image")) {
      const img = document.createElement("img");
      img.setAttribute("src", src);
      const w = exp.getAttribute("data-w");
      if (w) img.setAttribute("width", String(Math.round(Number(w))));
      fig.replaceWith(img);
    } else {
      fig.remove();
    }
  });
  clone.querySelectorAll(".chart-export-img").forEach((n) => n.remove());
  // Drop interactive UI that isn't part of the answer content.
  clone.querySelectorAll(".table-tools").forEach((n) => n.remove());
  return clone;
}

// Chart specs are a rendering directive, not prose — strip them from copied
// text. Require a line break after `chart` so it can't match ```chartjs etc.
const CHART_BLOCK_RE = /```chart[ \t]*\r?\n[\s\S]*?```/g;
function stripChartBlocks(md) {
  return (md || "").replace(CHART_BLOCK_RE, "").replace(/\n{3,}/g, "\n\n").trim();
}

async function copyHtml(node, plain) {
  if (!node) return false;
  const clone = cleanCloneForCopy(node);
  const html = clone.innerHTML || "";
  const text = plain || node.innerText || "";
  try {
    if (navigator.clipboard?.write && window.ClipboardItem) {
      await navigator.clipboard.write([new window.ClipboardItem({
        "text/html": new Blob([html], { type: "text/html" }),
        "text/plain": new Blob([text], { type: "text/plain" }),
      })]);
      return true;
    }
  } catch { /* fall through */ }
  // Fallback (non-secure context, e.g. plain-http LAN): select a temporary
  // off-screen node holding the CLEANED html so the copy excludes the live SVG.
  try {
    const holder = document.createElement("div");
    holder.setAttribute("contenteditable", "true");
    holder.style.cssText = "position:fixed;left:-9999px;top:0;white-space:pre-wrap";
    holder.appendChild(clone);
    document.body.appendChild(holder);
    // Ensure the chart PNGs are decoded before the synchronous copy, so the
    // paste doesn't land as broken images.
    await Promise.all([...holder.querySelectorAll("img")]
      .map((im) => (im.decode ? im.decode().catch(() => {}) : null)));
    const range = document.createRange();
    range.selectNodeContents(holder);
    const sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(range);
    const ok = document.execCommand("copy");
    sel.removeAllRanges();
    document.body.removeChild(holder);
    return ok;
  } catch { return false; }
}

// Renders the agent's live activity: status lines, SQL it ran, model reasoning,
// and tool outcomes. Used both live (under the spinner) and as a collapsible
// "Thoughts" log on the finished message.
function ThinkingTrace({ items }) {
  if (!items?.length) return null;
  return (
    // Focusable + named: the live trace caps at 260px and holds no focusable
    // children, so a keyboard-only user could not read past the first few lines
    // (WCAG 2.1.1, Level A). Only scrollable in the LIVE pending bubble —
    // styles.css unsets the cap inside .trace-panel — which is why the axe scan
    // that covers it has to be taken mid-stream.
    <div className="thought-list thin-scroll" tabIndex={0} role="region"
         aria-label="Thinking trace">
      {items.map((t, j) => {
        if (t.kind === "sql") return <SqlBlock key={j} code={t.text} className="thought-sql" />;
        if (t.kind === "reason") return <p key={j} className="thought-reason">{t.text}</p>;
        return <div key={j} className="thought-line muted">{t.text}</div>;
      })}
    </div>
  );
}

// Whether this answer's numbers were reproduced from the rows its query
// returned — the table's counterpart to the hero figure's "✓ verified".
//
// Rendered as a sibling AFTER <Markdown>, outside the .md node, for the same
// reason <Figure> sits outside it: copying the answer must yield the answer, not
// our annotation. ANSWER-scoped, not per-table — check_table returns one verdict
// for every table in the answer (see tabletruth.js).
//
// POSITIVE-ONLY: tableTrustNote returns null for `partial`/`unmatched`, and that
// silence is measured, not lazy — read the note in tabletruth.js before adding a
// caution here. It is a plain note, deliberately NOT a live region: several
// specs assert a single unscoped getByRole("status"), and a trust mark on a
// settled answer is not an announcement.
// The answer-level verdict on whether its numbers came from the query result.
// The caution's mark is an inline SVG rather than a ⚠ character: the codepoint
// renders as a colour emoji on several platforms, which would drag the eye
// harder than the sentence deserves and clash with the deliberate move away from
// emoji-as-status elsewhere. The ✓ stays a plain glyph — it needs no emphasis,
// and it is already shipped.
function TableTrust({ status, cellsChecked, cellsMatched, hasSql }) {
  const note = tableTrustNote({ status, cellsChecked, cellsMatched, hasSql });
  if (!note) return null;
  return (
    <p className={"table-trust " + note.tone} role="note" title={note.title}>
      {note.tone === "warn"
        ? <IconWarning className="tt-icon" aria-hidden="true" />
        : <span aria-hidden="true">✓ </span>}
      {note.text}
    </p>
  );
}

// What a stopped turn tells the reader, and the way out.
//
// Stop generating is abandon-and-drain: the request keeps running so the server
// still persists the answer. But the client throws its own copy away, so the
// answer only becomes visible on a refetch — and the old note said "reopen it in
// a moment to check", which does not work. Re-clicking the conversation you are
// already in is not a route change, and settleTurn deliberately schedules no
// reload for a stopped turn. The only thing that DID work was a page reload,
// which kills the very turn the note promises will be saved.
//
// So the note now says which of the two states it is in, and offers the check
// only once it can succeed. `live` is the whole gate: while the stream is open
// the answer is not on disk yet, and fetching then would replace the note with
// the thread as it stood BEFORE the question — the reader's own question
// disappearing, which is worse than waiting. `canCheck` additionally excludes a
// conversation with no id (a brand-new chat stopped before the server's
// `conversation` event) and a view that is busy streaming a LATER turn, whose
// finalize writes positionally into `messages` and would land on the refetched
// rows.
function StoppedNote({ live, failed, canCheck, onCheck }) {
  // THREE states, not two. `failed` is the one added after review: the stream
  // is over, but it ENDED BADLY, so nothing was necessarily persisted — the
  // generator may have unwound before _persist, and _delete_if_empty may have
  // removed a new chat's row. Claiming "saved" there is a promise the app
  // cannot keep, and the Check-now refetch would then replace the reader's own
  // question with the thread as it stood before they asked — the exact outcome
  // the `live` gate exists to prevent, reached through the other door.
  if (failed) {
    return (
      <p className="stopped-note">
        Stopped. The request failed, so the answer may not have been saved.
        Ask again to be sure.
      </p>
    );
  }
  return (
    <p className="stopped-note">
      {live
        ? "Stopped. The answer is still being written, and it will be saved to this chat."
        : "Stopped. The answer has been saved to this chat."}
      {!live && canCheck && (
        <>
          {" "}
          <button type="button" className="link" onClick={onCheck}>Check now</button>
        </>
      )}
    </p>
  );
}

// A route :id is only ever a plain conversation id (see api.js); anything
// else (e.g. "abc") is a malformed URL, not a real conversation, and must
// never reach the network -- same notice, zero fetch.
const NUMERIC_ID = /^\d+$/;

// Server rows -> the shape the view uses. The JSON columns arrive as strings.
const hydrate = (msgs) => msgs.map((m) => ({
  ...m,
  sql_log: m.sql_log ? JSON.parse(m.sql_log) : [],
  thinking: m.thinking ? JSON.parse(m.thinking) : [],
  figure: m.figure ? JSON.parse(m.figure) : null,
  suggestions: m.suggestions ? JSON.parse(m.suggestions) : null,
  clarify: m.clarify ? JSON.parse(m.clarify) : null,
}));
const NOT_AVAILABLE = "That conversation isn't available.";

export default function Chat({ me }) {
  const yearRange = collectionYearRange(me?.years);
  const [convos, setConvos] = useState([]);
  const { id: routeId = null } = useParams();
  const navigate = useNavigate();
  const confirm = useConfirm();
  const toast = useToast();
  const [openId, setOpenId] = useState(routeId);
  const [notice, setNotice] = useState("");
  const loadedFor = useRef(null); // routeId this conversation's messages were last fetched for
  const [messages, setMessages] = useState([]); // {role, content, id?, sql_log?, status?}
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [copied, setCopied] = useState(null); // `${i}:${kind}` most recently copied
  // Which message's Thinking/SQL trace is expanded, as `${i}:thinking`/`${i}:sql`
  // (null = none). A single global key makes the two toggles on a message
  // mutually exclusive — opening one closes the other — and renders the panel
  // full-width BELOW the actions row instead of inline (where a native <details>
  // widened its own flex cell and shoved the copy buttons sideways).
  const [openTrace, setOpenTrace] = useState(null);
  // True from a conversation route change until its messages fetch settles —
  // drives the loading skeleton so switching chats never flashes the
  // "What would you like to know" empty state (initial value covers a direct
  // deep-link page load, where the render-time reset below never fires).
  const [loadingConvo, setLoadingConvo] = useState(
    () => routeId !== null && NUMERIC_ID.test(routeId));
  // Inline sidebar rename: which conversation id is being renamed (null =
  // none) + the draft text. renameDone guards the input's blur-commit from
  // double-firing after Enter/Escape already settled it.
  const [renamingId, setRenamingId] = useState(null);
  const [renameText, setRenameText] = useState("");
  const renameDone = useRef(false);
  // Scroll containment: nearBottom tracks whether the viewer is (close to)
  // the bottom of the thread — the auto-scroll effect only follows new
  // content when they are, so scrolling up to read is never yanked away.
  // showJump renders the "Jump to latest" pill while they're scrolled up.
  const nearBottom = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const messagesRef = useRef(null); // the .messages scroll container
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("sidebarCollapsed") === "1");
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const v = parseInt(localStorage.getItem("sidebarWidth"), 10);
    return Number.isFinite(v) ? clampWidth(v) : SIDEBAR_DEFAULT;
  });
  const [resizing, setResizing] = useState(false);
  const [editingIdx, setEditingIdx] = useState(null);
  const [editText, setEditText] = useState("");
  const bottom = useRef(null);
  const taRef = useRef(null);
  const chatRef = useRef(null);
  const editTrigger = useRef(null); // Edit button that opened the inline editor
  const mdRefs = useRef({}); // message index -> rendered markdown DOM node
  // Set by deleteConvo() for the "deleted a DIFFERENT conv" case only --
  // {id} (focus that row) or {newchat:true} (focus "+ New chat", no rows
  // left). Consumed by the [convos] effect below, which is the only place
  // that actually moves focus for that case (see its comment for why).
  const focusAfterDelete = useRef(null);
  // Bumped by handleNewChat() and by any real route change (effect below) to mark
  // whichever stream is currently in flight as abandoned. submit() captures
  // the value at call time; the `conversation` SSE handler compares against
  // it before yanking the viewer to the new /chat/:id -- see the "'+ New
  // chat' mid-stream" fix in submit() below.
  const turnToken = useRef(0);
  // A per-turn identity stamped onto the two messages a turn appends, so a turn
  // that finishes while ABANDONED can still find its own messages.
  //
  // turnToken can't do this job. It marks "is this turn still the live one",
  // which is the right question for VIEW writes but the wrong one for identity:
  // submit() never bumps it, so a turn started AFTER a stop captures the same
  // value the stopped turn is comparing against. Any token-equality check would
  // be true for both, and the stale turn would write its ids onto the new
  // turn's messages (the finalize writes are positional -- c.length-1 /
  // c.length-2 -- so "the last two messages" is whoever is there NOW).
  //
  // The counter itself now lives in inflight.js, minted by startTurn(). It was a
  // useRef seeded at 0, which is fine while keys are component-local -- but Chat
  // UNMOUNTS on /admin, so a remount minted key 1 again while an abandoned turn
  // from the previous mount still held key 1. Harmless then; a real collision
  // now that the same key indexes a module-level map.
  //
  // Everything the registry holds is state that must OUTLIVE this component,
  // which is exactly why it can't be React state: the navigation this feature
  // exists for is the one that unmounts us.
  const inflightSnap = useSyncExternalStore(inflight.subscribe, inflight.getSnapshot);
  // Marks the CURRENT turn's own self-navigation (the "conversation" SSE
  // handler's / -> /chat/:id URL flip for a brand-new conversation) so the
  // [routeId] turnToken effect below doesn't mistake it for the user
  // navigating away and abandon the very turn that's still rendering.
  const selfNavId = useRef(null);
  // Render-readable twin of selfNavId — a ref can't be read during render
  // (react-hooks/refs). Both are set together in the `conversation` handler for a
  // brand-new turn's / -> /chat/:id self-nav; this state gates the render-time
  // reset below (and clears itself there once the flip settles), while the ref is
  // consumed by the [routeId] effect. Two markers because render and effect have
  // different lint-mandated shapes (state vs ref), not two sources of truth.
  const [selfNav, setSelfNav] = useState(null);

  // The URL changed out from under us -- sidebar click, "+ New chat",
  // delete-the-open-chat, or browser Back/Forward -- so reset local thread
  // state to match. This has to happen DURING RENDER, not in an effect:
  // react-hooks/set-state-in-effect is an ERROR in this repo's eslint config,
  // and an effect here would also mean an extra render with stale messages
  // visible before the reset lands.
  // react-router 7 defaults v7_startTransition ON, so the `conversation` handler's
  // navigate() for a brand-new turn defers the routeId flip as a transition: there
  // is an intermediate render where openId (set synchronously) is already the new
  // id but routeId is still the old one. Without a guard the reset below wipes the
  // just-streamed answer, and loadedFor (already set) blocks any refetch — gone
  // permanently, stuck on the skeleton (a11y.spec:79/:144 + the routing-chat/
  // midstream cascade). The handler records the target id in `selfNav` right before
  // navigate(), so skip the reset while THIS route flux is that self-nav.
  const isSelfNav = selfNav !== null
    && (selfNav === routeId || selfNav === openId);
  if (openId !== routeId && !isSelfNav) {
    // Also free the composer in the view navigated TO -- `busy`/`status` are
    // single shared state, not per-turn, so leaving them set would strand
    // the user here until the abandoned turn's stream resolves elsewhere.
    // This deliberately does NOT fire on the happy-path self-nav (guarded above).
    setOpenId(routeId); setMessages([]); setNotice(""); setEditingIdx(null);
    setBusy(false); setStatus("");
    // Entering a conversation route -> skeleton until its fetch settles
    // (the loader effect's callbacks flip it back). Entering "/" -> no load.
    setLoadingConvo(routeId !== null && NUMERIC_ID.test(routeId));
    // A fresh view starts pinned to the latest message (the nearBottom ref
    // itself is reset in the [routeId] effect below — a ref can't legally be
    // written during render).
    setShowJump(false);
  } else if (selfNav !== null && openId === routeId && selfNav === routeId) {
    // The self-nav flip has settled (openId === routeId === target). Clear the
    // marker so a LATER genuine navigation-away isn't mistaken for this one.
    // Set-state DURING render (React re-renders and bails once selfNav is null) --
    // NOT an effect (react-hooks/set-state-in-effect is an error here, and it would
    // leave the stale marker masking the next nav for a frame). The ref twin is
    // cleared independently by the [routeId] effect when it consumes the turn.
    setSelfNav(null);
  }
  const badFormat = routeId !== null && !NUMERIC_ID.test(routeId);
  const showNotice = notice || (badFormat ? NOT_AVAILABLE : "");
  const convId = routeId !== null && !badFormat && !notice ? Number(routeId) : null;

  // Turns still running in THIS conversation that this view isn't already
  // drawing itself. Rendered as a question + spinner after the loaded messages,
  // so leaving a running question and coming back shows something rather than
  // the thread as it was before you asked.
  //
  // The filter is the anti-double-render: a turn stamps `_turn` on the pair it
  // appends, so while its own view is watching it, the placeholder is
  // suppressed. It self-heals in every direction without any extra bookkeeping —
  // navigating clears `messages`, a refetch replaces them with server rows that
  // carry no `_turn`, and edit/rerun slices them off. Same self-scoping argument
  // as the abandoned-turn id lookup in submit().
  const localTurnKeys = new Set(messages.map((m) => m._turn).filter(Boolean));
  // Can this user message be edited or re-run without APPENDING a duplicate?
  //
  // It cannot if it has no server `id`: the request then carries
  // edit_message_id: null, `_persist` skips its `DELETE ... id>=?` and appends,
  // while the client has already sliced its own copy away. A stopped turn is
  // one way to be in that state; it is not the only one, and keying on
  // `inflight.isTurnLive` covered only that one. A turn whose stream THREW
  // settles with rendered:true, so the entry is deleted and the turn reads as
  // finished — but `done` never arrived, so there is still no id, and the
  // assistant-side "Try again" points straight at it. Asking whether the id is
  // actually there covers every route by construction.
  //
  // `!busy` keeps an ordinary streaming turn out of it: its id has not arrived
  // either, but nothing can be submitted mid-stream anyway (Rerun and the
  // editor's Send are both `disabled={busy}`), and gating on it alone made
  // every normal turn render Edit as inert with a tooltip about a stop that
  // never happened.
  //
  // `isTurnLive` scopes it to a turn that is STILL DRAINING — i.e. a stop,
  // where the note promises the answer is being saved and the ids are
  // genuinely on their way. A turn that FAILED also has no id, and gating on
  // `m.id == null` alone therefore blocked "Try again" on every failed turn,
  // which is that button's entire purpose (caught by the inline-error-retry
  // test). Accepted limitation: if a request drops AFTER `_persist` committed
  // but before `done`, retrying appends rather than replaces. The client
  // cannot tell that apart from the ordinary case where nothing was persisted
  // and appending is exactly right — the same ambiguity `settleTurn` documents
  // — and blocking recovery on every real failure to prevent the rare
  // duplicate is the worse trade.
  const turnIdsPending = (m) =>
    !busy && m != null && m.id == null && inflight.isTurnLive(m._turn, inflightSnap);
  const pendingTurns = inflight.pendingFor(convId, inflightSnap)
    .filter((t) => !localTurnKeys.has(t.key));

  // A11y (WCAG 4.1.3): the always-mounted live region that actually announces
  // showNotice. A role="status" node that's already populated at first paint
  // (the sync /chat/abc path) or that mounts brand-new inside an async
  // .catch (the /chat/999 path) is never reliably announced -- same class of
  // bug already fixed for Admin.jsx's flash box (Admin.jsx:249-258). Chat is
  // already mounted across every client-side nav (App.jsx keeps it alive
  // between "/" <-> "/chat/:id"), so this node is already committed/painted
  // BEFORE showNotice changes on any of those navigations -- a plain render
  // mutates the same already-mounted node, which is exactly what a screen
  // reader needs to announce it. (A setTimeout(0) deferral doesn't help the
  // one path that isn't already mounted -- a direct page load of /chat/abc --
  // since the mutation still lands inside the initial-load window screen
  // readers swallow either way.) The visible `.notice` below is deliberately
  // NOT role="status" anymore -- exactly one announcement, not two.
  const refreshConvos = () => api.conversations().then(setConvos).catch(() => {});
  useEffect(() => { refreshConvos(); }, []);
  // Moves focus after deleting a DIFFERENT conversation (case 2 -- deleting
  // the OPEN one is handled directly in deleteConvo() via navigate() + rAF,
  // matching fillExample/saveEdit's precedent, and never touches this ref).
  // `convos` changes for lots of reasons that have nothing to do with a
  // delete -- this mount effect, every submit()'s refreshConvos(), the
  // optimistic title patch -- so the ref is a ONE-SHOT: it's cleared the
  // instant this effect runs, regardless of whether `want` was set, so an
  // unrelated later `convos` update never re-fires the focus move.
  useEffect(() => {
    const want = focusAfterDelete.current;
    if (!want) return;
    focusAfterDelete.current = null;
    const el = (!want.newchat && document.getElementById(`convo-${want.id}`))
      || document.querySelector(".sidebar .newchat, .sidebar .newchat-collapsed")
      || taRef.current;
    el?.focus();
  }, [convos]);
  // A fresh view starts pinned to the latest message. Declared BEFORE the
  // follow effect below so that, on a route-change commit, the pin is reset
  // before the follow decision reads it (effects run in declaration order).
  useEffect(() => { nearBottom.current = true; }, [routeId]);

  // Follow new content only while the viewer is at (or near) the bottom.
  // Scrolled up to read an earlier answer, they stay put — streaming status
  // ticks and the final answer must never yank the view (the pill below is
  // the way back). nearBottom is a ref, not state: scroll position is not
  // render input, and making it state would re-render on every scroll frame.
  useEffect(() => {
    if (!nearBottom.current) return;
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    bottom.current?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
    // pendingTurns.length so the placeholder appearing/disappearing respects
    // the near-bottom pin like any other content change.
  }, [messages, status, pendingTurns.length]);

  // Track whether the viewer is near the bottom of the thread (within ~1.5
  // messages). Drives both the auto-scroll gate above and the pill's
  // visibility.
  function onMessagesScroll() {
    const el = messagesRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    nearBottom.current = near;
    setShowJump(!near && messages.length > 0);
  }

  function jumpToLatest() {
    nearBottom.current = true;
    setShowJump(false);
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    bottom.current?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
  }

  // Fetch a deep-linked/sidebar-selected conversation's messages. Every
  // setState here happens inside the async .then/.catch callback, never sync
  // in the effect body, so this can't collide with the render-time reset
  // above. Skipped entirely (no fetch) for a non-numeric :id -- see
  // NUMERIC_ID/badFormat above -- and for the id the live SSE stream just
  // assigned (loadedFor is set to it directly in the `conversation` event
  // handler below, before the URL flip, precisely so this effect no-ops for
  // an id it already has fully in memory).
  // ...and re-run when an abandoned turn for THIS conversation settles. The
  // counter only ever increases (see inflight.js), because a value that could
  // go back down would make this effect refetch in a loop.
  const reloadSeq = convId === null ? 0 : (inflightSnap.reloads[convId] ?? 0);
  const loadedSeq = useRef(0);
  useEffect(() => {
    if (routeId === null) { loadedFor.current = null; return; }
    // Both halves must agree: the id guard alone would block the reload, and
    // the dep alone would refetch on every unrelated snapshot change.
    if (loadedFor.current === routeId && loadedSeq.current === reloadSeq) return;
    loadedFor.current = routeId;
    loadedSeq.current = reloadSeq;
    if (!NUMERIC_ID.test(routeId)) return;
    let cancelled = false;
    // A full server load supersedes any SETTLED placeholder for this
    // conversation — dropped in the same batch as setMessages, so the
    // placeholder disappears in the same commit the real rows appear and the
    // handover doesn't flicker. Live turns survive it (inflight.js): the fetch
    // returns the thread as it stands now, and a turn that is still running
    // must keep its spinner. Safe to reload on settle because _persist commits
    // BEFORE the `done` event is yielded, so the rows are always there by then.
    const supersede = () => inflight.clearForConversation(Number(routeId));
    api.conversation(routeId)
      .then((msgs) => {
        if (cancelled) return;
        setMessages(hydrate(msgs));
        setLoadingConvo(false);
        // ONLY a fetch that came back with something may supersede the
        // placeholder — there is nothing in an empty one to replace it WITH.
        //
        // Found live: returning mid-flight issues a fetch that correctly
        // returns [] (the turn hasn't persisted yet). If that response lands
        // AFTER the turn settles, the entry is no longer live, so an
        // unconditional clear deleted it and set empty messages in the same
        // batch — the placeholder was replaced by the "What would you like to
        // know" greeting, on a /chat/:id URL, with the answer already on disk.
        //
        // A settled entry surviving an empty fetch is safe: the next fetch that
        // carries the answer clears it. And "settled but genuinely empty" can't
        // really happen — a turn that persisted nothing either had its NEW
        // conversation removed by _delete_if_empty (so this 404s into the catch)
        // or left the earlier messages in place (so this is non-empty).
        if (msgs.length) supersede();
      })
      .catch(() => {
        // The conversation is gone or unreadable; `notice` blanks convId, so no
        // placeholder can render for it anyway. Clearing keeps the registry from
        // holding an entry nothing will ever supersede.
        if (!cancelled) { setNotice(NOT_AVAILABLE); setLoadingConvo(false); supersede(); }
      });
    return () => { cancelled = true; };
  }, [routeId, reloadSeq]);

  // Land focus in the composer on mount and whenever the viewed conversation
  // changes (sidebar click, "+ New chat", Back/Forward) — asking is the
  // page's one job, so the box is always ready. Skipped while an inline
  // prompt-edit is open (its own textarea holds focus).
  useEffect(() => {
    if (editingIdx === null) taRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeId]);

  // "Type anywhere": a printable character typed while nothing editable has
  // focus is redirected into the composer, so a user can just start typing
  // after clicking a sidebar chat (predicate + misfire contract: typeahead.js,
  // vitest-pinned). Focus lands during keydown, BEFORE the browser's default
  // text-insertion runs, so the keystroke itself lands in the box too.
  useEffect(() => {
    function onKey(e) {
      if (editingIdx !== null || renamingId !== null) return;
      if (!shouldRedirectTyping(e, targetInfo(e.target))) return;
      const ta = taRef.current;
      if (ta && document.activeElement !== ta) ta.focus();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [editingIdx, renamingId]);

  // A real route change -- sidebar click to a different chat, browser Back/
  // Forward, delete-the-open-chat -- means the viewer has moved on from
  // whichever turn was in flight when it happened. Bump the token so that
  // turn's `conversation` SSE handler (if it lands later) won't yank them
  // back. (handleNewChat() bumps it directly too, since starting a fresh "/"
  // thread from an already-"/" URL never changes routeId, so this effect
  // alone wouldn't catch that case -- see handleNewChat() below.)
  useEffect(() => {
    // If this route change is the current turn's own self-nav (the
    // `conversation` handler's / -> /chat/:id flip for a brand-new
    // conversation), consume the ref marker and don't abandon the turn. This
    // fires whenever routeId reaches the target — no reliance on navigate()
    // being synchronous (under react-router 7's v7_startTransition it isn't;
    // the render-time reset is guarded separately via the `selfNav` state twin).
    if (selfNavId.current !== null && String(routeId) === selfNavId.current) {
      selfNavId.current = null;
      return;
    }
    turnToken.current++;
  }, [routeId]);

  async function doCopy(i, kind, markdown) {
    const text = stripChartBlocks(markdown);
    const ok = kind === "html"
      ? await copyHtml(mdRefs.current[i], text)
      : await copyText(text);
    // Both helpers SWALLOW their errors and return false, so without this a
    // denied clipboard (or an insecure context, or the execCommand fallback
    // refusing) was indistinguishable from success: no toast, no state change,
    // the trigger still reading "Copy". The user believes they have the answer
    // on the clipboard and pastes something else.
    if (ok) { setCopied(`${i}:${kind}`); setTimeout(() => setCopied(null), 1400); }
    else toast(COPY_FAILED, "error");
  }

  // Toggle a message's Thinking/SQL panel; opening either closes any other
  // (only one key can be set), so the two are mutually exclusive.
  function toggleTrace(i, kind) {
    const key = `${i}:${kind}`;
    setOpenTrace((cur) => (cur === key ? null : key));
  }

  // Pushes a new history entry -- Back from a freshly-opened chat should
  // return to whatever the sidebar/URL showed before, not vanish. The
  // render-time reset above (openId !== routeId) picks up the resulting
  // messages/notice reset once the route param changes. But when the URL is
  // ALREADY "/" (e.g. the has_data:false no-conversation-event guard, or a
  // streamChat() throw before any SSE event lands), routeId stays null,
  // openId===routeId never flips, and that render-time reset never fires --
  // so navigate("/") alone would be a silent no-op AND would push a
  // duplicate "/" history entry. Reset state directly in that case instead.
  function handleNewChat(e) {
    // Modified / middle / right clicks: let the browser open "/" in a NEW tab
    // WITHOUT running this tab's SPA-only side effects.
    if (e.defaultPrevented) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    // Plain left click (the one react-router turns into an in-tab nav to "/").
    // Abandon whichever turn is in flight (see turnToken).
    turnToken.current++;
    if (routeId === null) {
      // Already at "/" -- a Link nav to "/" would push a DUPLICATE history entry
      // and the render-time reset (openId !== routeId) never fires. Suppress the
      // Link's nav and reset thread state directly, as the old newChat() did.
      e.preventDefault();
      setMessages([]); setNotice(""); setEditingIdx(null);
      setBusy(false); setStatus("");
      // Mirror the render-time reset's scroll state: an empty thread has no
      // "latest" to jump to (without this, a pill from a scrolled-up prior
      // thread lingers over the fresh empty state — no scroll event fires to
      // clear it).
      nearBottom.current = true;
      setShowJump(false);
      // The [routeId] focus effect can't fire here (routeId never changes) —
      // land focus in the composer directly, ready for the next question.
      requestAnimationFrame(() => taRef.current?.focus());
    }
    // else: let the Link push "/"; routeId flips, the render-time reset fires.
  }

  // --- Inline sidebar rename -----------------------------------------------
  // Pencil -> the row's title swaps to an input. Enter/blur commit, Escape
  // cancels; commit is optimistic (title updates instantly, reverted with a
  // toast if the PATCH fails). renameDone guards blur from re-committing
  // after Enter/Escape already settled the edit in the same tick.
  function startRename(c) {
    renameDone.current = false;
    setRenamingId(c.id);
    setRenameText(c.title || "Untitled");
  }
  // After the input unmounts, focus would drop to <body> (WCAG 2.4.3) — hand
  // it back to the row's own link on the next frame instead.
  const refocusRow = (id) => requestAnimationFrame(() =>
    document.getElementById(`convo-${id}`)?.focus());
  function cancelRename(c) {
    if (renameDone.current) return;
    renameDone.current = true;
    setRenamingId(null); setRenameText("");
    refocusRow(c.id);
  }
  function commitRename(c) {
    if (renameDone.current) return;
    renameDone.current = true;
    const title = renameText.trim();
    setRenamingId(null); setRenameText("");
    refocusRow(c.id);
    // Unchanged or emptied -> a cancel, not a rename (the server would 400 an
    // empty title anyway; don't round-trip a no-op).
    if (!title || title === (c.title || "Untitled")) return;
    const prev = c.title;
    setConvos((cs) => cs.map((x) => (x.id === c.id ? { ...x, title } : x)));
    api.renameConversation(c.id, title).catch(() => {
      setConvos((cs) => cs.map((x) => (x.id === c.id ? { ...x, title: prev } : x)));
      toast("Couldn't rename the chat. Try again.", "error");
    });
  }

  function toggleSidebar() {
    setCollapsed((v) => { const n = !v; localStorage.setItem("sidebarCollapsed", n ? "1" : "0"); return n; });
  }

  const persistWidth = (w) => { const c = clampWidth(w); setSidebarWidth(c); localStorage.setItem("sidebarWidth", String(c)); };

  // Drag the divider to resize the sidebar; width persists on release.
  function startResize(e) {
    e.preventDefault();
    setResizing(true);
    const left = chatRef.current?.getBoundingClientRect().left ?? 0;
    let w = sidebarWidth;
    const onMove = (ev) => { w = clampWidth(ev.clientX - left); setSidebarWidth(w); };
    const onUp = () => {
      setResizing(false);
      localStorage.setItem("sidebarWidth", String(w));
      document.body.style.userSelect = "";
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }
  // Keyboard resize for the separator (arrow keys nudge the width).
  function resizeKey(e) {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      persistWidth(sidebarWidth + (e.key === "ArrowLeft" ? -16 : 16));
    }
  }

  // Drop an example prompt into the composer and focus it (user reviews, then sends).
  function fillExample(text) {
    setInput(text);
    requestAnimationFrame(() => taRef.current?.focus());
  }

  function send(e) {
    e?.preventDefault();
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    submit(q);
  }

  // Stop generating: abandon the in-flight turn exactly the way navigating
  // away already does (turnToken), then mark the pending bubble "stopped" and
  // free the composer. Deliberately NO network abort — the request keeps
  // draining in the background, so the server still finishes and PERSISTS the
  // answer (an aborted mid-turn request is the known server-side data-loss
  // path; see the backlog note on chat.py's pre-gen() writes). Reopening the
  // chat later shows the completed answer — the stopped note says so.
  function stopGenerating() {
    turnToken.current++;
    // Drop the placeholder but leave the stream marked live.
    //
    // Without this the stopped turn would settle like any abandoned one,
    // schedule a reload, and the finished answer would replace the "Stopped."
    // note — pulling a full answer in under someone who deliberately stopped
    // watching, which is the same yank the scroll containment exists to prevent.
    // `live` stays true so the unload guard remains armed: this note PROMISES
    // the answer will be saved, and refreshing now is what breaks that promise.
    const stoppedKey = messages[messages.length - 1]?._turn;
    if (stoppedKey) inflight.hideTurn(stoppedKey);
    setBusy(false); setStatus("");
    setMessages((m) => {
      const c = [...m]; const i = c.length - 1;
      if (i >= 0 && c[i].pending) c[i] = { ...c[i], pending: false, stopped: true };
      return c;
    });
    requestAnimationFrame(() => taRef.current?.focus());
  }

  // "Check now" on a stopped bubble: refetch this conversation so the answer the
  // drained stream saved actually appears. The refetch REPLACES the note, so the
  // button unmounts and focus would fall to <body>; the composer is the one node
  // that never unmounts here, and it is where this app parks focus after every
  // other completed action (fillExample, saveEdit, stopGenerating). Focused
  // synchronously rather than after the fetch, so it holds regardless of how the
  // load lands. The answer itself is announced by the bubble's aria-live region.
  function checkStoppedTurn() {
    inflight.reloadNow(convId);
    taRef.current?.focus();
  }

  // Edit a prior prompt inline, then re-run it — replacing that exchange and
  // everything after it (both in the UI and server-side). We remember the
  // trigger button so focus can return to it when the editor closes (a11y).
  function startEdit(i, content, trigger) {
    editTrigger.current = trigger || null;
    setEditingIdx(i); setEditText(content);
  }
  function cancelEdit() {
    setEditingIdx(null); setEditText("");
    requestAnimationFrame(() => editTrigger.current?.focus?.());
  }
  // Re-asking a prompt drops that turn AND everything after it, here and
  // server-side (_persist's DELETE ... id>=?), permanently. On the LAST turn
  // that's the ordinary refine gesture and costs nothing the user still wants,
  // so it stays modal-free — that path also carries the assistant-side "Try
  // again" button. Re-asking an EARLIER one silently destroys the analysis
  // below it, so it confirms first, naming the count. `confirm()` is not
  // awaitable (it just opens the dialog); the work happens in onConfirm, which
  // must NOT return submit()'s promise or the modal would sit spinning for the
  // whole streamed answer.
  function confirmDestructive(lost, verb, apply) {
    if (!lost) { apply(); return; }
    confirm({
      variant: "warning",
      title: "Remove the later questions?",
      body: editConfirmBody(lost),
      confirmLabel: editConfirmLabel(lost, verb),
      onConfirm: () => { apply(); },
    });
  }
  function saveEdit(i) {
    const text = editText.trim();
    if (!text || busy) return;
    const editMessageId = messages[i]?.id;
    // Cancelling must leave the editor open with the typed text intact, so the
    // editor is only torn down once the action is actually going ahead.
    confirmDestructive(laterTurnsLost(messages, i), "Edit", () => {
      setEditingIdx(null); setEditText("");
      setMessages((m) => m.slice(0, i));   // drop this turn + everything after
      submit(text, { editMessageId });
      requestAnimationFrame(() => taRef.current?.focus());  // land focus in composer
    });
  }
  // Asking from a suggestion / clarify chip.
  //
  // The chips are `disabled={busy}`, and submit() sets busy in the same tick —
  // so the chip the user just activated disables itself WHILE FOCUSED and
  // focus falls to <body>, dumping a keyboard or screen-reader user to the top
  // of the document for the whole generation (WCAG 2.4.3).
  //
  // Worst on a clarify: those chips are the only UI for its answer phrases, so
  // a user answering a blocking disambiguation is the one most likely to be
  // navigating by keyboard through them. The composer is the documented
  // free-text escape hatch for exactly these, which makes it the right landing
  // spot rather than an arbitrary one.
  //
  // Focus moves BEFORE the state change, mirroring BulkBar's onFocusFallback.
  function askFromChip(q) {
    taRef.current?.focus();
    submit(q);
  }
  // Rerun a prior prompt as-is (e.g. after a failure), replacing from that point.
  function rerun(i) {
    if (busy) return;
    const msg = messages[i];
    if (!msg) return;
    confirmDestructive(laterTurnsLost(messages, i), "Rerun", () => {
      setMessages((m) => m.slice(0, i));
      submit(msg.content, { editMessageId: msg.id });
      // Land focus in the composer, exactly as saveEdit does. Rerun is
      // `disabled={busy}`, so submit() disables the very button that was just
      // activated and focus falls to <body> — the user is dumped to the top of
      // the document for the whole generation (WCAG 2.4.3).
      //
      // It also closes a second path: on the CONFIRM route, ConfirmModal
      // correctly returns focus to its opener, but the opener is disabled by
      // then, so the modal's own a11y still ends at <body>.
      requestAnimationFrame(() => taRef.current?.focus());
    });
  }

  function deleteConvo(e, id) {
    e.stopPropagation();
    // Snapshot everything the post-delete UI needs from THIS render's convos
    // at confirm-request time. idx/next/remaining/isOpen/title are captured in
    // the onSuccess closure below, so they reflect the pre-delete list even
    // though refreshConvos() (which resolves when setConvos is called, not
    // after commit) runs later.
    const idx = convos.findIndex((c) => c.id === id);
    const title = (idx >= 0 ? convos[idx].title : "") || "Untitled";
    const isOpen = id === convId;
    const next = convos[idx + 1] || convos[idx - 1] || null;
    const remaining = Math.max(convos.length - 1, 0);
    confirm({
      variant: "danger",
      title: `Delete "${title}"?`,
      body: "This will permanently delete the chat and all of its messages. This action cannot be undone.",
      confirmLabel: "Delete chat",
      onConfirm: () => api.deleteConversation(id), // throws -> in-modal error + DELETE_FAILED toast
      successToast: deleteAnnouncement({ title, open: isOpen, remaining }),
      errorToast: DELETE_FAILED,
      onSuccess: () => {
        if (isOpen) {
          // Deleting the OPEN conversation: focus the composer via the same
          // navigate()+rAF precedent as fillExample/saveEdit -- deliberately
          // NOT through focusAfterDelete/the [convos] effect, which targets a
          // sidebar row on a different clock (refreshConvos() landing later
          // would otherwise steal focus back out of the composer).
          navigate("/");
          requestAnimationFrame(() => taRef.current?.focus());
        } else {
          // Deleting a DIFFERENT conversation: focus whatever now occupies the
          // deleted row's index once refreshConvos() commits (the [convos]
          // effect). The announcement's remaining-count is load-bearing for
          // re-announcement -- see announce.js.
          focusAfterDelete.current = next ? { id: next.id } : { newchat: true };
        }
        refreshConvos();
      },
    });
  }

  async function submit(q, { editMessageId = null } = {}) {
    q = (q || "").trim();
    if (!q || busy) return;
    // Your own question always scrolls into view. This lives HERE rather than
    // in send() because the invariant belongs to asking, and submit() has four
    // callers: send, a suggestion/clarify chip, Rerun and Save-edit. Only send
    // re-pinned, so from a scrolled-up thread a chip appended its new turn
    // off-screen and merely changed the "Latest" pill — the click read as doing
    // nothing. Putting it at the one place they all pass through means the next
    // caller inherits it instead of having to remember.
    nearBottom.current = true;
    const myTurn = turnToken.current; // see the `conversation` SSE handler below
    // True only while this is still the turn the user is looking at. Stale
    // (abandoned) turns must keep draining the stream to completion -- see
    // the note atop submit() -- but their VIEW writes must stop the instant
    // the user has moved on, so they don't bleed into whatever conversation
    // is now on screen.
    const isMine = () => turnToken.current === myTurn;
    // This turn's own identity, stamped on the two messages it appends below so
    // it can find them again even after being abandoned. Client-only: never
    // sent to the server, never persisted, and absent from server-loaded rows —
    // which is exactly what makes the lookup self-scoping (see the finalize).
    const turnKey = inflight.startTurn({ question: q, conversationId: convId });
    setBusy(true); setStatus("Thinking…");
    // Stamp the user turn with the client send time (unix seconds) — displayed in
    // the viewer's own tz; the server persists a near-identical created_at.
    setMessages((m) => [...m, { role: "user", content: q, created_at: Math.floor(Date.now() / 1000), _turn: turnKey },
                              { role: "assistant", content: "", sql_log: [], thinking: [], pending: true, _turn: turnKey }]);

    // Immutably patch the in-flight (last) message.
    const patchLast = (patch) => setMessages((m) => {
      const c = [...m]; const i = c.length - 1;
      if (i >= 0) c[i] = typeof patch === "function" ? patch(c[i]) : { ...c[i], ...patch };
      return c;
    });
    const addThought = (item) =>
      patchLast((last) => ({ ...last, thinking: [...(last.thinking || []), item] }));

    let answer = "", sqlLog = [], newConvId = convId, msgId = null, userMsgId = null, newTitle = null;
    // Everything else the `done` event carries onto the message (duration_ms,
    // results_truncated, figure_grounding, table_grounding, ...) accumulates
    // here rather than as one local per field — see messageFieldsFromDone,
    // the one merge point that replaces what used to be six hand-named locals.
    let doneFields = {};
    let figure = null; // the structured hero statistic, when the model emitted one
    let suggestions = null; // drill-down "you might also ask" questions
    let clarify = null; // disambiguation {question, options[]}, when the model asked instead of answering
    let failed = false; // drives the finalized message's inline "Try again"
    // ...and, separately, whether the request itself THREW. `failed` is also
    // set by a server-emitted {"type":"error"} event, and that turn IS on disk:
    // chat.py persists `answer or (result.error or "")` with ok=False and then
    // yields `done`. Only the catch below means nothing was written.
    let threw = false;
    try {
      await streamChat({ question: q, conversationId: convId, editMessageId }, (ev) => {
        if (ev.type === "conversation") {
          newConvId = ev.id;
          // BOTH of these run ABOVE the abandonment gate below, deliberately.
          //
          // A brand-new chat's turn starts with no conversation id at all -- the
          // server mints it and announces it here. If this backfill were gated,
          // an abandoned first turn would keep convId: null forever, so it could
          // never be drawn in any conversation and never schedule its reload:
          // the exact case with the worst symptom (nothing in the sidebar, so
          // nowhere to go back TO).
          inflight.attachConversation(turnKey, ev.id);
          // ...and put it in the sidebar straight away, so an abandoned first
          // turn is reachable while it runs. The row already exists server-side
          // (chat.py creates it at the top of the generator, titled from the
          // question), so this is a plain re-list, not a new write. Only for a
          // brand-new chat: a follow-up adds no row, and re-listing on every
          // turn would be a pointless GET that also races the model-generated
          // title patch at the end of this function.
          if (convId === null) refreshConvos();
          // The viewer may have already abandoned this turn -- "+ New chat"
          // mid-stream, a sidebar click to a different chat, browser Back/
          // Forward, or deleting the open chat -- before this event landed.
          // turnToken is bumped by handleNewChat() and by any real route change
          // (effect above); if it no longer matches what this turn captured
          // at the top of submit(), silently drop the "open/navigate to it"
          // side effects below. The turn still finishes normally in the
          // background -- refreshConvos() after the stream still lists the
          // new conversation in the sidebar -- only the yank-the-viewer-back
          // behavior is suppressed.
          if (turnToken.current !== myTurn) return;
          // setNotice, loadedFor, and setOpenId below are ALL load-bearing,
          // and all three must run before navigate(): React 18's createRoot
          // auto-batches every state update from this one event-handler tick
          // into a SINGLE render, in which openId === routeId already (the
          // :id param and openId flip to the same new value together) -- so
          // the render-time reset above never fires and the just-streamed
          // answer stays on screen. setNotice clears a stale notice left
          // over from a bad deep link (e.g. /chat/999 -> 404) -- otherwise it
          // would float above the thread forever, since the render-time
          // reset that normally clears it never fires here, for the same
          // reason. loadedFor stops the loader effect from refetching a
          // conversation the client already has fully in memory; setOpenId
          // stops the render-time reset from wiping it.
          setNotice("");
          // LOAD-BEARING PRECONDITION: under react-router 7, v7_startTransition is
          // the DEFAULT, so this navigate() defers the routeId flip as a transition
          // and commits openId FIRST — the render-time reset above would then see
          // openId/routeId diverge and wipe the just-streamed answer (loadedFor is
          // already set, so the wipe is permanent — no refetch). The `selfNav`
          // marker set here is what lets that reset skip this self-nav until the
          // flip settles. Do NOT assume navigate() is synchronous (it isn't in v7),
          // and do NOT drop the marker. Regression pinned by routing-chat + a11y.
          loadedFor.current = String(ev.id);
          setOpenId(String(ev.id));
          // Only a brand-new conversation (convId === null) actually flips
          // routeId here -- an existing-conversation turn's routeId is
          // already correct, so marking self-nav for it would linger
          // unconsumed and mask a LATER genuine navigation-away.
          if (convId === null) { selfNavId.current = String(ev.id); setSelfNav(String(ev.id)); }
          navigate(`/chat/${ev.id}`, { replace: true });
        }
        else if (ev.type === "status") {
          // Gated: a stale turn's status text must not overwrite whatever
          // the now-viewed conversation is showing (or isn't).
          if (isMine()) { setStatus(ev.text); addThought({ kind: "status", text: ev.text }); }
        }
        else if (ev.type === "sql") {
          sqlLog = [...sqlLog, ev.sql]; // local accumulation stays ungated -- needed for the finalization write below
          if (isMine()) { setStatus("Running query…"); addThought({ kind: "sql", text: ev.sql }); }
        }
        else if (ev.type === "thinking") { if (isMine()) addThought({ kind: "reason", text: ev.text }); }
        else if (ev.type === "tool") { if (isMine()) addThought({ kind: "tool", text: `${ev.name}${ev.ok ? " ✓" : " ✗"}` }); }
        else if (ev.type === "answer") answer = ev.text;
        else if (ev.type === "figure") figure = ev.figure; // structured hero stat, rendered above the prose
        else if (ev.type === "suggestions") suggestions = ev.suggestions; // drill-down chips below the answer
        else if (ev.type === "clarify") clarify = ev.clarify; // disambiguation chips, no figure/suggestions on this turn
        else if (ev.type === "error") { answer = ev.text; failed = true; }
        else if (ev.type === "done") {
          if (ev.message_id) msgId = ev.message_id;
          if (ev.user_message_id) userMsgId = ev.user_message_id;
          // Every OTHER answer field the done event carries (duration_ms,
          // results_truncated, figure_grounding, table_grounding, ...) —
          // merged, not named, so a server field this file has never heard of
          // still reaches the message (see donefields.js).
          doneFields = { ...doneFields, ...messageFieldsFromDone(ev) };
          if (ev.title) newTitle = ev.title;
        }
      });
    } catch (err) {
      // NEVER the raw response body. This used to be `"⚠️ " + err.message`,
      // where message was the unparsed body — so an ordinary rate-limit reached
      // the user as ⚠️ {"detail":"Too many requests — please slow down…"},
      // JSON braces and all, and an expired session as ⚠️ {"detail":"Not signed
      // in."} with no way to act on it. turnErrorMessage keys on the status so
      // the expected failures read as conditions, not as breakage.
      answer = turnErrorMessage(err?.status, err?.detail);
      failed = true;
      threw = true;
    }
    // The stream is done (or threw). One call, one join point, both paths.
    //
    // `rendered` says the owning view painted the result itself, so there is
    // nothing to reload -- that is what keeps a turn from refetching the very
    // conversation it just created (pinned by midstream-nav's conv7.calls === 0).
    // Otherwise the entry stays as a placeholder and schedules exactly one
    // reload for whoever is looking at that conversation.
    //
    // Deliberately NOT keyed on `failed`: _persist commits BEFORE the `done`
    // event is yielded, so a connection drop between the two throws here while
    // the answer IS on disk. Reloading and letting the server say what exists is
    // the only reading that can't be wrong.
    inflight.settleTurn(turnKey, { rendered: isMine() });
    // VIEW writes -- gated: a stale (abandoned) turn's final answer must not
    // land in whatever conversation is now on screen, and must not leave
    // that conversation's composer stuck disabled. The stream still drained
    // to completion above, so the answer IS persisted server-side; reopening
    // that conversation will show it.
    if (isMine()) {
      setMessages((m) => {
        const c = [...m];
        const ai = c.length - 1, ui = c.length - 2;
        // ORDER IS LOAD-BEARING: `results_truncated: false` is the only
        // default a `done` field needs (every other field already reads as
        // "nothing" when absent/null — see donefields.js), and it sits
        // BEFORE the spread so a present value wins over it. Every key the
        // turn itself owns (role/content/sql_log/figure/suggestions/clarify/
        // id/pending/error) sits AFTER the spread, so no key the server adds
        // to `done` in the future can ever clobber the streamed answer, the
        // parsed sub-objects, or the message's identity.
        if (ai >= 0) c[ai] = { ...c[ai], results_truncated: false, ...doneFields, role: "assistant", content: answer, sql_log: sqlLog, figure, suggestions, clarify, id: msgId ?? c[ai].id, pending: false, error: failed };
        if (ui >= 0 && userMsgId) c[ui] = { ...c[ui], id: userMsgId };
        return c;
      });
      setBusy(false); setStatus("");
    } else {
      // ABANDONED turn -- almost always "Stop generating", which bumps
      // turnToken without the viewer going anywhere.
      //
      // Write the server ids and NOTHING else. The ids are identity, not
      // content: without them the stopped turn's user message has no `id`, so
      // Rerun sends editMessageId: undefined -> chat.py sets edit_from = None
      // -> _persist skips its DELETE and APPENDS. The client had already done
      // slice(0, i), so the DB grows a duplicate of the question the user was
      // trying to replace, and nothing says so (a stopped turn is the LAST
      // turn, so laterTurnsLost() is 0 and no confirmation fires).
      //
      // Deliberately NOT written: content, pending, stopped, error, busy,
      // status. The user chose to stop; pulling the finished answer in under
      // them is the same yank the scroll containment exists to prevent.
      //
      // Targeted by LOOKUP, never by position, and the lookup IS the scope
      // check -- there is no separate "is this still the right conversation?"
      // test to get wrong. Navigating away clears messages (the render-time
      // reset), "+ New chat" clears them, edit/rerun slices them off, and a
      // refetch replaces them with server rows that carry no _turn. In every
      // one of those cases findIndex misses and this is a no-op.
      setMessages((m) => {
        const ai = m.findIndex((x) => x._turn === turnKey && x.role === "assistant");
        const ui = m.findIndex((x) => x._turn === turnKey && x.role === "user");
        if (ai < 0 && ui < 0) return m;
        const c = [...m];
        if (ai >= 0 && msgId) c[ai] = { ...c[ai], id: msgId };
        if (ui >= 0 && userMsgId) c[ui] = { ...c[ui], id: userMsgId };
        // ...and, when the stream THREW, one more identity-shaped fact: that
        // it did. Not content — the stopped note reads it to stop claiming
        // the answer was saved. `settleTurn` runs on both branches and
        // deliberately does not key on `failed` (see its call site: a drop
        // between _persist and `done` leaves the answer ON disk, so reloading
        // and letting the server say what exists is the only reading that
        // can't be wrong). But an error BEFORE _persist — a 429, an expired
        // session, an early transport failure — persisted nothing, and the
        // note was asserting "saved" for those too.
        if (threw && ai >= 0) c[ai] = { ...c[ai], stoppedFailed: true };
        return c;
      });
    }
    // Ungated -- these touch only the sidebar list/titles, never the viewed
    // thread, and stay useful even for an abandoned-but-persisted turn (its
    // new conversation should still show up in the sidebar).
    refreshConvos();
    // Optimistically show the model-generated conversation title right away.
    if (newTitle && newConvId) {
      setConvos((cs) => cs.map((c) => (c.id === newConvId ? { ...c, title: newTitle } : c)));
    }
  }

  return (
    <div className="chat" ref={chatRef}>
      <aside className={"sidebar" + (collapsed ? " collapsed" : "") + (resizing ? " resizing" : "")}
             style={collapsed ? undefined : { width: sidebarWidth }}>
        <div className="sidebar-head">
          <button className="icon-btn" onClick={toggleSidebar}
                  title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
                  aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
                  aria-expanded={!collapsed}>
            {collapsed ? <IconChevronRight /> : <IconChevronLeft />}
          </button>
          {!collapsed && (
            <Link to="/" className="newchat" onClick={handleNewChat}>
              <IconPlus size={15} />New chat
            </Link>
          )}
        </div>
        {collapsed ? (
          <Link to="/" className="icon-btn newchat-collapsed" onClick={handleNewChat}
                title="New chat" aria-label="New chat"><IconPlus /></Link>
        ) : (
          <div className="convo-list thin-scroll">
            {convos.map((c) => (
              <div key={c.id} className={"convo-row" + (c.id === convId ? " on" : "")}>
                {renamingId === c.id ? (
                  <input
                    className="convo-rename" value={renameText} autoFocus
                    maxLength={200}
                    aria-label={`Rename chat: ${c.title || "Untitled"}`}
                    onFocus={(e) => e.target.select()}
                    onChange={(e) => setRenameText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") { e.preventDefault(); commitRename(c); }
                      else if (e.key === "Escape") cancelRename(c);
                    }}
                    onBlur={() => commitRename(c)}
                  />
                ) : (
                  <>
                    <Link to={`/chat/${c.id}`} id={`convo-${c.id}`}
                          className={"convo" + (c.id === convId ? " on" : "")}
                          title={c.title || "Untitled"}
                          aria-current={c.id === convId ? "page" : undefined}>
                      {c.title || "Untitled"}
                    </Link>
                    <div className="convo-actions">
                      <button type="button" className="convo-act"
                              onClick={() => startRename(c)}
                              title="Rename chat"
                              aria-label={`Rename chat: ${c.title || "Untitled"}`}><IconEdit /></button>
                      <button type="button" className="convo-act convo-del"
                              onClick={(e) => deleteConvo(e, c.id)}
                              title="Delete chat"
                              aria-label={`Delete chat: ${c.title || "Untitled"}`}><IconTrash /></button>
                    </div>
                  </>
                )}
              </div>
            ))}
          </div>
        )}
      </aside>

      {!collapsed && (
        <div className="sidebar-resizer" role="separator" aria-orientation="vertical"
             tabIndex={0} aria-label="Resize sidebar"
             aria-valuenow={sidebarWidth} aria-valuemin={SIDEBAR_MIN} aria-valuemax={SIDEBAR_MAX}
             title="Drag to resize (or use arrow keys)"
             onMouseDown={startResize} onKeyDown={resizeKey} />
      )}

      <main className="thread">
        <h1 className="sr-only">Chat</h1>
        <div className="messages thin-scroll" ref={messagesRef} onScroll={onMessagesScroll}>
          <div className="messages-inner">
          {/* Rendered ABOVE the empty state, URL left as-is -- navigating away
              would re-run the render-time reset above and wipe the notice
              right when the user needs to see it. Never renders the server's
              `detail` (see NOT_AVAILABLE): a 404 (doesn't exist) and a 403
              (not yours) must read identically so this can't be used to
              enumerate other users' conversation ids. */}
          {showNotice && <div className="notice">{showNotice}</div>}
          <div className="sr-only" role="status" aria-live="polite">{showNotice}</div>
          {/* A11Y-6: a non-visual progress announcement. The streaming bubble is
              aria-busy while pending, and a screen reader skips a busy region's
              content — so the visible "Thinking…/Running query…" status inside it
              is never spoken. Mirror it here, OUTSIDE any busy region, so the
              wait is audible; it clears when the turn settles (the bubble then
              announces the finished answer as its aria-busy flips to false).
              A plain aria-live region, NOT role="status" — the Chat page keeps
              exactly one role=status.sr-only node (the bad-conversation notice,
              pinned in routing-chat/delete-focus specs); aria-live="polite" +
              aria-atomic is the same announcement to AT without a second status. */}
          <div className="sr-only" aria-live="polite" aria-atomic="true"
               data-testid="generating-status">
            {busy ? (status || "Generating response…")
              : pendingTurns.length ? "Still working on your earlier question…" : ""}
          </div>
          {/* Switching chats: skeleton bubbles until the fetch settles, so the
              empty-state prompt never flashes over a conversation that's
              merely loading. aria-hidden — the sr experience is the (quiet)
              moment before messages render, not three fake gray bars. */}
          {loadingConvo && messages.length === 0 && !showNotice && (
            <div className="convo-skeleton" aria-hidden="true" data-testid="convo-skeleton">
              <div className="skel skel-user" />
              <div className="skel skel-answer" />
              <div className="skel skel-answer short" />
            </div>
          )}
          {/* BOTH empty states belong to the NO-CONVERSATION route, and that is
              the whole gate. They are the "you haven't asked anything yet"
              screen, so rendering them on a /chat/:id URL means the index page
              is impersonating someone's conversation.
              FOUND LIVE: the in-flight placeholder was replaced by this
              greeting — heading, blurb and six example chips — on a /chat/:id
              URL whose answer was already saved. `messages` being momentarily
              empty on a conversation route is a transient the view must ride
              out (the loader is mid-flight, or a turn hasn't persisted yet),
              never a cue to offer a fresh start. Keyed on routeId, NOT convId:
              convId is also null for a malformed id, where the notice is the
              right thing to show and this is not.
              This also stops a failed load rendering "That conversation isn't
              available." AND "What would you like to know" together — a
              contradiction the skeleton already guarded against with
              !showNotice and these two never did. */}
          {routeId === null && messages.length === 0 && !me?.has_data && (
            <div className="empty">
              <h2>No IPEDS data loaded yet</h2>
              <p>
                {me?.is_admin
                  ? "No IPEDS data is loaded yet. Head to the Admin → Imports tab "
                    + "to load a year, then come back to ask questions."
                  : "No IPEDS data is loaded yet. An administrator needs to load "
                    + "a dataset before you can ask questions — please check back soon."}
              </p>
            </div>
          )}
          {routeId === null && messages.length === 0 && me?.has_data && (
            <div className="empty">
              <span className="field-label">Ask the record</span>
              <h2 className="empty-prompt">What would you like to know about U.S. colleges?</h2>
              {/* The range comes from /me (i.e. from `_years`), never a literal
                  — each deployment loads its own years via Admin → Imports, so a
                  hardcoded span is wrong everywhere but the box it was written
                  on. Falls back to the sentence without a range if /me is old or
                  the bounds are unusable, rather than printing a broken clause. */}
              <p className="muted">
                Degrees awarded, enrollment, tuition, graduation rates, staffing
                and finance{yearRange ? ` — across ${yearRange}` : ""}.
              </p>
              <div className="examples-grid">
                {EXAMPLES.map((ex) => (
                  <button key={ex.q} type="button" className="example-chip"
                          onClick={() => fillExample(ex.q)}>
                    <span className="chip-tag">{ex.tag}</span>
                    {ex.q}
                  </button>
                ))}
              </div>
              {!me?.trust_llm_provider && (
                <p className="privacy-warning" role="note">
                  Public IPEDS data only — no student records, confidential
                  figures, or other non-public information. Questions are sent
                  to a third-party model that may use them to improve its service.
                </p>
              )}
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={"msg " + m.role + (m.error ? " failed" : "") + (editingIdx === i ? " editing" : "")}>
              <div className="bubble">
                {m.role === "assistant" ? (
                  <div aria-live="polite" aria-busy={!!m.pending}
                       ref={(el) => { mdRefs.current[i] = el?.querySelector(".md") || el; }}>
                    {m.pending && !m.content ? (
                      <div className="thinking-live">
                        <div className="thinking-head">
                          <span className="spinner" aria-hidden="true" />
                          <span className="muted">{status || "Thinking…"}</span>
                        </div>
                        {/* Same controlled trace-toggle as the settled answer
                            below (a <button> + aria-expanded + panel), NOT a
                            native <details> — one disclosure mechanic app-wide
                            (P1). Keyed on the pending message's own index. */}
                        {m.thinking?.length > 0 && (
                          <>
                            <button type="button" className="link trace-toggle"
                                    aria-expanded={openTrace === `${i}:thinking`}
                                    aria-controls={`trace-${i}`}
                                    onClick={() => toggleTrace(i, "thinking")}>Thinking</button>
                            {openTrace === `${i}:thinking` && (
                              <div className="trace-panel" id={`trace-${i}`}>
                                <ThinkingTrace items={m.thinking} />
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    ) : m.stopped ? (
                      <StoppedNote live={inflight.isTurnLive(m._turn, inflightSnap)}
                                   failed={!!m.stoppedFailed}
                                   canCheck={convId != null && !busy}
                                   onCheck={checkStoppedTurn} />
                    ) : (
                      <>
                        {/* Sibling BEFORE <Markdown> (outside the .md node
                            mdRefs targets), so the hero figure sits above the
                            prose and stays out of the copy surface. Renders
                            null when the message carries no figure. */}
                        <Figure spec={m.figure} grounding={m.figure_grounding} />
                        {/* hasSql gates the SERVER-side CSV. A turn that
                            reshapes an earlier table from context runs no SQL
                            (sql_log is []), so the server has nothing to
                            re-run — offering it there produced a button whose
                            only outcome was "No query is associated with this
                            answer." The table is still on screen, so it falls
                            back to the client-side export of those rows. */}
                        <Markdown messageId={m.id} hasSql={m.sql_log?.length > 0}
                                  resultsTruncated={!!m.results_truncated}
                                  rowCap={me?.sql_row_cap}>{m.content || ""}</Markdown>
                        {/* hasSql: grounding is conversation-scoped, so a turn
                            that reshapes an earlier table is checked against
                            THAT turn's rows. Same claim, different source — and
                            the note has to say which, or it points the reader at
                            a SQL disclosure this answer doesn't have. */}
                        <TableTrust status={m.table_grounding}
                                    cellsChecked={m.table_cells_checked}
                                    cellsMatched={m.table_cells_matched}
                                    hasSql={m.sql_log?.length > 0} />
                      </>
                    )}
                  </div>
                ) : editingIdx === i ? (
                  <div className="edit-box">
                    <MarkdownTextarea value={editText} autoFocus aria-label="Edit prompt"
                      onChange={(e) => setEditText(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); saveEdit(i); }
                        else if (e.key === "Escape") cancelEdit();
                      }} />
                    <div className="edit-actions">
                      <button className="link ico" onClick={cancelEdit}>
                        <IconClose />Cancel
                      </button>
                      <button className="send-sm ico" onClick={() => saveEdit(i)}
                              disabled={busy || !editText.trim()}>
                        <IconSend />Send
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <Markdown>{m.content || ""}</Markdown>
                    {/* Edit and Rerun are BLOCKED while this turn's stream is
                        still draining. Stop clears `busy` immediately, but the
                        server ids are only applied when the drained turn
                        settles — so in that window `m.id` is undefined, the
                        request carries edit_message_id: null, `_persist` skips
                        its DELETE and APPENDS, and the database silently grows
                        a duplicate of the question being replaced. A stopped
                        turn is the last one, so laterTurnsLost() is 0 and no
                        confirmation fires either. The existing regression test
                        waits the drain out before rerunning, which is exactly
                        why this window survived it. A turn whose stream THREW
                        is the same state by a different route — settleTurn
                        deletes its entry, so it READS as finished, but `done`
                        never arrived and the id never landed. Asking whether
                        the id is there covers both.
                        aria-disabled, not `disabled`: a natively disabled
                        button is unfocusable, so the title explaining WHY
                        could never be read. The handlers early-return. */}
                    <div className="msg-actions user-actions">
                      <button className="link ico"
                              aria-disabled={turnIdsPending(m) || undefined}
                              onClick={(e) => { if (turnIdsPending(m)) return;
                                startEdit(i, m.content, e.currentTarget); }}
                              title={turnIdsPending(m)
                                ? "Unavailable until this question is saved to the chat"
                                : "Edit this prompt"}><IconEdit />Edit</button>
                      <button className="link ico" disabled={busy}
                              aria-disabled={turnIdsPending(m) || undefined}
                              onClick={() => { if (turnIdsPending(m)) return; rerun(i); }}
                              title={turnIdsPending(m)
                                ? "Unavailable until this question is saved to the chat"
                                : "Run this prompt again"}><IconRerun />Rerun</button>
                      {formatStamp(m.created_at) && (
                        <span className="msg-time" title="When you asked"
                              aria-label={`Asked at ${formatStamp(m.created_at)}`}>
                          {formatStamp(m.created_at)}
                        </span>
                      )}
                    </div>
                  </>
                )}
                {m.role === "assistant" && !m.pending && (
                  <>
                    <div className="msg-actions">
                      {thoughtLabel(m.duration_ms) && !m.error && (
                        <span className="msg-time" title="Time to generate this answer"
                              aria-label={`Answer generated — ${thoughtLabel(m.duration_ms)}`}>
                          {thoughtLabel(m.duration_ms)}
                        </span>
                      )}
                      {/* A failed turn's recovery lives ON the failure, not
                          hidden up on the user message's Rerun. */}
                      {m.error && messages[i - 1]?.role === "user" && (
                        <button className="link ico" onClick={() => rerun(i - 1)} disabled={busy}
                                title="Ask this question again"><IconRerun />Try again</button>
                      )}
                      {/* Thinking/SQL are toggle buttons, not inline <details> —
                          the expanded content renders full-width BELOW this row
                          (see the trace-panel below), so opening one never
                          reflows the copy buttons, and the two are mutually
                          exclusive. */}
                      {m.thinking?.length > 0 && (
                        <button type="button" className="link trace-toggle"
                                aria-expanded={openTrace === `${i}:thinking`}
                                aria-controls={`trace-${i}`}
                                onClick={() => toggleTrace(i, "thinking")}>Thinking</button>
                      )}
                      {m.sql_log?.length > 0 && (
                        <button type="button" className="link trace-toggle"
                                aria-expanded={openTrace === `${i}:sql`}
                                aria-controls={`trace-${i}`}
                                onClick={() => toggleTrace(i, "sql")}>SQL</button>
                      )}
                      {m.content && (
                        <CopyMenu
                          onCopyMarkdown={() => doCopy(i, "md", m.content)}
                          onCopyHtml={() => doCopy(i, "html", m.content)}
                          copied={copied === `${i}:md` || copied === `${i}:html`}
                        />
                      )}
                    </div>
                    {openTrace === `${i}:thinking` && m.thinking?.length > 0 && (
                      <div className="trace-panel" id={`trace-${i}`}>
                        <ThinkingTrace items={m.thinking} />
                      </div>
                    )}
                    {openTrace === `${i}:sql` && m.sql_log?.length > 0 && (
                      <div className="trace-panel" id={`trace-${i}`}>
                        <button className="link sql-copy"
                                onClick={async () => {
                                  // Same silent-failure shape as doCopy above.
                                  if (await copyText(m.sql_log.join(";\n\n"))) {
                                    setCopied(`${i}:sql`); setTimeout(() => setCopied(null), 1400);
                                  } else toast(COPY_FAILED, "error");
                                }}>
                          {copied === `${i}:sql` ? "Copied!" : "Copy SQL"}
                        </button>
                        <SqlBlock code={m.sql_log.join(";\n\n")} />
                      </div>
                    )}
                    {/* Drill-down chips — clicking one asks it as a follow-up turn
                        (which gets its own brief), an exploration loop.
                        askFromChip, not submit, so activating a chip doesn't
                        strand focus on <body> when it disables itself. */}
                    <Suggestions items={m.suggestions} onAsk={askFromChip} disabled={busy} />
                    {/* Disambiguation answer-phrase chips — clicking one submits the
                        short phrase verbatim as a follow-up turn. The composer stays
                        the free-text escape hatch. showQuestion is a defensive
                        fallback: if the model emitted the clarify fence with no
                        surrounding prose, m.content is empty and the chips would
                        otherwise be unlabeled — Clarify then shows its own
                        question as the heading instead of "Did you mean". */}
                    <Clarify spec={m.clarify} onAsk={askFromChip} disabled={busy}
                             showQuestion={!m.content || !m.content.trim()} />
                  </>
                )}
              </div>
            </div>
          ))}
          {/* A turn still running that this view isn't drawing itself — you
              asked, wandered off, and came back.

              Rendered OUTSIDE messages.map on purpose. `i` is load-bearing in
              six places (React key, mdRefs, the openTrace key AND the
              `trace-${i}` DOM id, edit/rerun indices, the copy key), so keeping
              these rows out of the loop means none of that can collide —
              structurally, not by being careful. Keys are registry-minted and
              globally unique.

              The user row deliberately has no .msg-actions: Edit and Rerun index
              into `messages`, and this row isn't in it and has no server id yet.
              Reuses the live bubble's CSS so the spinner is pixel-identical, but
              with fixed copy rather than `status` — we don't park the live
              trace, and replaying a stale "Running query…" would be a lie. */}
          {pendingTurns.map((t) => (
            <React.Fragment key={`pending-${t.key}`}>
              <div className="msg user">
                <div className="bubble"><Markdown>{t.question}</Markdown></div>
              </div>
              <div className="msg assistant">
                <div className="bubble">
                  <div aria-live="polite" aria-busy="true">
                    <div className="thinking-live">
                      <div className="thinking-head">
                        <span className="spinner" aria-hidden="true" />
                        <span className="muted">Still working on your question…</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </React.Fragment>
          ))}
          <div ref={bottom} />
          </div>
        </div>

        {/* Above the composer, only while scrolled up: the way back down. */}
        {showJump && (
          <button type="button" className="jump-latest" onClick={jumpToLatest}
                  aria-label="Jump to latest message">
            <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 5v13M6 12l6 6 6-6" fill="none" stroke="currentColor"
                    strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Latest
          </button>
        )}
        <form className="composer" onSubmit={send}>
          <div className="composer-box">
            <label htmlFor="composer-input" className="sr-only">Ask about IPEDS data</label>
            <MarkdownTextarea
              id="composer-input" ref={taRef}
              value={input} placeholder="Ask about IPEDS data…  (Shift-Enter for a new line)"
              // Mirrors chat.py's MAX_QUESTION_LEN so the browser stops an
              // over-long question before it is sent; the server cap is the
              // backstop, not the primary UX. Keep the two in sync.
              maxLength={MAX_QUESTION_LEN}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) send(e); }}
            />
            {busy ? (
              <button type="button" className="send stop" onClick={stopGenerating}
                      aria-label="Stop generating" title="Stop generating">
                <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">
                  <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" />
                </svg>
              </button>
            ) : (
              <button type="submit" className="send" disabled={!input.trim()}
                      aria-label="Send" title="Send">
                <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M4 12h14M12 5l7 7-7 7" fill="none" stroke="currentColor"
                        strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            )}
          </div>
        </form>
      </main>
    </div>
  );
}
