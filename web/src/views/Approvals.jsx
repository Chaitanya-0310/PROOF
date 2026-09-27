import React, { useEffect, useState } from "react";
import { api } from "../api.js";

const fmt = (n) => (typeof n === "number" ? n.toLocaleString() : n);

const TYPE_ICONS = {
  reallocate_run: "↗",
  notify_customer: "✉",
  expedite_purchase_order: "⚡",
  hold_product: "⏸",
};

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
  const [decidingId, setDecidingId] = useState(null);

  const load = () => {
    setRows(null);
    setError(null);
    api.pending().then((d) => setRows(d.rows)).catch((e) => setError(e.message));
  };
  useEffect(load, [principalKey]);

  async function decide(id, event) {
    setResult(null);
    setDecidingId(id);
    try {
      const note =
        event === "approved" ? "approved from console" : "rejected from console";
      const r = await api.decide(id, event, note);
      setResult({ ok: true, message: r.message });
      load();
    } catch (e) {
      // A 403 lands here with the authz layer's exact reason.
      setResult({ ok: false, message: e.message });
    } finally {
      setDecidingId(null);
    }
  }

  if (error) return <div className="banner err">{error}</div>;

  return (
    <div>
      <div className="view-title">Approvals</div>
      <div className="view-sub">
        Actions the agent proposed, waiting for a human decision. Nothing here has
        changed the plant yet. Whether you can approve depends on your role — the
        server enforces it, not this UI.
      </div>

      {result && (
        <div className={"banner " + (result.ok ? "ok" : "err")}>
          {result.message}
        </div>
      )}

      {!rows ? (
        <div className="loading">Loading queue</div>
      ) : rows.length === 0 ? (
        <div className="card">
          <div className="empty-state">
            <div className="empty-state-icon">✓</div>
            <div className="empty-state-text">Nothing pending — the queue is clear</div>
          </div>
        </div>
      ) : (
        rows.map((a) => {
          const icon = TYPE_ICONS[a.action_type] || "·";
          const isOpen = openId === a.action_id;
          return (
            <div key={a.action_id} className="action-item">
              <div
                className="action-head"
                onClick={() => setOpenId(isOpen ? null : a.action_id)}
              >
                <div
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: 8,
                    background: "var(--fgf-orange-glow)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 16,
                    flexShrink: 0,
                  }}
                >
                  {icon}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span
                      className="pill orange"
                      style={{ fontFamily: "var(--mono)" }}
                    >
                      #{a.action_id}
                    </span>
                    <b style={{ fontSize: 14 }}>
                      {a.action_type.replace(/_/g, " ")}
                    </b>
                    <span className="pill">{a.plant_code}</span>
                  </div>
                  <div
                    className="muted"
                    style={{ fontSize: 12, marginTop: 3 }}
                  >
                    proposed by {a.proposed_by}
                  </div>
                </div>
                <span
                  style={{
                    color: "var(--ink-dim)",
                    fontSize: 18,
                    transition: "transform 0.2s ease",
                    transform: isOpen ? "rotate(180deg)" : "rotate(0)",
                  }}
                >
                  ▾
                </span>
              </div>
              {isOpen && (
                <div className="action-body">
                  <div className="asked">{a.rationale}</div>
                  <div className="kv">
                    {Object.entries(a.payload || {}).map(([k, v]) => (
                      <React.Fragment key={k}>
                        <div className="k">{k.replace(/_/g, " ")}</div>
                        <div style={{ fontWeight: 500 }}>{fmt(v)}</div>
                      </React.Fragment>
                    ))}
                  </div>
                  <div className="btn-row">
                    <button
                      className="btn primary"
                      onClick={() => decide(a.action_id, "approved")}
                      disabled={decidingId === a.action_id}
                    >
                      {decidingId === a.action_id ? "Processing…" : "✓  Approve"}
                    </button>
                    <button
                      className="btn danger"
                      onClick={() => decide(a.action_id, "rejected")}
                      disabled={decidingId === a.action_id}
                    >
                      ✕  Reject
                    </button>
                  </div>
                </div>
              )}
            </div>
          );
        })
      )}
    </div>
  );
}
