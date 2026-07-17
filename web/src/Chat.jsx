import React, { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, streamChat } from "./api.js";
import { IconClose, IconEdit, IconRerun, IconSend, IconTrash } from "./icons.jsx";
import Markdown from "./Markdown.jsx";

// Clickable starter prompts shown on the empty chat screen.
const EXAMPLES = [
  "Top 20 institutions awarding Associate's degrees in Registered Nursing (CIP 51.3801) over the last 3 years.",
  "How many Computer Science (CIP 11.0701) bachelor's degrees did California public universities award last year?",
  "National total of Associate's degrees per year, all programs.",
  "Which states awarded the most Master's degrees in Education?",
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
        if (t.kind === "sql") return <pre key={j} className="thought-sql">{t.text}</pre>;
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
  const [openId, setOpenId] = useState(routeId);
  const [notice, setNotice] = useState("");
  const [deleteAnnounce, setDeleteAnnounce] = useState("");
  const loadedFor = useRef(null); // routeId this conversation's messages were last fetched for
  const [messages, setMessages] = useState([]); // {role, content, id?, sql_log?, status?}
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [copied, setCopied] = useState(null); // `${i}:${kind}` most recently copied
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
  // Bumped by newChat() and by any real route change (effect below) to mark
  // whichever stream is currently in flight as abandoned. submit() captures
  // the value at call time; the `conversation` SSE handler compares against
  // it before yanking the viewer to the new /chat/:id -- see the "'+ New
  // chat' mid-stream" fix in submit() below.
  const turnToken = useRef(0);

  // The URL changed out from under us -- sidebar click, "+ New chat",
  // delete-the-open-chat, or browser Back/Forward -- so reset local thread
  // state to match. This has to happen DURING RENDER, not in an effect:
  // react-hooks/set-state-in-effect is an ERROR in this repo's eslint config,
  // and an effect here would also mean an extra render with stale messages
  // visible before the reset lands.
  if (openId !== routeId) {
    setOpenId(routeId); setMessages([]); setNotice(""); setEditingIdx(null);
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
  useEffect(() => {
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    bottom.current?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
  }, [messages, status]);
  // Auto-grow the composer to fit multi-line input (Shift-Enter adds a line).
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, [input]);

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
        setMessages(msgs.map((m) => ({ ...m, sql_log: m.sql_log ? JSON.parse(m.sql_log) : [] })));
      })
      .catch(() => { if (!cancelled) setNotice(NOT_AVAILABLE); });
    return () => { cancelled = true; };
  }, [routeId]);

  // A real route change -- sidebar click to a different chat, browser Back/
  // Forward, delete-the-open-chat -- means the viewer has moved on from
  // whichever turn was in flight when it happened. Bump the token so that
  // turn's `conversation` SSE handler (if it lands later) won't yank them
  // back. (newChat() bumps it directly too, since starting a fresh "/"
  // thread from an already-"/" URL never changes routeId, so this effect
  // alone wouldn't catch that case -- see newChat() below.)
  useEffect(() => { turnToken.current++; }, [routeId]);

  async function doCopy(i, kind, markdown) {
    const text = stripChartBlocks(markdown);
    const ok = kind === "html"
      ? await copyHtml(mdRefs.current[i], text)
      : await copyText(text);
    if (ok) { setCopied(`${i}:${kind}`); setTimeout(() => setCopied(null), 1400); }
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
  function newChat() {
    // Abandon whichever turn is in flight -- see turnToken's declaration and
    // the `conversation` SSE handler in submit() below. The user is entitled
    // to walk away from a stream; it just must not yank them back afterward.
    turnToken.current++;
    if (routeId === null) { setMessages([]); setNotice(""); setEditingIdx(null); }
    else navigate("/");
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

  async function deleteConvo(e, id) {
    e.stopPropagation();
    // Snapshot everything the post-delete UI needs from THIS render's convos
    // BEFORE the await -- refreshConvos() resolves when setConvos is called,
    // not after commit (React 18 batches), so `convos` read after the await
    // could already be stale/wrong. idx/next/remaining below are all derived
    // from this pre-delete snapshot.
    const idx = convos.findIndex((c) => c.id === id);
    const title = (idx >= 0 ? convos[idx].title : "") || "Untitled";
    if (!window.confirm(`Delete "${title}"? This can't be undone.`)) return;
    const isOpen = id === convId;
    const next = convos[idx + 1] || convos[idx - 1] || null;
    const remaining = Math.max(convos.length - 1, 0);
    const ok = await api.deleteConversation(id).then(() => true).catch(() => false);
    if (!ok) { setDeleteAnnounce("Couldn't delete that chat."); return; }
    if (isOpen) {
      // Case 1: deleting the OPEN conversation. Focus goes to the composer,
      // via the same navigate()+rAF precedent as fillExample/saveEdit --
      // deliberately NOT through focusAfterDelete/the [convos] effect below,
      // which targets a sidebar row on a different clock (refreshConvos()
      // landing later would otherwise steal focus back out of the composer).
      setDeleteAnnounce(`Deleted "${title}". Started a new chat.`);
      navigate("/");
      requestAnimationFrame(() => taRef.current?.focus());
    } else {
      // Case 2: deleting a DIFFERENT conversation. Focus whatever now
      // occupies the deleted row's index once refreshConvos() actually
      // commits -- see the [convos] effect above.
      focusAfterDelete.current = next ? { id: next.id } : { newchat: true };
      // The remaining-count is LOAD-BEARING, not chatty: a live region only
      // announces on a text MUTATION, and two chats both titled "Untitled"
      // (the render fallback below) would otherwise produce an IDENTICAL
      // announcement -- the second delete would be silently swallowed. The
      // count strictly decreases, so consecutive announcements always differ.
      setDeleteAnnounce(remaining === 0
        ? `Deleted "${title}". No chats remaining.`
        : `Deleted "${title}". ${remaining} ${remaining === 1 ? "chat" : "chats"} remaining.`);
    }
    refreshConvos();
  }

  async function submit(q, { editMessageId = null } = {}) {
    q = (q || "").trim();
    if (!q || busy) return;
    const myTurn = turnToken.current; // see the `conversation` SSE handler below
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
    try {
      await streamChat({ question: q, conversationId: convId, editMessageId }, (ev) => {
        if (ev.type === "conversation") {
          newConvId = ev.id;
          // The viewer may have already abandoned this turn -- "+ New chat"
          // mid-stream, a sidebar click to a different chat, browser Back/
          // Forward, or deleting the open chat -- before this event landed.
          // turnToken is bumped by newChat() and by any real route change
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
          navigate(`/chat/${ev.id}`, { replace: true });
        }
        else if (ev.type === "status") { setStatus(ev.text); addThought({ kind: "status", text: ev.text }); }
        else if (ev.type === "sql") { sqlLog = [...sqlLog, ev.sql]; setStatus("Running query…"); addThought({ kind: "sql", text: ev.sql }); }
        else if (ev.type === "thinking") addThought({ kind: "reason", text: ev.text });
        else if (ev.type === "tool") addThought({ kind: "tool", text: `${ev.name}${ev.ok ? " ✓" : " ✗"}` });
        else if (ev.type === "answer") answer = ev.text;
        else if (ev.type === "error") answer = "⚠️ " + ev.text;
        else if (ev.type === "done") {
          if (ev.message_id) msgId = ev.message_id;
          if (ev.user_message_id) userMsgId = ev.user_message_id;
          if (ev.title) newTitle = ev.title;
        }
      });
    } catch (err) {
      answer = "⚠️ " + err.message;
    }
    setMessages((m) => {
      const c = [...m];
      const ai = c.length - 1, ui = c.length - 2;
      if (ai >= 0) c[ai] = { ...c[ai], role: "assistant", content: answer, sql_log: sqlLog, id: msgId ?? c[ai].id, pending: false };
      if (ui >= 0 && userMsgId) c[ui] = { ...c[ui], id: userMsgId };
      return c;
    });
    setBusy(false); setStatus("");
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
          {!collapsed && <button className="newchat" onClick={newChat}>+ New chat</button>}
        </div>
        {collapsed ? (
          <button className="icon-btn newchat-collapsed" onClick={newChat}
                  title="New chat" aria-label="New chat">+</button>
        ) : (
          <div className="convo-list thin-scroll">
            {convos.map((c) => (
              <div key={c.id} className={"convo-row" + (c.id === convId ? " on" : "")}>
                <button type="button" id={`convo-${c.id}`}
                        className={"convo" + (c.id === convId ? " on" : "")}
                        aria-current={c.id === convId ? "page" : undefined}
                        onClick={() => navigate(`/chat/${c.id}`)}>
                  {c.title || "Untitled"}
                </button>
                <button type="button" className="convo-del"
                        onClick={(e) => deleteConvo(e, c.id)}
                        title="Delete chat"
                        aria-label={`Delete chat: ${c.title || "Untitled"}`}><IconTrash /></button>
              </div>
            ))}
          </div>
        )}
        {/* Always mounted (outside the collapsed ternary above) so a delete
            while the sidebar is collapsed still announces. Deliberately a
            BARE aria-live, never role="status" -- Chat's bad-conversation
            notice below is already a role="status" node, and several e2e
            specs assert an UNSCOPED page.getByRole("status") resolves to
            exactly one match (same reasoning as App.jsx's route-announcer).
            A separate `deleteAnnounce` state, not `notice` -- the render-time
            reset above (openId !== routeId) does setNotice("") on every route
            change, which would wipe this out from under case 1 (delete the
            open chat navigates to "/") before it could be heard. */}
        <div className="sr-only" aria-live="polite" data-testid="delete-announcer">
          {deleteAnnounce}
        </div>
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
        <div className="messages thin-scroll">
          <div className="messages-inner">
          {/* Rendered ABOVE the empty state, URL left as-is -- navigating away
              would re-run the render-time reset above and wipe the notice
              right when the user needs to see it. Never renders the server's
              `detail` (see NOT_AVAILABLE): a 404 (doesn't exist) and a 403
              (not yours) must read identically so this can't be used to
              enumerate other users' conversation ids. */}
          {showNotice && <div className="notice">{showNotice}</div>}
          <div className="sr-only" role="status" aria-live="polite">{showNotice}</div>
          {messages.length === 0 && !me?.has_data && (
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
          {messages.length === 0 && me?.has_data && (
            <div className="empty">
              <p className="muted">
                Ask a question about IPEDS data — degrees awarded, enrollment,
                tuition, graduation rates, and more, across 2020-21 → 2024-25.
              </p>
              <div className="examples-grid">
                {EXAMPLES.map((ex) => (
                  <button key={ex} type="button" className="example-chip"
                          onClick={() => fillExample(ex)}>
                    {ex}
                  </button>
                ))}
              </div>
              <div className="privacy-warning" role="note">
                <strong>⚠️ Do not enter proprietary or confidential
                information.</strong> This tool sends your questions to a
                third-party AI service that may use submitted data to improve
                its models. Ask only about public IPEDS data —{" "}
                <strong>no</strong> student records, internal figures, or
                other non-public information.
              </div>
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
                    ) : (
                      <Markdown>{m.content || ""}</Markdown>
                    )}
                  </div>
                ) : editingIdx === i ? (
                  <div className="edit-box">
                    <textarea className="thin-scroll" value={editText} autoFocus
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
                  <div className="msg-actions">
                    {m.thinking?.length > 0 && (
                      <details className="sql">
                        <summary>Thinking</summary>
                        <ThinkingTrace items={m.thinking} />
                      </details>
                    )}
                    {m.sql_log?.length > 0 && (
                      <details className="sql">
                        <summary>SQL</summary>
                        <pre>{m.sql_log.join(";\n\n")}</pre>
                      </details>
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
                )}
              </div>
            </div>
          ))}
          <div ref={bottom} />
          </div>
        </div>

        <form className="composer" onSubmit={send}>
          <div className="composer-box">
            <label htmlFor="composer-input" className="sr-only">Ask about IPEDS data</label>
            <textarea
              id="composer-input" ref={taRef} className="thin-scroll"
              rows={1} value={input} placeholder="Ask about IPEDS data…  (Shift-Enter for a new line)"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) send(e); }}
            />
            <button type="submit" className="send" disabled={busy || !input.trim()}
                    aria-label="Send" title="Send">
              <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M4 12h14M12 5l7 7-7 7" fill="none" stroke="currentColor"
                      strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </div>
        </form>
      </main>
    </div>
  );
}
