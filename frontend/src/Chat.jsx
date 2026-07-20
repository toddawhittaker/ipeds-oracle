import React, { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, streamChat } from "./api.js";
import { IconClose, IconEdit, IconRerun, IconSend, IconTrash } from "./icons.jsx";
import Markdown from "./Markdown.jsx";
import MarkdownTextarea from "./MarkdownTextarea.jsx";
import Figure from "./Figure.jsx";
import Suggestions from "./Suggestions.jsx";
import SqlBlock from "./SqlBlock.jsx";
import { DELETE_FAILED, deleteAnnouncement } from "./announce.js";
import { useConfirm } from "./ConfirmModal.jsx";
import { useToast } from "./Toast.jsx";
import { shouldRedirectTyping, targetInfo } from "./typeahead.js";

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

// Copy plain text, falling back to execCommand for non-secure contexts
// (navigator.clipboard is undefined over plain http on a LAN IP).
async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through */ }
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  document.body.removeChild(ta);
  return ok;
}

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
    <div className="thought-list thin-scroll">
      {items.map((t, j) => {
        if (t.kind === "sql") return <SqlBlock key={j} code={t.text} className="thought-sql" />;
        if (t.kind === "reason") return <p key={j} className="thought-reason">{t.text}</p>;
        return <div key={j} className="thought-line muted">{t.text}</div>;
      })}
    </div>
  );
}

// A route :id is only ever a plain conversation id (see api.js); anything
// else (e.g. "abc") is a malformed URL, not a real conversation, and must
// never reach the network -- same notice, zero fetch.
const NUMERIC_ID = /^\d+$/;
const NOT_AVAILABLE = "That conversation isn't available.";

export default function Chat({ me }) {
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
  // Marks the CURRENT turn's own self-navigation (the "conversation" SSE
  // handler's / -> /chat/:id URL flip for a brand-new conversation) so the
  // [routeId] turnToken effect below doesn't mistake it for the user
  // navigating away and abandon the very turn that's still rendering.
  const selfNavId = useRef(null);

  // The URL changed out from under us -- sidebar click, "+ New chat",
  // delete-the-open-chat, or browser Back/Forward -- so reset local thread
  // state to match. This has to happen DURING RENDER, not in an effect:
  // react-hooks/set-state-in-effect is an ERROR in this repo's eslint config,
  // and an effect here would also mean an extra render with stale messages
  // visible before the reset lands.
  if (openId !== routeId) {
    // Also free the composer in the view navigated TO -- `busy`/`status` are
    // single shared state, not per-turn, so leaving them set would strand
    // the user here until the abandoned turn's stream resolves elsewhere.
    // This deliberately does NOT fire on the happy-path self-nav (the
    // `conversation` handler pre-syncs setOpenId so openId === routeId by
    // the time this render runs).
    setOpenId(routeId); setMessages([]); setNotice(""); setEditingIdx(null);
    setBusy(false); setStatus("");
    // Entering a conversation route -> skeleton until its fetch settles
    // (the loader effect's callbacks flip it back). Entering "/" -> no load.
    setLoadingConvo(routeId !== null && NUMERIC_ID.test(routeId));
    // A fresh view starts pinned to the latest message (the nearBottom ref
    // itself is reset in the [routeId] effect below — a ref can't legally be
    // written during render).
    setShowJump(false);
  }
  const badFormat = routeId !== null && !NUMERIC_ID.test(routeId);
  const showNotice = notice || (badFormat ? NOT_AVAILABLE : "");
  const convId = routeId !== null && !badFormat && !notice ? Number(routeId) : null;

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
  }, [messages, status]);

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
  useEffect(() => {
    if (routeId === null) { loadedFor.current = null; return; }
    if (loadedFor.current === routeId) return;
    loadedFor.current = routeId;
    if (!NUMERIC_ID.test(routeId)) return;
    let cancelled = false;
    api.conversation(routeId)
      .then((msgs) => {
        if (cancelled) return;
        setMessages(msgs.map((m) => ({
          ...m,
          sql_log: m.sql_log ? JSON.parse(m.sql_log) : [],
          thinking: m.thinking ? JSON.parse(m.thinking) : [],
          figure: m.figure ? JSON.parse(m.figure) : null,
          suggestions: m.suggestions ? JSON.parse(m.suggestions) : null,
        })));
        setLoadingConvo(false);
      })
      .catch(() => { if (!cancelled) { setNotice(NOT_AVAILABLE); setLoadingConvo(false); } });
    return () => { cancelled = true; };
  }, [routeId]);

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
    // conversation), consume the marker and don't abandon it -- this relies
    // on navigate() being a synchronous (non-transition) history update,
    // same load-bearing precondition as the main.jsx v7_startTransition
    // warning referenced in submit() below.
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
    if (ok) { setCopied(`${i}:${kind}`); setTimeout(() => setCopied(null), 1400); }
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
    nearBottom.current = true; // your own question always scrolls into view
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
    setBusy(false); setStatus("");
    setMessages((m) => {
      const c = [...m]; const i = c.length - 1;
      if (i >= 0 && c[i].pending) c[i] = { ...c[i], pending: false, stopped: true };
      return c;
    });
    requestAnimationFrame(() => taRef.current?.focus());
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
  function saveEdit(i) {
    const text = editText.trim();
    if (!text || busy) return;
    const editMessageId = messages[i]?.id;
    setEditingIdx(null); setEditText("");
    setMessages((m) => m.slice(0, i));   // drop this turn + everything after
    submit(text, { editMessageId });
    requestAnimationFrame(() => taRef.current?.focus());  // land focus in composer
  }
  // Rerun a prior prompt as-is (e.g. after a failure), replacing from that point.
  function rerun(i) {
    if (busy) return;
    const msg = messages[i];
    if (!msg) return;
    setMessages((m) => m.slice(0, i));
    submit(msg.content, { editMessageId: msg.id });
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
    const myTurn = turnToken.current; // see the `conversation` SSE handler below
    // True only while this is still the turn the user is looking at. Stale
    // (abandoned) turns must keep draining the stream to completion -- see
    // the note atop submit() -- but their VIEW writes must stop the instant
    // the user has moved on, so they don't bleed into whatever conversation
    // is now on screen.
    const isMine = () => turnToken.current === myTurn;
    setBusy(true); setStatus("Thinking…");
    setMessages((m) => [...m, { role: "user", content: q },
                              { role: "assistant", content: "", sql_log: [], thinking: [], pending: true }]);

    // Immutably patch the in-flight (last) message.
    const patchLast = (patch) => setMessages((m) => {
      const c = [...m]; const i = c.length - 1;
      if (i >= 0) c[i] = typeof patch === "function" ? patch(c[i]) : { ...c[i], ...patch };
      return c;
    });
    const addThought = (item) =>
      patchLast((last) => ({ ...last, thinking: [...(last.thinking || []), item] }));

    let answer = "", sqlLog = [], newConvId = convId, msgId = null, userMsgId = null, newTitle = null;
    let figure = null; // the structured hero statistic, when the model emitted one
    let suggestions = null; // drill-down "you might also ask" questions
    let failed = false; // drives the finalized message's inline "Try again"
    try {
      await streamChat({ question: q, conversationId: convId, editMessageId }, (ev) => {
        if (ev.type === "conversation") {
          newConvId = ev.id;
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
          // LOAD-BEARING PRECONDITION, WARNING FOR FUTURE MAINTAINERS: this
          // batching guarantee holds only because navigate() here is a plain
          // synchronous history update, NOT a React transition. If
          // <BrowserRouter future={{ v7_startTransition: true }}> is ever
          // added (main.jsx) -- the documented v6->v7 migration step, and
          // the v7 DEFAULT -- navigate() below starts deferring its location
          // update as a transition: React would commit openId/routeId
          // BEFORE the transition resolves, the render-time reset above
          // would wipe the just-streamed answer, and loadedFor.current
          // already being set to this id would stop the loader effect from
          // ever refetching it -- the answer would be gone permanently. See
          // the matching warning at main.jsx.
          loadedFor.current = String(ev.id);
          setOpenId(String(ev.id));
          // Only a brand-new conversation (convId === null) actually flips
          // routeId here -- an existing-conversation turn's routeId is
          // already correct, so marking self-nav for it would linger
          // unconsumed and mask a LATER genuine navigation-away.
          if (convId === null) selfNavId.current = String(ev.id);
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
        else if (ev.type === "error") { answer = "⚠️ " + ev.text; failed = true; }
        else if (ev.type === "done") {
          if (ev.message_id) msgId = ev.message_id;
          if (ev.user_message_id) userMsgId = ev.user_message_id;
          if (ev.title) newTitle = ev.title;
        }
      });
    } catch (err) {
      answer = "⚠️ " + err.message;
      failed = true;
    }
    // VIEW writes -- gated: a stale (abandoned) turn's final answer must not
    // land in whatever conversation is now on screen, and must not leave
    // that conversation's composer stuck disabled. The stream still drained
    // to completion above, so the answer IS persisted server-side; reopening
    // that conversation will show it.
    if (isMine()) {
      setMessages((m) => {
        const c = [...m];
        const ai = c.length - 1, ui = c.length - 2;
        if (ai >= 0) c[ai] = { ...c[ai], role: "assistant", content: answer, sql_log: sqlLog, figure, suggestions, id: msgId ?? c[ai].id, pending: false, error: failed };
        if (ui >= 0 && userMsgId) c[ui] = { ...c[ui], id: userMsgId };
        return c;
      });
      setBusy(false); setStatus("");
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
            {collapsed ? "»" : "«"}
          </button>
          {!collapsed && <Link to="/" className="newchat" onClick={handleNewChat}>+ New chat</Link>}
        </div>
        {collapsed ? (
          <Link to="/" className="icon-btn newchat-collapsed" onClick={handleNewChat}
                title="New chat" aria-label="New chat">+</Link>
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
          {!loadingConvo && messages.length === 0 && !me?.has_data && (
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
          {!loadingConvo && messages.length === 0 && me?.has_data && (
            <div className="empty">
              <span className="field-label">Ask the record</span>
              <h2 className="empty-prompt">What would you like to know about U.S. colleges?</h2>
              <p className="muted">
                Degrees awarded, enrollment, tuition, graduation rates, staffing
                and finance — across collection years 2019-20 through 2024-25.
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
            <div key={i} className={"msg " + m.role + (editingIdx === i ? " editing" : "")}>
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
                        {m.thinking?.length > 0 && (
                          <details className="thoughts">
                            <summary>Thinking</summary>
                            <ThinkingTrace items={m.thinking} />
                          </details>
                        )}
                      </div>
                    ) : m.stopped ? (
                      <p className="stopped-note">
                        Stopped. If the answer finishes generating, it will be
                        saved to this chat — reopen it in a moment to check.
                      </p>
                    ) : (
                      <>
                        {/* Sibling BEFORE <Markdown> (outside the .md node
                            mdRefs targets), so the hero figure sits above the
                            prose and stays out of the copy surface. Renders
                            null when the message carries no figure. */}
                        <Figure spec={m.figure} />
                        <Markdown>{m.content || ""}</Markdown>
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
                    <div className="msg-actions user-actions">
                      <button className="link ico" onClick={(e) => startEdit(i, m.content, e.currentTarget)}
                              title="Edit this prompt"><IconEdit />Edit</button>
                      <button className="link ico" onClick={() => rerun(i)} disabled={busy}
                              title="Run this prompt again"><IconRerun />Rerun</button>
                    </div>
                  </>
                )}
                {m.role === "assistant" && !m.pending && (
                  <>
                    <div className="msg-actions">
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
                        <>
                          <button className="link" onClick={() => doCopy(i, "md", m.content)}
                                  title="Copy the answer as Markdown">
                            {copied === `${i}:md` ? "Copied!" : "Copy Markdown"}
                          </button>
                          <button className="link" onClick={() => doCopy(i, "html", m.content)}
                                  title="Copy the answer as rich HTML (paste into email/Word)">
                            {copied === `${i}:html` ? "Copied!" : "Copy HTML"}
                          </button>
                        </>
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
                                  if (await copyText(m.sql_log.join(";\n\n"))) {
                                    setCopied(`${i}:sql`); setTimeout(() => setCopied(null), 1400);
                                  }
                                }}>
                          {copied === `${i}:sql` ? "Copied!" : "Copy SQL"}
                        </button>
                        <SqlBlock code={m.sql_log.join(";\n\n")} />
                      </div>
                    )}
                    {/* Drill-down chips — clicking one asks it as a follow-up turn
                        (which gets its own brief), an exploration loop. */}
                    <Suggestions items={m.suggestions} onAsk={(q) => submit(q)} disabled={busy} />
                  </>
                )}
              </div>
            </div>
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
