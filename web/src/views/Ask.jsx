import React, { useEffect, useState } from "react";
import { api } from "../api.js";

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

// The operator chat. The composer is enabled only when a model is actually
// configured -- otherwise a banner says exactly what to set, and the rest of
// the console (dashboard, approvals) still works. This is the one view that
// depends on the LLM, and it says so honestly rather than failing on submit.
export default function Ask({ principalKey }) {
  const [status, setStatus] = useState(null);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.askStatus().then(setStatus).catch(() => setStatus({ model_configured: false }));
  }, []);

  async function submit() {
    if (!q.trim()) return;
    setBusy(true);
    setError(null);
    setAnswer(null);
    try {
      const r = await api.ask(q.trim());
      setAnswer(r);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey && !busy && q.trim() && status?.model_configured) {
      e.preventDefault();
      submit();
    }
  }

  const enabled = status?.model_configured;

  return (
    <div>
      <div className="view-title">Ask PROOF</div>
      <div className="view-sub">
        One plain question. The coordinator plans, delegates to domain agents, and
        composes an answer — with full tool call transparency shown below.
      </div>

      {status && !enabled && (
        <div className="banner warn">
          No model connected. Set <code>ANTHROPIC_API_KEY</code> in{" "}
          <code>.env</code> to enable Ask. The dashboard and approvals work
          without it.
        </div>
      )}

      <div className="card">
        <div className="composer">
          <textarea
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="e.g. Line 3 is down, what's at risk?"
            disabled={!enabled || busy}
          />
          <button
            className="btn primary"
            onClick={submit}
            disabled={!enabled || busy || !q.trim()}
            style={{ minWidth: 90, height: 52 }}
          >
            {busy ? (
              <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="loading" style={{ padding: 0, fontSize: 0 }} />
                Thinking
              </span>
            ) : (
              "Ask →"
            )}
          </button>
        </div>
        <div className="chip-row">
          {EXAMPLES.map((ex, i) => (
            <button
              key={i}
              className="chip"
              onClick={() => setQ(ex)}
              disabled={busy}
            >
              {ex.length > 48 ? ex.slice(0, 46) + "…" : ex}
            </button>
          ))}
        </div>
      </div>

      {error && <div className="banner err">{error}</div>}

      {answer && (
        <>
          <div className="card" style={{ borderLeft: "3px solid var(--fgf-orange)" }}>
            <div className="answer">{answer.answer}</div>
          </div>

          {answer.delegations?.length > 0 && (
            <div className="card">
              <h3>Agent Delegations & Tool Calls</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 4 }}>
                {answer.delegations.map((d, i) => {
                  const dc = DOMAIN_COLORS[d.domain] || DOMAIN_COLORS.production;
                  return (
                    <div
                      key={i}
                      style={{
                        background: "var(--surface-2)",
                        borderRadius: "var(--radius-sm)",
                        padding: "14px 16px",
                        borderLeft: `3px solid ${dc.color}`,
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                        <div
                          style={{
                            width: 28,
                            height: 28,
                            borderRadius: 6,
                            background: dc.bg,
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            fontSize: 14,
                          }}
                        >
                          {dc.icon}
                        </div>
                        <span
                          style={{
                            fontWeight: 700,
                            fontSize: 13,
                            color: dc.color,
                            textTransform: "uppercase",
                            letterSpacing: "0.5px",
                          }}
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
                            style={{ fontSize: 10 }}
                          >
                            citations: {d.citation_status}
                          </span>
                        )}
                        {d.authorization === "denied" && (
                          <span className="pill danger" style={{ fontSize: 10 }}>
                            REFUSED
                          </span>
                        )}
                      </div>
                      {d.tools.length > 0 && (
                        <div
                          style={{
                            fontFamily: "var(--mono)",
                            fontSize: 12,
                            color: "var(--ink-3)",
                            display: "flex",
                            flexWrap: "wrap",
                            gap: 6,
                          }}
                        >
                          {d.tools.map((t, j) => (
                            <span
                              key={j}
                              style={{
                                background: "var(--surface-3)",
                                padding: "3px 8px",
                                borderRadius: 4,
                                border: "1px solid var(--line)",
                                fontSize: 11,
                                color: "var(--ink-2)",
                              }}
                            >
                              {t}()
                            </span>
                          ))}
                        </div>
                      )}
                      {d.tools.length === 0 && (
                        <span style={{ fontSize: 12, color: "var(--ink-dim)" }}>
                          no tools called
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
              <div
                style={{
                  marginTop: 14,
                  fontSize: 12,
                  color: "var(--ink-3)",
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  fontFamily: "var(--mono)",
                }}
              >
                <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <span style={{
                    width: 6, height: 6,
                    borderRadius: "50%",
                    background: "var(--accent)",
                    display: "inline-block",
                  }} />
                  {answer.model_calls} model calls
                </span>
                <span>·</span>
                <span style={{ color: "var(--fgf-orange-light)" }}>
                  ${answer.cost_usd}
                </span>
                {answer.budget_tripped && (
                  <>
                    <span>·</span>
                    <span className="pill danger" style={{ fontSize: 10 }}>
                      {answer.budget_tripped}
                    </span>
                  </>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
