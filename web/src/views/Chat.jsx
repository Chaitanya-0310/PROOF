import React, { useEffect, useState, useRef, useCallback } from "react";
import { api } from "../api.js";
import Markdown from "../components/Markdown.jsx";

const fmt = (n) => (typeof n === "number" ? n.toLocaleString() : n);

const EXAMPLES = [
  "Line 3 at TOR1 just went down, sheeter failure, maintenance says 90 minutes. What's at risk and what should I do?",
  "The flour delivery to TOR1 is running late. What does that break?",
  "Scrap is up on our sweet goods this month. Which line, and why?",
];

const DOMAIN_COLORS = {
  production: { bg: "var(--danger-bg)", color: "var(--danger)", icon: "⚙" },
  inventory: { bg: "var(--warn-bg)", color: "var(--warn)", icon: "📦" },
  demand: { bg: "rgba(100, 181, 246, 0.1)", color: "#64B5F6", icon: "📋" },
  quality: { bg: "var(--accent-bg)", color: "var(--accent)", icon: "✓" },
  actions: { bg: "var(--fgf-orange-glow)", color: "var(--fgf-orange)", icon: "▶" },
};

const TYPE_ICONS = {
  reallocate_run: "↗",
  notify_customer: "✉",
  expedite_purchase_order: "⚡",
  hold_product: "⏸",
};

// ─── Message Types ──────────────────────────────────────────────────
// Each message in the conversation is one of:
//   { role: "system",  text }           — initial greeting / status
//   { role: "user",    text }           — what the operator typed
//   { role: "agent",   text, meta }     — PROOF's answer w/ delegations & cost
//   { role: "action",  action }         — a pending approval card (inline HITL)
//   { role: "decided", actionId, event, message }  — outcome of a decision

let msgIdCounter = 0;
function nextId() { return ++msgIdCounter; }

// ─── Tool Call Expander ─────────────────────────────────────────────
function ToolCallSection({ delegations, initiallyOpen = false }) {
  const [open, setOpen] = useState(initiallyOpen);
  if (!delegations || delegations.length === 0) return null;

  const totalTools = delegations.reduce((s, d) => s + d.tools.length, 0);

  return (
    <div className="chat-tools-section">
      <button
        className="chat-tools-toggle"
        onClick={() => setOpen(!open)}
      >
        <span className="chat-tools-toggle-icon">{open ? "▾" : "▸"}</span>
        <span className="chat-tools-toggle-label">
          {totalTools} tool call{totalTools !== 1 ? "s" : ""} across {delegations.length} agent{delegations.length !== 1 ? "s" : ""}
        </span>
      </button>
      {open && (
        <div className="chat-tools-list">
          {delegations.map((d, i) => {
            const dc = DOMAIN_COLORS[d.domain] || DOMAIN_COLORS.production;
            return (
              <div key={i} className="chat-tool-domain">
                <div className="chat-tool-domain-header">
                  <span
                    className="chat-tool-domain-icon"
                    style={{ background: dc.bg, color: dc.color }}
                  >
                    {dc.icon}
                  </span>
                  <span
                    className="chat-tool-domain-name"
                    style={{ color: dc.color }}
                  >
                    {d.domain}
                  </span>
                  {d.citation_status !== "n/a" && (
                    <span
                      className={`pill ${
                        d.citation_status === "ok"
                          ? "accent"
                          : d.citation_status === "fabricated"
                          ? "danger"
                          : "warn"
                      }`}
                    >
                      citations: {d.citation_status}
                    </span>
                  )}
                  {d.authorization === "denied" && (
                    <span className="pill danger">REFUSED</span>
                  )}
                </div>
                <div className="chat-tool-calls">
                  {d.tools.map((t, j) => (
                    <div key={j} className="chat-tool-item">
                      <span className="chat-tool-badge">{t.name}()</span>
                      {t.explanation && (
                        <div className="chat-tool-explanation">
                          {t.explanation}
                        </div>
                      )}
                    </div>
                  ))}
                  {d.tools.length === 0 && (
                    <span className="chat-tool-empty">no tools called</span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─── Inline Action Card (HITL) ──────────────────────────────────────
function ActionCard({ action, onDecide }) {
  const [deciding, setDeciding] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [result, setResult] = useState(null);
  const icon = TYPE_ICONS[action.action_type] || "·";

  async function handleDecide(event) {
    setDeciding(true);
    try {
      const note = event === "approved" ? "approved from chat" : "rejected from chat";
      const r = await api.decide(action.action_id, event, note);
      setResult({ ok: true, event, message: r.message });
      if (onDecide) onDecide(action.action_id, event, r.message);
    } catch (e) {
      setResult({ ok: false, message: e.message });
    } finally {
      setDeciding(false);
    }
  }

  const isResolved = result?.ok;

  return (
    <div className={`chat-action-card ${isResolved ? "resolved" : ""}`}>
      <div className="chat-action-header">
        <div className="chat-action-icon">{icon}</div>
        <div className="chat-action-title">
          <div className="chat-action-type">
            {action.action_type.replace(/_/g, " ")}
          </div>
          <div className="chat-action-meta">
            <span className="pill orange" style={{ fontFamily: "var(--mono)" }}>
              #{action.action_id}
            </span>
            <span className="pill">{action.plant_code}</span>
            <span style={{ color: "var(--ink-dim)", fontSize: 11 }}>
              proposed by {action.proposed_by}
            </span>
          </div>
        </div>
      </div>
      <div className="chat-action-rationale">{action.rationale}</div>

      <button
        className="chat-action-details-toggle"
        onClick={() => setExpanded(!expanded)}
      >
        {expanded ? "Hide details ▴" : "Show details ▾"}
      </button>

      {expanded && action.payload && (
        <div className="chat-action-payload">
          {Object.entries(action.payload).map(([k, v]) => (
            <div key={k} className="chat-action-kv">
              <span className="chat-action-key">{k.replace(/_/g, " ")}</span>
              <span className="chat-action-value">{fmt(v)}</span>
            </div>
          ))}
        </div>
      )}

      {result && (
        <div className={`banner ${result.ok ? "ok" : "err"}`} style={{ marginTop: 10, marginBottom: 0 }}>
          {result.message}
        </div>
      )}

      {!isResolved && (
        <div className="chat-action-buttons">
          <button
            className="btn primary"
            onClick={() => handleDecide("approved")}
            disabled={deciding}
          >
            {deciding ? "…" : "✓  Approve"}
          </button>
          <button
            className="btn danger"
            onClick={() => handleDecide("rejected")}
            disabled={deciding}
          >
            {deciding ? "…" : "✕  Reject"}
          </button>
        </div>
      )}
    </div>
  );
}


// ─── Main Chat View ─────────────────────────────────────────────────
export default function Chat({ principalKey }) {
  const [status, setStatus] = useState(null);
  const [messages, setMessages] = useState([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const chatEndRef = useRef(null);
  const textareaRef = useRef(null);
  // The identity the pending-actions effect last ran for. Decides "greeting"
  // vs "identity changed" -- message count cannot, because StrictMode runs
  // the effect twice on mount and the second run would see the first's
  // greeting and announce an identity change that never happened.
  const lastPrincipalRef = useRef(null);

  // Scroll to bottom on new messages
  const scrollToBottom = useCallback(() => {
    setTimeout(() => {
      chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }, 50);
  }, []);

  // Check if the model is configured
  useEffect(() => {
    api.askStatus().then(setStatus).catch(() => setStatus({ model_configured: false }));
  }, []);

  // On mount: greet + load pending actions as inline HITL cards
  useEffect(() => {
    // Ignore a response that arrives after this effect was torn down --
    // StrictMode's discarded first run, or an identity switched mid-request.
    // Without this, both runs append and the transcript gets duplicates.
    let cancelled = false;
    const identityChanged =
      lastPrincipalRef.current !== null && lastPrincipalRef.current !== principalKey;
    lastPrincipalRef.current = principalKey;

    api.pending().then((d) => {
      if (cancelled) return;
      const pending = d.rows || [];

      setMessages(prev => {
        const msgs = [...prev];
        if (!identityChanged) {
          if (msgs.length === 0) {
            msgs.push({
              id: nextId(),
              role: "system",
              text: "PROOF is online. Ask me anything about the plant — or review the pending actions below.",
            });
          }
        } else {
          msgs.push({
            id: nextId(),
            role: "system",
            text: "Active identity changed. Checking pending actions...",
          });
        }

        if (pending.length > 0) {
          msgs.push({
            id: nextId(),
            role: "agent",
            text: `I have ${pending.length} proposed action${pending.length !== 1 ? "s" : ""} waiting for your review. You can approve or reject each one inline.`,
          });
          pending.forEach((a) => {
            msgs.push({ id: nextId(), role: "action", action: a });
          });
        }
        return msgs;
      });
      scrollToBottom();
    }).catch(() => {
      if (cancelled) return;
      setMessages(prev => prev.length === 0 ? [{
        id: nextId(),
        role: "system",
        text: "PROOF is online. Ask me anything about the plant — or review the pending actions below.",
      }] : prev);
    });
    return () => { cancelled = true; };
  }, [principalKey, scrollToBottom]);

  // Submit a question
  async function submit() {
    if (!q.trim() || busy) return;
    const question = q.trim();
    setQ("");

    // Add user message
    const userMsg = { id: nextId(), role: "user", text: question };
    setMessages((prev) => [...prev, userMsg]);
    scrollToBottom();

    // Add thinking indicator
    const thinkingId = nextId();
    setMessages((prev) => [...prev, { id: thinkingId, role: "thinking", delegations: [] }]);
    scrollToBottom();

    setBusy(true);
    try {
      const r = await api.ask(question, (event) => {
        setMessages((prev) => prev.map(m => {
          if (m.id !== thinkingId) return m;
          const updated = { ...m };
          if (event.type === "delegate_start") {
             updated.delegations = [...updated.delegations, { domain: event.data.domain, tools: [] }];
          } else if (event.type === "tool_call") {
             const ds = [...updated.delegations];
             const last = ds.find(d => d.domain === event.data.domain);
             if (last) {
               // Prevent exact duplicate tool calls from showing if multiple events fire
               const exists = last.tools.find(t => t.name === event.data.tool && t.explanation === event.data.explanation);
               if (!exists) {
                 last.tools.push({ name: event.data.tool, explanation: event.data.explanation });
               }
             }
             updated.delegations = ds;
          }
          return updated;
        }));
        scrollToBottom();
      });

      // Replace thinking with agent response
      setMessages((prev) => {
        const without = prev.filter((m) => m.id !== thinkingId);
        const agentMsg = {
          id: nextId(),
          role: "agent",
          text: r.answer,
          meta: {
            delegations: r.delegations,
            cost_usd: r.cost_usd,
            model_calls: r.model_calls,
            budget_tripped: r.budget_tripped,
          },
        };
        return [...without, agentMsg];
      });

      // After agent replies, reload pending actions — new ones may have been proposed
      try {
        const pd = await api.pending();
        const newActions = pd.rows || [];
        if (newActions.length > 0) {
          setMessages((prev) => {
            // Find action IDs already in chat
            const existingIds = new Set(
              prev.filter((m) => m.role === "action").map((m) => m.action.action_id)
            );
            const fresh = newActions.filter((a) => !existingIds.has(a.action_id));
            if (fresh.length === 0) return prev;

            const newMsgs = [
              {
                id: nextId(),
                role: "agent",
                text: `I've proposed ${fresh.length} new action${fresh.length !== 1 ? "s" : ""}. Please review:`,
              },
              ...fresh.map((a) => ({ id: nextId(), role: "action", action: a })),
            ];
            return [...prev, ...newMsgs];
          });
        }
      } catch { /* actions reload failure is non-fatal */ }

    } catch (e) {
      // Replace thinking with error
      setMessages((prev) => {
        const without = prev.filter((m) => m.id !== thinkingId);
        return [
          ...without,
          { id: nextId(), role: "error", text: e.message },
        ];
      });
    } finally {
      setBusy(false);
      scrollToBottom();
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function handleDecide(actionId, event, message) {
    setMessages((prev) => [
      ...prev,
      {
        id: nextId(),
        role: "decided",
        actionId,
        event,
        message,
      },
    ]);
    scrollToBottom();
  }

  const enabled = status?.model_configured;
  // Suggestions are for starting a conversation, so they stay until the
  // operator has asked something. Greeting, identity notices and pending
  // action cards are not questions and must not hide them.
  const hasAsked = messages.some((m) => m.role === "user");

  return (
    <div className="chat-container">
      {/* Chat messages area */}
      <div className="chat-messages">
        {status && !enabled && (
          <div className="banner warn" style={{ margin: "0 0 16px" }}>
            No model connected. Set <code>ANTHROPIC_API_KEY</code> in{" "}
            <code>.env</code> to enable the agent. Pending actions below still
            work — the dashboard and approvals need no LLM.
          </div>
        )}

        {messages.map((m) => {
          switch (m.role) {
            case "system":
              return (
                <div key={m.id} className="chat-msg chat-msg-system">
                  <div className="chat-msg-avatar chat-avatar-system">P</div>
                  <div className="chat-msg-body">
                    <div className="chat-msg-sender">PROOF</div>
                    <div className="chat-msg-text">{m.text}</div>
                  </div>
                </div>
              );

            case "user":
              return (
                <div key={m.id} className="chat-msg chat-msg-user">
                  <div className="chat-msg-body chat-msg-body-user">
                    <div className="chat-msg-text">{m.text}</div>
                  </div>
                  <div className="chat-msg-avatar chat-avatar-user">You</div>
                </div>
              );

            case "agent":
              return (
                <div key={m.id} className="chat-msg chat-msg-agent">
                  <div className="chat-msg-avatar chat-avatar-agent">P</div>
                  <div className="chat-msg-body">
                    <div className="chat-msg-sender">PROOF</div>
                    <Markdown className="chat-msg-text chat-msg-text-agent">
                      {m.text}
                    </Markdown>
                    {m.meta && (
                      <>
                        <ToolCallSection delegations={m.meta.delegations} />
                        {m.meta.cost_usd != null && (
                          <div className="chat-msg-cost">
                            <span className="chat-cost-dot" />
                            {m.meta.model_calls} model calls · ${m.meta.cost_usd}
                            {m.meta.budget_tripped && (
                              <span className="pill danger" style={{ marginLeft: 8 }}>
                                {m.meta.budget_tripped}
                              </span>
                            )}
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </div>
              );

            case "action":
              return (
                <div key={m.id} className="chat-msg chat-msg-agent">
                  <div className="chat-msg-avatar chat-avatar-agent">P</div>
                  <div className="chat-msg-body" style={{ width: "100%", maxWidth: "100%" }}>
                    <ActionCard action={m.action} onDecide={handleDecide} />
                  </div>
                </div>
              );

            case "decided":
              return (
                <div key={m.id} className="chat-msg chat-msg-system">
                  <div className="chat-msg-avatar chat-avatar-system">P</div>
                  <div className="chat-msg-body">
                    <div className={`banner ${m.event === "approved" ? "ok" : "err"}`} style={{ margin: 0 }}>
                      Action #{m.actionId} {m.event}. {m.message}
                    </div>
                  </div>
                </div>
              );

            case "thinking":
              return (
                <div key={m.id} className="chat-msg chat-msg-agent">
                  <div className="chat-msg-avatar chat-avatar-agent">P</div>
                  <div className="chat-msg-body">
                    <div className="chat-msg-sender">PROOF</div>
                    
                    {m.delegations && m.delegations.length > 0 && (
                      <div style={{ marginBottom: 12, marginTop: 4 }}>
                        <ToolCallSection delegations={m.delegations} initiallyOpen={true} />
                      </div>
                    )}
                    
                    <div className="chat-thinking">
                      <span className="chat-thinking-dot" />
                      <span className="chat-thinking-dot" />
                      <span className="chat-thinking-dot" />
                    </div>
                  </div>
                </div>
              );

            case "error":
              return (
                <div key={m.id} className="chat-msg chat-msg-agent">
                  <div className="chat-msg-avatar chat-avatar-agent" style={{ background: "var(--danger-bg)", color: "var(--danger)" }}>!</div>
                  <div className="chat-msg-body">
                    <div className="banner err" style={{ margin: 0 }}>{m.text}</div>
                  </div>
                </div>
              );

            default:
              return null;
          }
        })}
        <div ref={chatEndRef} />
      </div>

      {/* Sticky composer at bottom */}
      <div className="chat-composer-wrap">
        <div className="chat-composer">
          {!hasAsked && (
            <div className="chip-row" style={{ marginTop: 0, marginBottom: 10 }}>
              {EXAMPLES.map((ex, i) => (
                <button
                  key={i}
                  className="chip"
                  onClick={() => setQ(ex)}
                  disabled={busy}
                >
                  {ex.length > 52 ? ex.slice(0, 50) + "…" : ex}
                </button>
              ))}
            </div>
          )}
          <div className="chat-input-row">
            <textarea
              ref={textareaRef}
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={enabled ? "Ask PROOF anything about your plant…" : "Connect an LLM to start asking questions"}
              disabled={!enabled || busy}
              rows={1}
            />
            <button
              className="btn primary chat-send-btn"
              onClick={submit}
              disabled={!enabled || busy || !q.trim()}
            >
              {busy ? "…" : "↑"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
