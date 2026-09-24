import React, { useEffect, useState } from "react";
import { api } from "../api.js";

const fmt = (n) => (typeof n === "number" ? n.toLocaleString() : n);

// The human-in-the-loop screen. The buttons always attempt the decision; the
// SERVER decides whether the acting principal may. A refusal comes back as the
// authz layer's own words, so the UI never has to re-implement the rules --
// and can't get them subtly wrong. Switch to a plant manager in the top bar
// and the same Approve that was refused now succeeds.
export default function Approvals({ principalKey }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const [openId, setOpenId] = useState(null);
  const [result, setResult] = useState(null);

  const load = () => {
    setRows(null);
    setError(null);
    api.pending().then((d) => setRows(d.rows)).catch((e) => setError(e.message));
  };
  useEffect(load, [principalKey]);

  async function decide(id, event) {
    setResult(null);
    try {
      const note =
        event === "approved" ? "approved from console" : "rejected from console";
      const r = await api.decide(id, event, note);
      setResult({ ok: true, message: r.message });
      load();
    } catch (e) {
      // A 403 lands here with the authz layer's exact reason.
      setResult({ ok: false, message: e.message });
    }
  }

  if (error) return <div className="banner err">{error}</div>;

  return (
    <div>
      <div className="view-title">Approvals</div>
      <div className="view-sub">
        Actions the agent proposed, waiting for a human. Nothing here has
        changed the plant. Whether you can approve depends on who you're acting
        as — the server enforces it.
      </div>

      {result && (
        <div className={"banner " + (result.ok ? "ok" : "err")}>
          {result.message}
        </div>
      )}

      {!rows ? (
        <div className="loading">Loading queue…</div>
      ) : rows.length === 0 ? (
        <div className="card muted">Nothing pending.</div>
      ) : (
        rows.map((a) => (
          <div key={a.action_id} className="action-item">
            <div
              className="action-head"
              onClick={() => setOpenId(openId === a.action_id ? null : a.action_id)}
            >
              <span className="pill">#{a.action_id}</span>
              <b>{a.action_type.replace(/_/g, " ")}</b>
              <span className="pill">{a.plant_code}</span>
              <span className="muted">proposed by {a.proposed_by}</span>
              <div className="spacer" />
              <span className="muted">{openId === a.action_id ? "▲" : "▼"}</span>
            </div>
            {openId === a.action_id && (
              <div className="action-body">
                <div className="asked">
                  {a.rationale}
                </div>
                <div className="kv">
                  {Object.entries(a.payload || {}).map(([k, v]) => (
                    <React.Fragment key={k}>
                      <div className="k">{k.replace(/_/g, " ")}</div>
                      <div>{fmt(v)}</div>
                    </React.Fragment>
                  ))}
                </div>
                <div className="btn-row">
                  <button
                    className="btn primary"
                    onClick={() => decide(a.action_id, "approved")}
                  >
                    Approve
                  </button>
                  <button
                    className="btn danger"
                    onClick={() => decide(a.action_id, "rejected")}
                  >
                    Reject
                  </button>
                </div>
              </div>
            )}
          </div>
        ))
      )}
    </div>
  );
}
