import React, { useEffect, useRef, useState } from "react";
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

export default function Chat() {
  const [convos, setConvos] = useState([]);
  const [convId, setConvId] = useState(null);
  const [messages, setMessages] = useState([]); // {role, content, id?, sql_log?, feedback?, status?}
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

  const refreshConvos = () => api.conversations().then(setConvos).catch(() => {});
  useEffect(() => { refreshConvos(); }, []);
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

  async function doCopy(i, kind, markdown) {
    const text = stripChartBlocks(markdown);
    const ok = kind === "html"
      ? await copyHtml(mdRefs.current[i], text)
      : await copyText(text);
    if (ok) { setCopied(`${i}:${kind}`); setTimeout(() => setCopied(null), 1400); }
  }

  async function openConvo(id) {
    setConvId(id);
    const msgs = await api.conversation(id);
    setMessages(msgs.map((m) => ({
      ...m, sql_log: m.sql_log ? JSON.parse(m.sql_log) : [],
    })));
  }

  function newChat() { setConvId(null); setMessages([]); }

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
    if (!window.confirm("Delete this chat? This can't be undone.")) return;
    await api.deleteConversation(id).catch(() => {});
    if (id === convId) newChat();
    refreshConvos();
  }

  async function submit(q, { editMessageId = null } = {}) {
    q = (q || "").trim();
    if (!q || busy) return;
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
        if (ev.type === "conversation") { newConvId = ev.id; setConvId(ev.id); }
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

  async function vote(msg, value) {
    if (!msg.id) return;
    await api.feedback(msg.id, value);
    setMessages((m) => m.map((x) => x.id === msg.id ? { ...x, feedback: value } : x));
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
                <button type="button"
                        className={"convo" + (c.id === convId ? " on" : "")}
                        aria-current={c.id === convId ? "page" : undefined}
                        onClick={() => openConvo(c.id)}>
                  {c.title || "Untitled"}
                </button>
                <button type="button" className="convo-del"
                        onClick={(e) => deleteConvo(e, c.id)}
                        title="Delete chat" aria-label="Delete chat"><IconTrash /></button>
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
        <div className="messages thin-scroll">
          <div className="messages-inner">
          {messages.length === 0 && (
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
                <strong>⚠️ Do not enter proprietary or confidential Franklin
                information.</strong> To keep costs down, this tool uses DeepSeek,
                which may train on the data you submit. Ask only about public IPEDS
                data — <strong>no</strong> student records, internal figures, or
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
                    {m.id && (
                      <>
                        <span className="spacer" />
                        <button className={"vote" + (m.feedback === 1 ? " on" : "")}
                                onClick={() => vote(m, 1)} title="Helpful"
                                aria-label="Helpful" aria-pressed={m.feedback === 1}>👍</button>
                        <button className={"vote" + (m.feedback === -1 ? " on" : "")}
                                onClick={() => vote(m, -1)} title="Not helpful"
                                aria-label="Not helpful" aria-pressed={m.feedback === -1}>👎</button>
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
