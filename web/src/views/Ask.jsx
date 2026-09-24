import React, { useEffect, useState } from "react";
import { api } from "../api.js";

const EXAMPLES = [
  "Line 3 at TOR1 just went down, sheeter failure, maintenance says 90 minutes. What's at risk and what should I do?",
  "The flour delivery to TOR1 is running late. What does that break?",
  "Scrap is up on our sweet goods this month. Which line, and why?",
];

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

  const enabled = status?.model_configured;

  return (
    <div>
      <div className="view-title">Ask</div>
      <div className="view-sub">
        One plain question. The coordinator plans, asks the domain agents, and
        composes an answer — with the tools it used shown underneath.
      </div>

      {status && !enabled && (
        <div className="banner warn">
          No model connected. Set <code>OPENROUTER_API_KEY</code> (DeepSeek) in{" "}
          <code>.env</code> to enable Ask. The dashboard and approvals work
          without it.
        </div>
      )}

      <div className="card">
        <div className="composer">
          <textarea
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="e.g. Line 3 is down, what's at risk?"
            disabled={!enabled || busy}
          />
          <button
            className="btn primary"
            onClick={submit}
            disabled={!enabled || busy || !q.trim()}
          >
            {busy ? "Thinking…" : "Ask"}
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
        <div className="card">
          <div className="answer">{answer.answer}</div>
          {answer.delegations?.length > 0 && (
            <>
              <h3 style={{ marginTop: 18 }}>How it got there</h3>
              <div className="trace">
                {answer.delegations.map((d, i) => (
                  <div key={i}>
                    <span className="dom">{d.domain}</span> ·{" "}
                    {d.tools.join(", ") || "no tools"}
                    {d.citation_status !== "n/a" &&
                      ` · citations: ${d.citation_status}`}
                    {d.authorization === "denied" && " · REFUSED"}
                  </div>
                ))}
              </div>
              <div className="muted" style={{ marginTop: 10, fontSize: 13 }}>
                {answer.model_calls} model calls · ${answer.cost_usd}
                {answer.budget_tripped && ` · ${answer.budget_tripped}`}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
