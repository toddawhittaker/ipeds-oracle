import React, { useEffect, useRef, useState } from "react";
import { api, streamChat } from "./api.js";
import Markdown from "./Markdown.jsx";

export default function Chat() {
  const [convos, setConvos] = useState([]);
  const [convId, setConvId] = useState(null);
  const [messages, setMessages] = useState([]); // {role, content, id?, sql_log?, feedback?, status?}
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const bottom = useRef(null);

  const refreshConvos = () => api.conversations().then(setConvos).catch(() => {});
  useEffect(() => { refreshConvos(); }, []);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, status]);

  async function openConvo(id) {
    setConvId(id);
    const msgs = await api.conversation(id);
    setMessages(msgs.map((m) => ({
      ...m, sql_log: m.sql_log ? JSON.parse(m.sql_log) : [],
    })));
  }

  function newChat() { setConvId(null); setMessages([]); }

  async function send(e) {
    e?.preventDefault();
    const q = input.trim();
    if (!q || busy) return;
    setInput(""); setBusy(true); setStatus("Thinking…");
    setMessages((m) => [...m, { role: "user", content: q },
                              { role: "assistant", content: "", sql_log: [], pending: true }]);

    let answer = "", sqlLog = [], newConvId = convId;
    try {
      await streamChat({ question: q, conversationId: convId }, (ev) => {
        if (ev.type === "conversation") { newConvId = ev.id; setConvId(ev.id); }
        else if (ev.type === "status") setStatus(ev.text);
        else if (ev.type === "sql") { sqlLog = [...sqlLog, ev.sql]; setStatus("Running query…"); }
        else if (ev.type === "answer") answer = ev.text;
        else if (ev.type === "error") answer = "⚠️ " + ev.text;
      });
    } catch (err) {
      answer = "⚠️ " + err.message;
    }
    setMessages((m) => {
      const copy = [...m];
      copy[copy.length - 1] = { role: "assistant", content: answer, sql_log: sqlLog, pending: false };
      return copy;
    });
    setBusy(false); setStatus("");
    // reload to pick up message ids (for feedback/csv) and refresh sidebar
    if (newConvId) { openConvo(newConvId); refreshConvos(); }
  }

  async function vote(msg, value) {
    if (!msg.id) return;
    await api.feedback(msg.id, value);
    setMessages((m) => m.map((x) => x.id === msg.id ? { ...x, feedback: value } : x));
  }

  return (
    <div className="chat">
      <aside className="sidebar">
        <button className="newchat" onClick={newChat}>+ New chat</button>
        <div className="convo-list">
          {convos.map((c) => (
            <div key={c.id}
                 className={"convo" + (c.id === convId ? " on" : "")}
                 onClick={() => openConvo(c.id)}>
              {c.title || "Untitled"}
            </div>
          ))}
        </div>
      </aside>

      <main className="thread">
        <div className="messages">
          {messages.length === 0 && (
            <div className="empty muted">
              Ask a question about IPEDS data — degrees awarded, enrollment,
              tuition, graduation rates, and more, across 2020-21 → 2024-25.
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={"msg " + m.role}>
              <div className="bubble">
                {m.role === "assistant" && m.pending && !m.content
                  ? <div className="muted">{status || "…"}</div>
                  : <Markdown>{m.content || ""}</Markdown>}
                {m.role === "assistant" && !m.pending && (
                  <div className="msg-actions">
                    {m.sql_log?.length > 0 && (
                      <details className="sql">
                        <summary>SQL</summary>
                        <pre>{m.sql_log.join(";\n\n")}</pre>
                      </details>
                    )}
                    {m.id && (
                      <>
                        <a className="link" href={api.csvUrl(m.id)}>Download CSV</a>
                        <span className="spacer" />
                        <button className={"vote" + (m.feedback === 1 ? " on" : "")}
                                onClick={() => vote(m, 1)} title="Helpful">👍</button>
                        <button className={"vote" + (m.feedback === -1 ? " on" : "")}
                                onClick={() => vote(m, -1)} title="Not helpful">👎</button>
                      </>
                    )}
                  </div>
                )}
              </div>
            </div>
          ))}
          <div ref={bottom} />
        </div>

        <form className="composer" onSubmit={send}>
          <textarea
            rows={1} value={input} placeholder="Ask about IPEDS data…"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) send(e); }}
          />
          <button type="submit" disabled={busy || !input.trim()}>Send</button>
        </form>
      </main>
    </div>
  );
}
